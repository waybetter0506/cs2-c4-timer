"""主程序：把 GSI、状态机、悬浮窗接成一个能跑的整体。

数据流
------
    GSI 接收线程 ──▶ 状态机（加锁）──▶ 快照 ──▶ ticker 线程 ──▶ 悬浮窗

设计要点
--------
1. **只有红字**（炸弹时限）。拆弹的绿字已按需求移除。
   音频走过两个阶段：2026-09-16 那代"频带能量模板"整条移除（见
   docs/实机反馈与待办.md 第十五节）；2026-09-17 起以**指纹方案**回到产品
   （`audio/plant.py` + `audio/fingerprint.py`，两队都启用、确认后才采用），
   本文件里的 `_start_audio` / `_drain_audio` / `_handle_audio_hit` 就是它的接线。

2. **锚点只有两个**：观战时用服务端倒计时（准，0.25 秒粒度）；
   活着时用"安放报文 − 补偿值"。后者天生有秒级误差 —— Valve 在
   2015-12-17 的补丁里故意把 bomb-plant 的 GSI 事件延迟了（社区实测 0.5~2.2 秒随机），
   任何 cfg 调参都改不动。所以活着时宁可显示估算值，也不假装精确。

3. 状态机的所有变更都在 `_lock` 里；ticker 只读快照。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import render
from .config import Config
from .gsi import GsiReceiver, GsiSample
from .logs import SessionLog
from .paths import runtime_dir
from .single import SingleInstance
from . import refstats, tuning
from .state import CALIB_TIGHT, C4StateMachine

# 音频候选等 GSI 揭示包等多久（秒）。实测 GSI 延迟最长 2.6 秒，留一倍余量；
# 超时就丢弃候选 —— 因为「确认后才显示」，丢弃时屏幕上是干净的。
AUDIO_PENDING_SEC = 6.0
# 还在系统环回上时，每隔这么久重试一次进程环回（游戏起来后就能切过去）
AUDIO_RETRY_SEC = 20.0
# 候选命中时落盘多少秒音频（诊断/重标定用）
AUDIO_DUMP_SEC = 6.0
# 安放片段（参考素材）落盘多长 —— 与检测器的滑动窗等长（`FP_WINDOW_SEC`），
# 这样"实时能看到的音频"和"落盘留存的音频"是同一段，离线复盘才有可比性。
# 2026-09-18 由 8 秒改成 10 秒：8 秒时，揭示包来得早的那几局播报落在 6.35~6.50 秒处、
# 整句被尾巴截断 ⇒ 素材不可用（详见 app 里 _plant_dump_at 的注释）。
PLANT_DUMP_SEC = 10.0
# 落盘窗口以"命中时刻 + 这个尾巴"结尾（常量 ⇒ 各局落盘里播报位置一致）
AUDIO_DUMP_TAIL_SEC = 1.5
# 进程环回重试的总时限（秒）：超过就彻底放弃（本机 Win10 上它不可能成功）
AUDIO_RETRY_GIVEUP_SEC = 120.0
AUDIO_DUMP_DIR = runtime_dir() / "audio_dumps"
# 落盘文件的保留上限（2026-09-19 加）。这两类文件以前**只增不减**，谁也不管：
#   · live_*  每 30 秒一个 6 秒片段 —— 打一下午就能堆几百 MB；
#   · plant_* 每次安放一个 10 秒片段 —— 除了占地方，标定还要在它们身上做两两互证，
#     素材越多越慢（实测：25 段 1.8 秒 / 100 段 6.0 秒 / 131 段 8.9 秒）。
# 两个数都按"够用就行"取：标定最后只挑互证最好的 5 个当参考，候选堆到几百是浪费。
# 想改就改这两个数，改完不用动别的地方。
KEEP_PLANT_DUMPS = 300
KEEP_LIVE_DUMPS = 20

TICK_SECONDS = 0.02


def _prune_dumps(pattern: str, keep: int, directory: Path | None = None) -> int:
    """只留最近 keep 个匹配的文件，返回删掉几个。

    文件名形如 `plant_20260919_223214.wav`，名字里就是时间，所以**字典序 = 时间序**，
    排一下取尾部即可，不需要读文件时间戳（复制/解压会打乱 mtime，名字不会）。

    这是"用户永远不用自己清"的那一半：新文件写进去，最旧的自动被挤掉。
    删不掉（被占用等）不算错，下次再说 —— 清理失败绝不能影响录音与计时。
    """
    folder = AUDIO_DUMP_DIR if directory is None else Path(directory)
    try:
        files = sorted(folder.glob(pattern))
    except OSError:
        return 0
    if keep <= 0:
        return 0
    removed = 0
    for old in files[:-keep]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def raise_timer_resolution(enable: bool = True) -> bool:
    """把 Windows 的定时器精度提到 1ms；返回是否成功。

    为什么需要：默认调度精度约 15.6ms，`sleep(0.02)` 实际会睡 ~21.8ms，
    于是 20ms 的 tick 变成 21.8ms，读秒每 5 tick 走一格就是 109ms —— 而且
    抖动可达 ±30ms，看上去就是"一顿一顿"。CS2 自己也会提精度，
    这里跟着做一次，退出时还原。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        winmm = ctypes.WinDLL("winmm")
        return bool(winmm.timeBeginPeriod(1) == 0) if enable else \
            bool(winmm.timeEndPeriod(1) == 0)
    except Exception:                      # noqa: BLE001 —— 提不了也不影响功能
        return False


@dataclass
class Stats:
    gsi_packets: int = 0
    gsi_observer: int = 0
    started_mono: float = field(default_factory=time.monotonic)

    def line(self) -> str:
        up = time.monotonic() - self.started_mono
        return (f"[{up:7.1f}s] GSI {self.gsi_packets} 包(观战 {self.gsi_observer})")


class C4TimerApp:
    def __init__(self, cfg: Config, *,
                 enable_hud: bool = True, debug: bool = False) -> None:
        self.cfg = cfg
        self.enable_hud = enable_hud
        self.debug = debug or cfg.debug.print_gsi

        self.state = C4StateMachine(cfg)
        self.stats = Stats()
        self._lock = threading.RLock()
        self._stop = threading.Event()

        self.receiver = GsiReceiver(cfg.gsi.host, cfg.gsi.port, on_sample=self._on_gsi)
        self.hud = None

        self.single = SingleInstance()
        self.already_running = False
        log_path = Path(cfg.debug.log_path) if cfg.debug.log_path \
            else runtime_dir() / "c4timer.log"
        self.log = SessionLog(log_path, enabled=not getattr(cfg.debug, "no_log", False))
        # 已写进日志的事件序号
        self._events_written = 0
        self._last_sample: GsiSample | None = None
        # 自己最后一次已知阵营（活着时才知道；观战期间沿用）
        self._team: str | None = None
        # --- 音频（听"安放播报"）---
        self._audio = None               # LoopbackCapture；None = 不启用
        self._detector = None            # PlantVoiceDetector
        self._audio_names: list[str] = []
        self._audio_hits: queue.Queue = queue.Queue()
        # 待确认候选：(plant_mono, 分数, 模板名, 判定时刻)。等 GSI 揭示包来确认。
        self._pending_audio: tuple[float, float, str, float] | None = None
        self._prev_planted = False       # 用于识别"GSI 刚揭示安放"
        self._audio_retry_at: float | None = None   # 进程环回重试时刻
        self._reveal_mono: float | None = None
        # 本局揭示包的时刻（**不清空**，直到这一局不再是"已安放"）。
        # 2026-09-18 加：播报检测天然滞后，实测候选常**晚于**揭示包 0.35~0.93 秒，
        # 而 _reveal_mono 只在下一次 tick 里存活 ⇒ 迟到的候选永远等不到确认，6 秒后被丢。
        self._last_reveal_mono: float | None = None
        self._ticker: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 事件日志
    # ------------------------------------------------------------------

    def _drain_events(self) -> None:
        """把状态机的内部事件写进日志（DEBUG 标签）。

        以前这些事件只在 `--debug` 时打到控制台，日志里看不出"规则到底有没有触发"。
        """
        events = self.state.events
        # 用事件自带的单调序号（events 列表满了会从头部裁掉，按下标会漏）
        new = [ev for ev in events if ev.seq > self._events_written]
        for ev in new:
            self.log.write("DEBUG", ev.text)
        if new:
            self._events_written = new[-1].seq

    # ------------------------------------------------------------------
    # GSI
    # ------------------------------------------------------------------

    def _on_gsi(self, sample: GsiSample) -> None:
        with self._lock:
            self.stats.gsi_packets += 1
            if sample.observer:
                self.stats.gsi_observer += 1
            if sample.self_team:
                # 活着时 player 块就是自己 -> 记下阵营；观战期间沿用最后一次已知值
                self._team = sample.self_team
            self._last_sample = sample
            exp = "-" if sample.explosion_remaining is None else f"{sample.explosion_remaining:.1f}"
            deff = "-" if sample.defuse_remaining is None else f"{sample.defuse_remaining:.1f}"
            self.log.write("GSI", f"#{self.stats.gsi_packets} "
                                  f"phase={sample.round_phase or '-'} "
                                  f"r={sample.map_round if sample.map_round is not None else '-'} "
                                  f"bomb={sample.bomb_state or '-'} "
                                  f"raw={sample.bomb_state_raw or '-'} "
                                  f"exp={exp} def={deff} pcd={sample.pcd_phase or '-'} "
                                  f"obs={int(sample.observer)} team={self._team or '-'} "
                                  f"chg={int(sample.carried_delta)} "
                                  # 变更键（诊断用，2026-09-16 晚加）：验证"安放触发的那一包"
                                  # 能否从内容上认出来，见 docs/实机反馈与待办.md 第二十节
                                  f"Δ={','.join(sample.changed) if sample.changed else '-'}")
            planted_now = sample.bomb_state in ("planting", "planted", "defusing")
            if planted_now and not self._prev_planted:
                self._reveal_mono = sample.recv_mono
                self._last_reveal_mono = sample.recv_mono
                # 每次安放都安排一次落盘（安放后 2.5 秒取 10 秒）：播报必然在 GSI 揭示包
                # 之前 0.03~2.6 秒，所以窗口能把**整句**装进去。
                # 2026-09-18 加的：之前只落"命中前 6 秒"，实测只有 1/5 装到了播报。
                # 2026-09-18 再改（实机第二局暴露）：窗口原来 8 秒、在揭示包 +1.0 秒取，
                # 于是**揭示包来得早的那几局**播报落在片段末尾（实测 6.35 / 6.50 秒）⇒
                # 整句被尾巴截断，离线素材不可用（那些局实时检测照样过阈值，因为实时是
                # 10 秒滑窗）。改成"揭示包 +2.5 秒取 10 秒"（与检测器窗口等长）：
                # 播报位置从此稳定落在 5.0~7.5 秒之间，整句一定装得下。
                self._plant_dump_at = sample.recv_mono + 2.5
            self._prev_planted = planted_now
            if not planted_now:
                self._last_reveal_mono = None      # 这一局结束了，揭示包时刻作废
            self.state.feed_gsi(sample)
            self._drain_events()          # 拆弹内部事件写进日志（诊断用）

            # 每次"服务端报爆炸"打一行标定。这是唯一能量化"倒计时快了多少/慢了多少"
            # 的观测点。注意给的是**区间**：活着时 GSI 包可能隔几秒才来，
            # 「报文晚到 1.8 秒」和「我们快了 1.8 秒」分不开。只报一次，报完清掉。
            if sample.bomb_state == "exploded" and self.state.last_calibration is not None:
                lo, hi = self.state.last_calibration
                source = self.state.calibration_source or "-"
                self.state.last_calibration = None
                gap = (sample.recv_mono - self.state.prev_gsi_mono
                       if self.state.prev_gsi_mono is not None else 0.0)
                comp = self.cfg.timers.plant_latency_compensation
                if hi - lo <= CALIB_TIGHT and source == "gsi+comp":
                    mid = (lo + hi) / 2.0
                    tail = (f"建议={comp + mid:.2f}")
                elif hi - lo <= CALIB_TIGHT:
                    tail = "建议=—（T0 来自服务端真值，补偿没参与）"
                else:
                    tail = f"区间太宽（报文间隔 {gap:.2f}s），不给建议"
                self.log.write("CALIB", f"本局误差 [{lo:+.2f}, {hi:+.2f}]s  "
                                        f"T0来源={source}  "
                                        f"当前补偿={comp:.2f}  {tail}")
                # 自成长：只有**音频生效过**的局才进音频偏置的统计（缺局排除规则见
                # docs/实机反馈与待办.md 第二十八节；旁观位真值 gsi-truth 本来就没偏差可言）。
                if "audio" in source:
                    msg = tuning.observe((lo + hi) / 2.0, hi - lo, source)
                    if msg:
                        self.log.write("TUNE", msg)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def start(self) -> None:
        # 读秒要稳，先提定时器精度（默认 15.6ms 会让 20ms 的 tick 变成 ~21.8ms）
        raise_timer_resolution(True)
        # 单实例：两个实例会抢同一个 GSI 端口。
        # 用命名互斥体而不是"端口占用"，因为端口是资源不是身份 ——
        # 采集工具也用 30077，用端口判断会把"采集在跑"误报成"程序已在运行"。
        if not self.single.acquire():
            self.already_running = True
            raise SystemExit(
                "[X] c4timer 已经在运行了（单实例保护）。\n"
                "    先关掉那个窗口再启动。\n"
                "    如果确认没有窗口，可能是上次异常退出残留 —— 重开一次即可。"
            )

        try:
            self.receiver.start()
        except OSError as exc:
            self.single.release()       # 抢到互斥体后又失败，必须还回去
            raise SystemExit(
                f"[X] 无法监听 {self.cfg.gsi.host}:{self.cfg.gsi.port} —— {exc}\n"
                f"    端口被占用？先确认没有另一个 c4timer 实例在跑。\n"
                f"    或改配置 gsi.port（需同步改游戏 cfg）。"
            ) from exc

        self._start_audio()             # 失败也只是退回 GSI 估算，不影响启动
        self._write_start_banner()      # 放在 ticker 之前：让横幅成为一轮日志的第一行
        self._ticker = threading.Thread(target=self._tick_loop, name="ticker", daemon=True)
        self._ticker.start()

    def _write_start_banner(self) -> None:
        """启动横幅：日志里有了这一行，才能证明"这一次确实跑起来了"、以及
        当时生效的是哪套参数（实机排查时最常问的两个问题）。

        2026-09-16 加：用户报了两轮"过早归零"，但 `runtime/c4timer.log` 一个字都没长 ——
        没有这一行就分不清"程序没跑"和"跑了但没收到 GSI"。
        """
        if self._audio is not None:
            audio_status = f"on({','.join(self._audio_names)})"
        elif not self.cfg.audio.enabled:
            audio_status = "off"
        else:
            audio_status = "off(无模板)"        # 配置开着但没标定过模板
        self.log.write("START", f"c4timer 起来了 pid={os.getpid()} "
                                f"GSI={self.cfg.gsi.host}:{self.cfg.gsi.port} "
                                f"HUD={'on' if self.hud is not None else 'off'} "
                                f"音频={audio_status} "
                                f"补偿={self.cfg.timers.plant_latency_compensation:.2f} "
                                f"真爆滞后={self.cfg.timers.explode_after_deadline_sec:.2f} "
                                f"小数门槛={self.cfg.ui.decimal_below_sec:.0f}s")

    # ------------------------------------------------------------------
    # 音频：听"安放播报"拿真实安放时刻（2026-09-17 加）
    # ------------------------------------------------------------------

    def _start_audio(self) -> None:
        """按配置起音频采集 + 播报检测器。任何一步失败都只是"退回 GSI 估算"，不影响运行。"""
        if not self.cfg.audio.enabled:
            return
        try:
            from .audio.capture import LoopbackCapture
            from .audio.plant import ANALYSIS_RATE, PlantVoiceDetector, load_templates
        except ImportError as exc:                       # numpy 没装等情况
            self.log.write("AUDIO", f"音频不可用（{exc!r}）—— 只用 GSI 估算")
            return
        tdir = self.cfg.audio.template_dir or str(runtime_dir() / "audio_templates")
        templates = load_templates(tdir)
        if not templates:
            # 没有参考指纹也**照样开采集**（2026-09-18 修）。
            # 为什么：标定素材（每次安放自动存的 10 秒片段）就是靠采集攒的；从前这里直接
            # return => 新机器永远攒不到素材 => 自动标定死循环（朋友那边实测卡在这里）。
            self.log.write("AUDIO", f"还没有参考指纹（{tdir}）—— 先只录素材：打完一局后，"
                                    f"下次启动会自动标定；在此之前精度用 GSI 估算。")
        self._detector = PlantVoiceDetector(templates, threshold=self.cfg.audio.threshold,
                                            template_sec=self.cfg.audio.template_sec,
                                            min_hits=self.cfg.audio.min_hits)
        self._audio_names = [t.name for t in templates]
        self._analysis_rate = ANALYSIS_RATE
        # 把每个参考指纹的时长/哈希数/来源写进日志（2026-09-18 加）。
        # 为什么：实机"分数为什么这么低"的第一嫌疑永远是"程序加载的是哪一批参考"——
        # 曾因为程序启动早于重建参考而白测一轮；有了这行，一眼就能对上。
        try:
            import numpy as _np3
            from .audio import fingerprint as _fp3
            for t in templates:
                if t.pcm is None:
                    continue
                _n = sum(len(v) for v in
                         _fp3.reference_table(_np3.asarray(t.pcm, dtype=_np3.float32)).values())
                self.log.write("AUDIO", f"  参考 {t.name}：{len(t.pcm) / ANALYSIS_RATE:.2f}s，"
                                        f"{_n} 个哈希，onset={t.onset_sec:.2f}s，"
                                        f"来源 {t.source or '-'}")
        except Exception as exc:                     # noqa: BLE001 诊断信息不该影响启动
            self.log.write("AUDIO", f"  [!] 参考信息读取失败：{exc!r}")
        # 自成长统计摘要（2026-09-18）：让"自动"不神秘 —— 多少局、当前自动修正量多少
        self.log.write("AUDIO", f"  {tuning.summary()}")
        self._audio = LoopbackCapture(self.cfg.audio, self._on_pcm, rate=ANALYSIS_RATE)
        self._audio.start()
        if self._audio.error:
            self.log.write("AUDIO", f"音频采集失败（{self._audio.error}）—— 只用 GSI 估算")
            self._audio = None
            return
        for w in self._audio.warnings:
            self.log.write("AUDIO", f"[!] {w}")
        self.log.write("AUDIO", f"音频已启用：模板 {'/'.join(self._audio_names)}，"
                                f"阈值 {self.cfg.audio.threshold:.2f}，"
                                f"模板长 {self.cfg.audio.template_sec:.1f}s，"
                                f"来源 {self._audio.description}")
        # 启动时先清一次：以前是没有上限的，老用户的目录可能已经堆了几百 MB。
        # 只在真的超了的时候才写日志（正常情况下一个字都不会多）。
        for pattern, keep, what in (("plant_*.wav", KEEP_PLANT_DUMPS, "安放片段"),
                                    ("live_*.wav", KEEP_LIVE_DUMPS, "实时片段")):
            gone = _prune_dumps(pattern, keep)
            if gone:
                self.log.write("AUDIO", f"  启动清理：{what}超出上限，清掉最旧的 {gone} 个"
                                        f"（只留最近 {keep} 个）")

    def _retry_process_loopback(self, now: float) -> None:
        """只要还在"系统环回"上，就定期重试进程环回（只录 CS2 自己的声音）。

        实测教训：采集只在启动那一刻找一次 cs2.exe，游戏还没起来就永久退化成系统环回，
        于是别的程序的说话声也进了检测器（而模板正是人声播报）⇒ 大量误报。
        """
        cap = self._audio
        if cap is None or self._detector is None:
            return
        # 给重试加个总时限：进程环回在 Win10 上永远不可能成功（实测 49 次全失败），
        # 而每次重试都会重启采集、清空检测器缓冲 ⇒ 跑够 AUDIO_RETRY_GIVEUP_SEC 就彻底放弃。
        first = getattr(self, "_audio_retry_first", None)
        if first is None:
            self._audio_retry_first = now
        elif now - first > AUDIO_RETRY_GIVEUP_SEC:
            return
        if "system loopback" not in (cap.description or ""):
            return                                   # 已经是进程环回，不用管
        if self._audio_retry_at is not None and now < self._audio_retry_at:
            return
        self._audio_retry_at = now + AUDIO_RETRY_SEC
        try:
            from .audio.capture import LoopbackCapture
            from .audio.plant import ANALYSIS_RATE, PlantVoiceDetector
            cap.stop()
            new = LoopbackCapture(self.cfg.audio, self._on_pcm, rate=ANALYSIS_RATE)
            new.start()
            if new.error:
                raise RuntimeError(new.error)
            self._audio = new
            self._detector = PlantVoiceDetector(
                self._detector.templates, threshold=self.cfg.audio.threshold,
                template_sec=self.cfg.audio.template_sec,
                min_hits=self.cfg.audio.min_hits)
            if "system loopback" in (new.description or ""):
                self.log.write("AUDIO", f"重试进程环回：还没找到 cs2.exe，{AUDIO_RETRY_SEC:.0f}s 后再试"
                                        f"（当前仍录系统混音，误报可能偏多）")
            else:
                self.log.write("AUDIO", f"已切到进程环回（只录 CS2）：{new.description}")
        except Exception as exc:                     # noqa: BLE001 重试失败不影响运行
            self.log.write("AUDIO", f"重试进程环回失败（{exc!r}）—— 继续用当前来源")

    def _on_pcm(self, pcm) -> None:
        """采集线程回调：喂给检测器，命中的丢进队列（主循环再取，避免跨线程动状态）。

        另外每 10 秒打一行**采集电平**：2026-09-17/18 两次实机里指纹检测器零命中，
        而 CT 门控（两次都是 CT）与算法（落盘音频上 4/5 召回）都已排除 ⇒
        必须分清"采集哑了"还是"检测没命中"。RMS 恒为 0 = 采集没在送音频。
        """
        det = self._detector
        if det is None:
            return
        try:
            import numpy as _np
            arr = _np.asarray(pcm, dtype=_np.float32)
            if arr.size:
                self._audio_rms_acc = getattr(self, "_audio_rms_acc", 0.0) + float((arr ** 2).sum())
                self._audio_rms_n = getattr(self, "_audio_rms_n", 0) + int(arr.size)
                now = time.monotonic()
                if now - getattr(self, "_audio_rms_last", 0.0) >= 10.0:
                    n = self._audio_rms_n
                    rms = (self._audio_rms_acc / n) ** 0.5 if n else 0.0
                    self._audio_rms_acc, self._audio_rms_n = 0.0, 0
                    self._audio_rms_last = now
                    sc = getattr(det, "last_scores", None)
                    extra = (f"  指纹最高命中={sc[0]:.0f} need={sc[1]:.0f} 基线={sc[2]:.1f}"
                             if sc else "")
                    self.log.write("AUDIO", f"音频电平 RMS={rms:.5f}（最近 10s）"
                                            f"{'  <== 哑的！采集没在送音频' if rms < 0.0005 else ''}"
                                            f"{extra}"
                                            f"  来源 {getattr(self._audio, 'description', '-')}")
                    # 每 30 秒（= 每 3 行电平）把**实时音频**也存一段：即使没有任何候选，
                    # 也能拿实战音频跟参考指纹离线对一遍（用户 2026-09-18 选的 C）。
                    self._audio_snap_n = getattr(self, "_audio_snap_n", 0) + 1
                    if self._audio_snap_n % 3 == 0:
                        try:
                            import wave as _wave
                            from .audio.plant import ANALYSIS_RATE as _RATE
                            w = det.pcm_window(det.stream_time, 6.0)
                            if w is not None and len(w):
                                out = AUDIO_DUMP_DIR / f"live_{time.strftime('%Y%m%d_%H%M%S')}.wav"
                                out.parent.mkdir(parents=True, exist_ok=True)
                                fh = _wave.open(str(out), "wb")
                                fh.setnchannels(1); fh.setsampwidth(2); fh.setframerate(_RATE)
                                frames = (_np.clip(w, -1, 1) * 32767).astype(_np.int16).tobytes()
                                fh.writeframes(frames)
                                fh.close()
                                self.log.write("AUDIO", f"  实时片段已存：{out.name}（6s）")
                                _prune_dumps("live_*.wav", KEEP_LIVE_DUMPS)
                        except Exception as exc:             # noqa: BLE001 存档失败不影响运行
                            self.log.write("AUDIO", f"  [!] 实时片段存档失败：{exc!r}")
            for hit in det.feed(pcm):
                self._audio_hits.put((det.stream_time, hit))
        except Exception as exc:                         # noqa: BLE001 采集线程不能把主程序带崩
            self.log.write("AUDIO", f"[!] 检测异常：{exc!r}")

    def _drain_audio(self, now: float) -> None:
        """主循环里把音频命中取出来处理（此时已持有状态锁）。"""
        while True:
            try:
                stream_t, hit = self._audio_hits.get_nowait()
            except queue.Empty:
                return
            # 音频流时间 -> 单调时钟：命中时刻 = 现在 − 流里已经过去的时间；
            # 再加"音频链路偏置"（`onset_bias_sec`，>0 = 归零更晚）。
            # 参考指纹里只烧了"裁剪起点 → 播报起点"的几何值（0.40 秒），
            # "播报起点 → 真实安放"这段（含采集延迟）是产品常数，放在配置里好调。
            plant_mono = (now - max(0.0, self._detector.stream_time - hit.t_stream)
                          + self.cfg.audio.onset_bias_sec
                          + tuning.offset())          # 自成长的自动修正量（见 tuning.py）
            self._handle_audio_hit(plant_mono, hit, now)

    def _handle_audio_hit(self, plant_mono: float, hit, now: float) -> None:
        """门控：只剩两条数据逼出来的条件，**阵营门控 2026-09-19 已取消**。

        原来只给 CT 方用，理由是"T 方自己下包本来就准"。用户实机复核发现那只覆盖了
        "自己下包"：**T 方队友下包时，变的是队友的背包，你自己那一包数据没有任何变化**，
        锚点一样退到常数补偿、一样有 ±1 秒误差。所以音频对两队都开。
        （"自己下包"那种局锚点本来就准到 0.02 秒，由 `state.feed_audio_plant` 里
        "两者一致就别动"那条保护兜住，不会被音频覆盖钝掉。）

        剩下这两条是数据逼出来的（三份录音里原始误报全部落在 live 之外）：
        回合必须 live、且 GSI 还没揭示过安放。

        ⚠️ 这里**不碰时钟**：只把命中挂成"待确认候选"。用户 2026-09-17 拍板
        "确定不是误报再显示倒计时，从多少秒开始计时都行" —— 所以等 GSI 揭示包
        （Valve 自己的信号，只是晚 0.03~2.6 秒）来确认，确认后把 T0 校准到播报时刻。
        没有确认 ⇒ 候选过期丢弃，屏幕上一闪都不会有 ⇒ 误报无害。
        """
        if self.state.round_phase != "live":
            return                                  # 主菜单/回合结束/买枪阶段的播报一律不算
        if hit.score < self.cfg.audio.threshold:
            self.log.write("AUDIO", f"播报命中但分数不足（{hit.score:.2f} < "
                                    f"{self.cfg.audio.threshold:.2f}，参考={hit.name}）—— 忽略")
            return
        self._dump_audio(hit, now)
        prev = self._pending_audio
        if prev is None or hit.score >= prev[1]:
            self._pending_audio = (plant_mono, hit.score, hit.name, now)
        self.log.write("AUDIO", f"播报候选（分数={hit.score:.2f} 参考={hit.name} "
                                f"滞后 {now - plant_mono:.2f}s）—— 等 GSI 揭示包确认后才采用")
        # 2026-09-18 修（实机数据逼出来的）：指纹要等整句播报听完才够分，实测检测滞后
        # 1.8~3.7 秒，经常**晚于** GSI 揭示包 0.35~0.93 秒。而确认原来只发生在
        # "揭示包到达的那一 tick"里 ⇒ 迟到候选永远等不到，6 秒后被丢弃。
        # 实测三次安放：分数 25 / 17 的那两次就是这样白丢的，只有 16 的那次赶上。
        # 现在：本局揭示包已经来过（仍是已安放状态）=> 当场确认。
        if self._last_reveal_mono is not None:
            self._check_audio_anchor(now, self._last_reveal_mono)

    def _dump_audio(self, hit, now: float) -> None:
        """把命中前后的音频落盘（实战音频是重标阈值的唯一依据）。

        为什么要它：离线三份录音里真身 0.82~0.88、误报 ≤0.76，实战却变成
        真身 0.70~0.75 / 误报 0.70~0.73（重叠）—— 环境不一样，必须用**实战音频**重标。
        """
        det = self._detector
        if det is None:
            return
        try:
            # 以"命中时刻 + 固定尾巴"结尾（不要用"处理这一刻"，否则每局位置都不同）
            pcm = None
            if hasattr(det, "pcm_window"):
                pcm = det.pcm_window(hit.t_stream + AUDIO_DUMP_TAIL_SEC, AUDIO_DUMP_SEC)
            if pcm is None:
                pcm = det.recent_pcm(AUDIO_DUMP_SEC)
            if pcm is None or len(pcm) == 0:
                return
            import wave
            stamp = time.strftime('%Y%m%d_%H%M%S')
            out = AUDIO_DUMP_DIR / f"{stamp}_{hit.score:.2f}_{Path(hit.name).stem}.wav"
            out.parent.mkdir(parents=True, exist_ok=True)
            w = wave.open(str(out), "wb")
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(self._analysis_rate)
            import numpy as _np
            w.writeframes((_np.clip(pcm, -1, 1) * 32767).astype(_np.int16).tobytes())
            w.close()
            self.log.write("AUDIO", f"  音频片段已存：{out.name}")
        except Exception as exc:                     # noqa: BLE001 落盘失败不影响运行
            self.log.write("AUDIO", f"  [!] 音频落盘失败：{exc!r}")

    def _check_audio_anchor(self, now: float, reveal_mono: float | None) -> None:
        """候选的确认与过期：揭示包到了就用它校准 T0；太久没到就丢弃（不显示过任何东西）。"""
        pend = self._pending_audio
        if pend is None:
            return
        plant_mono, score, name, decided = pend
        if reveal_mono is not None:
            # 用状态机既有的"修正 T0"路径（它自带合理性窗口 −1.0~+3.5 秒）
            before = self.state.planted_mono
            res = self.state.feed_audio_plant(plant_mono, reveal_mono, score=score, name=name)
            if res == "refined":
                refstats.record(name, score)     # 只有"被采纳"的才算参考的实战成绩
                self.log.write("AUDIO", f"确认：GSI 揭示包到了，T0 校准到播报时刻"
                                        f"（比 GSI 锚点早 {reveal_mono - plant_mono:+.2f}s，"
                                        f"分数={score:.2f} 参考={name}）")
            elif res == "kept":
                # 2026-09-19 加：播报与锚点本来就一致 ⇒ 锚点走的是"认出安放触发包"那条
                # 准路（0.02 秒级），保留它、别用音频覆盖（音频 ±0.05~0.1 秒，换上去更钝）。
                # 这也是一条**确认过的真命中**，所以照样计入参考的实战成绩。
                # ⚠️ 这里报的差必须是**决定 kept 的那个差**（T0 − 播报时刻），
                # 不能拿 `reveal_mono - plant_mono`（那是 GSI 揭示包自己的延迟）——
                # 实机日志里出现过"一致（差 +0.83s）"这种自相矛盾的行（2026-09-19 修）。
                gap = (before - plant_mono) if before is not None else 0.0
                refstats.record(name, score)
                self.log.write("AUDIO", f"确认：播报与 GSI 锚点一致（差 {gap:+.2f}s）"
                                        f"—— 锚点本来就准，T0 保持不动"
                                        f"（分数={score:.2f} 参考={name}）")
            else:
                self.log.write("AUDIO", f"候选被否：与 GSI 锚点相差超出允许范围"
                                        f"（分数={score:.2f} 参考={name}）")
            self._pending_audio = None
        elif now - decided > AUDIO_PENDING_SEC:
            self.log.write("AUDIO", f"候选过期丢弃（{now - decided:.1f}s 内没等到 GSI 揭示包，"
                                    f"分数={score:.2f} 参考={name}）—— 期间不显示任何东西")
            self._pending_audio = None

    def _tick_loop(self) -> None:
        last_status = time.monotonic()
        prev_state = None
        # 截止时刻驱动，而不是 sleep(0.02)：后者会把"每帧的工作耗时"累加进去，
        # 实际周期变成 20ms + 工作耗时 且不断漂移，读秒就会一阵一阵地跳。
        next_t = time.monotonic()
        while not self._stop.is_set():
            next_t += TICK_SECONDS
            now = time.monotonic()
            with self._lock:
                self._retry_process_loopback(now)
                self._drain_audio(now)        # 音频命中（已按 CT + live 门控）
                # 揭示包 +2.5 秒时落盘 10 秒（含整句播报）—— 让每一局都留下可用的参考素材
                at = getattr(self, "_plant_dump_at", None)
                if at is not None and now >= at and self._detector is not None:
                    self._plant_dump_at = None
                    try:
                        import wave as _wave
                        import numpy as _np2
                        from .audio.plant import ANALYSIS_RATE as _RATE2
                        end_stream = self._detector.stream_time - (now - at)
                        seg = self._detector.pcm_window(end_stream, PLANT_DUMP_SEC)
                        if seg is not None and len(seg):
                            out = AUDIO_DUMP_DIR / f"plant_{time.strftime('%Y%m%d_%H%M%S')}.wav"
                            out.parent.mkdir(parents=True, exist_ok=True)
                            fh = _wave.open(str(out), "wb")
                            fh.setnchannels(1); fh.setsampwidth(2); fh.setframerate(_RATE2)
                            frames = (_np2.clip(seg, -1, 1) * 32767).astype(_np2.int16).tobytes()
                            fh.writeframes(frames)
                            fh.close()
                            self.log.write("AUDIO", f"  安放片段已存：{out.name}"
                                                    f"（{PLANT_DUMP_SEC:.0f}s，含整句播报）")
                            gone = _prune_dumps("plant_*.wav", KEEP_PLANT_DUMPS)
                            if gone:
                                self.log.write("AUDIO", f"  素材到上限，已清掉最旧的 {gone} 个"
                                                        f"（只留最近 {KEEP_PLANT_DUMPS} 个）")
                    except Exception as exc:                 # noqa: BLE001 落盘失败不影响运行
                        self.log.write("AUDIO", f"  [!] 安放片段落盘失败：{exc!r}")
                self._check_audio_anchor(now, self._reveal_mono)
                self._reveal_mono = None
                self.state.tick(now)
                self._drain_events()          # 状态机内部事件写进日志（诊断用）
                snapshot = self.state.snapshot(now)
                view = render.build(snapshot, self.cfg.ui.show_idle, self.debug,
                                    self.cfg.ui.decimal_below_sec)

                if snapshot.state != prev_state:
                    # exp 的两种写法（未知 = " -"）提成变量：原来挤在一行 f-string 里 148 字符
                    exp_text = (" -" if snapshot.explosion_remaining is None
                                else format(snapshot.explosion_remaining, ".1f"))
                    self.log.write("STATE", f"{prev_state or '-'} -> {snapshot.state}"
                                            f"  {snapshot.status_text}"
                                            f"  exp={exp_text}"
                                            f"  src={snapshot.source}")
                    prev_state = snapshot.state

                if self.hud is not None:
                    self.hud.set_view(view)
                elif self.debug and now - last_status >= 5.0:
                    last_status = now
                    print(f"  {self.stats.line()}  状态={snapshot.state}", flush=True)
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()      # 落后太多就重新对齐，别补帧补到爆

    def run(self, seconds: float | None = None) -> int:
        self.start()
        print(f"c4timer 运行中  GSI http://{self.cfg.gsi.host}:{self.cfg.gsi.port}/",
              flush=True)
        # 把日志路径打出来：实机排查时"日志在哪"是第一个要回答的问题
        # （2026-09-16：用户报了三轮现象，我们却找不到任何日志 —— 因为日志文件被设成了只读）
        print((f"  日志: {self.log.path}" if self.log.enabled
               else f"  [!] 日志没开：{self.log.error}"), flush=True)
        if seconds:
            print(f"  （{seconds:.0f} 秒后自动退出 —— 自检模式）")

        if self.hud is not None:
            if seconds:
                self.hud.root.after(int(seconds * 1000), self._finish)
            else:
                print("  按 Ctrl+C 退出。")
            try:
                self.hud.run()
            except KeyboardInterrupt:
                pass
        else:
            if not seconds:
                print("  按 Ctrl+C 退出。")
            try:
                deadline = (time.monotonic() + seconds) if seconds else None
                while not self._stop.is_set():
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)
            except KeyboardInterrupt:
                pass
        self.stop()
        print("\n" + self.stats.line())
        return 0

    def _finish(self) -> None:
        self._stop.set()
        if self.hud is not None:
            self.hud.stop()

    def _stop_audio(self) -> None:
        cap, self._audio = self._audio, None
        if cap is not None:
            try:
                cap.stop()
            except Exception:                        # noqa: BLE001 收尾不该抛
                pass

    def stop(self) -> None:
        self._stop_audio()
        self._stop.set()
        try:
            self.receiver.stop()
        except Exception:
            pass
        if self._ticker is not None:
            self._ticker.join(timeout=1.0)
            self._ticker = None
        if self.hud is not None:
            try:
                self.hud.stop()
            except Exception:
                pass
            self.hud = None
        self.single.release()
        raise_timer_resolution(False)      # 还原系统定时器精度
        self.log.close()

    # ------------------------------------------------------------------

    def make_hud(self, click_through: bool | None = None):
        """必须在主线程调用（tkinter 的硬约束）。

        `click_through=False` = 编辑模式：窗口不再穿透，于是可以拖动定位，
        松手时把位置写进 config.json。正常运行时是穿透的、拖不动 ——
        穿透与拖动天生冲突（穿透后窗口收不到鼠标事件）。
        """
        if not self.enable_hud or not self.cfg.ui.enabled:
            return None
        from .hud import Hud
        self.hud = Hud(self.cfg, click_through=click_through)
        return self.hud
