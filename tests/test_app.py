"""P3 自检：应用层（真实报文 -> 回调 -> 状态机 -> 日志/显示）。

不需要游戏：把 210 个**真实 GSI 报文**按原始时间轴灌进
`app._on_gsi`，验证"回调 + 锁 + 状态机 + 渲染 + 标定日志"整条链路。

需求变更后原来的 [1][2][2b][3][4][5] 整块删掉了 —— 它们测的是
"音频检测 -> 门控 -> 绿字/DEFUSING"那条链路，而拆弹声判定与绿字已从产品里移除。
[6b][6c]（采集对象、`_on_pcm`、爆炸音效接线）也删掉了：那代音频链路 2026-09-16 移除。
⚠️ 但音频在 2026-09-17 以**指纹方案**回归了产品（`audio/plant.py` + `audio/fingerprint.py`），
本次自检的第 [12] 节覆盖它（合成"播报"必须命中、纯噪声不报、指纹路径的 stream_time 等）。
保留下来的 [6][7][8][9][9b][10][11] 按"GSI + 音频两条链路"的新语义适配。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from dataclasses import replace
from pathlib import Path

from c4timer.app import C4TimerApp
from c4timer.config import Config
from c4timer.gsi import GsiSample
from c4timer.state import ARMED, DEFUSED, EXPLODED, IDLE

from .checks import Checks

ROOT = Path(__file__).resolve().parent.parent

_BASE = GsiSample(
    recv_mono=0.0, recv_wall="", observer=False,
    observed_steamid="me", self_steamid="me", observing_other=False,
    round_phase="live", map_round=1, map_name="de_dust2", map_mode="competitive",
    bomb_state=None, bomb_state_raw=None, round_bomb=None,
    explosion_remaining=None, defuse_remaining=None, pcd_phase=None, pcd_ends_in=None,
    bomb_countdown=None, bomb_player=None, defuser_kit=None,
    top_keys=(), allplayers_count=0,
)


def _make_app() -> C4TimerApp:
    """建一个不碰设备、不落盘、不开窗的 app。"""
    cfg = Config()
    cfg.debug.print_events = False
    cfg.debug.print_gsi = False
    cfg.debug.no_log = True          # 自检不要污染 runtime/c4timer.log
    cfg.audio.enabled = False        # 自检不去开真实音频采集
    return C4TimerApp(cfg, enable_hud=False, debug=False)


class _Capture:
    """假日志：只记下写了什么，不落盘。

    放在模块级（2026-09-19 从 run() 里提出来）：音频那一节的用例分布在两处，
    各自定义一份会重复，而其中一处又早于原来的定义位置（差点写成 NameError）。
    """

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def write(self, tag: str, text: str) -> None:
        self.lines.append((tag, text))


def run(verbose: bool = True) -> tuple[int, int, list[str]]:
    c = Checks()
    if verbose:
        print("=" * 72)
        print("P3 自检：应用层（真实报文 -> 回调 -> 状态机 -> 日志）")
        print("=" * 72)

    # ================================================================
    # 注意顺序：真实报文回放必须排在 HUD 测试**之前**。
    # 测试里创建并销毁 Tk 根窗口之后再跑大循环会导致解释器硬退出；
    # 这是脚手架的副作用，不是产品问题（产品只创建一次悬浮窗，从不销毁重建）。
    if verbose:
        print("\n[6] 应用层：210 个真实报文灌进 app._on_gsi（走真实回调与锁）")
    from c4timer.fixtures import DEFAULT_FIXTURE, load_captures
    from c4timer.gsi import parse_payload
    from c4timer.render import build

    app_replay = _make_app()
    seen: set[str] = set()
    offset = 0.0
    view_rows_by_state: dict[str, set[int]] = {}
    for cap in load_captures(DEFAULT_FIXTURE):
        for rec in cap.records:
            body = rec.get("body")
            if not isinstance(body, dict):
                continue
            t = offset + float(rec.get("elapsed", 0.0))
            app_replay._on_gsi(parse_payload(body, t, rec.get("wall", "")))
            app_replay.state.tick(t)
            state = app_replay.state.state
            seen.add(state)
            rows = len(build(app_replay.state.snapshot(t)).rows)
            view_rows_by_state.setdefault(state, set()).add(rows)
        offset += cap.duration + 5.0

    c.eq(app_replay.stats.gsi_packets, 210, "app 的接收计数 = 210（全部真实包都进了回调）")
    for state in (IDLE, ARMED, DEFUSED, EXPLODED):
        c.ok(state in seen, f"app 在真实报文上到达过 {state}")
    c.eq(view_rows_by_state.get(IDLE), {0}, "IDLE 时恒为 0 行")
    c.eq(view_rows_by_state.get(ARMED), {1}, "ARMED 时恒为 1 行（只有红字）")
    c.ok(all(rows <= 1 for rs in view_rows_by_state.values() for rows in rs),
         f"任何状态都不超过 1 行（绿字已删），实际 {view_rows_by_state}")
    # 音频这条路已经整条移除，所以**永远**不该出现第二个倒计时状态
    c.ok("defusing" not in seen, "没有任何包能把状态推到已删除的 defusing")

    # ================================================================
    if verbose:
        print("\n[7] 单实例保护（命名互斥体，不是锁文件）")
    from c4timer.single import SingleInstance
    name = f"Local\\c4timer-selftest-{os.getpid()}"
    first, second = SingleInstance(name), SingleInstance(name)
    c.ok(first.acquire(), "第一个实例拿到互斥体")
    c.ok(not second.acquire(), "第二个实例被拒绝")
    c.ok(second.already_running, "第二个实例记下了 already_running")
    first.release()
    third = SingleInstance(name)
    c.ok(third.acquire(), "释放后能再次获取（不会留下'永远被占用'的假锁）")
    third.release()

    # ================================================================
    if verbose:
        print("\n[8] CS2 配置体检（只读，不写游戏目录）")
    from c4timer.cs2path import OUR_CFG_NAME, diagnose
    diag = diagnose()
    c.ok(len(diag.cfg_dirs) >= 1, f"找到 CS2 cfg 目录（{len(diag.cfg_dirs)} 个）")
    c.ok(len(diag.ours) >= 1, f"找到本程序的 {OUR_CFG_NAME}")
    if diag.ours:
        info = diag.ours[0]
        c.ok(info.has_bomb, "cfg 请求了 bomb 组件")
        c.ok(info.has_phase_countdowns, "cfg 请求了 phase_countdowns 组件")
        c.eq(info.blockers, [], "cfg 没有阻塞性问题")
        c.eq(info.port, 30077, "cfg 端口与程序默认端口一致")
        c.ok("BOM" not in info.uri, "cfg 没有 UTF8-BOM（带 BOM 游戏不会加载）")

    # 仓库里的 GSI 模板必须和代码里的常量/默认端口一致 —— 否则新用户装上去收不到炸弹信息。
    # 这三条是给"交付给别人用"上的护栏（模板改名、端口改动、少请求组件都会在这里变红）。
    tmpl = ROOT / "gsi" / OUR_CFG_NAME
    c.ok(tmpl.exists(), f"仓库里带着 GSI 模板（gsi/{OUR_CFG_NAME}）")
    if tmpl.exists():
        raw = tmpl.read_text(encoding="utf-8")
        c.ok('"uri" "http://127.0.0.1:30077/"' in raw, "模板的 uri 与默认端口 30077 一致")
        for comp in ("bomb", "phase_countdowns", "allplayers_state", "player_state"):
            c.ok(f'"{comp}"' in raw, f"模板请求了 {comp} 组件")
        c.ok(not raw.startswith("\ufeff"), "模板没有 BOM")

    # ================================================================
    if verbose:
        print("\n[9] HUD 编辑模式：click_through=False 必须真正清掉 WS_EX_TRANSPARENT")
    try:
        import ctypes
        from ctypes import wintypes

        from c4timer.hud import GWL_EXSTYLE, WS_EX_TRANSPARENT, Hud, _get_long

        cfg_hud = Config()
        cfg_hud.ui.show_idle = True

        h1 = Hud(cfg_hud, click_through=True)
        h1.root.update()
        c.ok(h1._hwnd is not None, "穿透模式拿到了窗口句柄")
        s1 = _get_long(wintypes.HWND(h1._hwnd), GWL_EXSTYLE) if h1._hwnd else 0
        c.ok(bool(s1 & WS_EX_TRANSPARENT), "穿透模式：WS_EX_TRANSPARENT 已设置")
        h1.stop()

        h2 = Hud(cfg_hud, click_through=False)
        h2.root.update()
        s2 = _get_long(wintypes.HWND(h2._hwnd), GWL_EXSTYLE) if h2._hwnd else 0
        c.ok(not (s2 & WS_EX_TRANSPARENT),
             "编辑模式：WS_EX_TRANSPARENT 已清除（所以能拖动）")
        c.ok(bool(s2 & 0x00080000), "编辑模式仍然保持 WS_EX_LAYERED（透明不丢）")
        h2.stop()

        # ---- 读秒卡顿的修法：内容没变就不许重排窗口 ----
        # 老实现每次 poll 都 configure + update_idletasks + geometry，等于每 20ms
        # 重排并搬一次窗口，读秒就一顿一顿的。
        if verbose:
            print("\n[9b] HUD 只在内容变化时重排窗口")
        from c4timer.render import ROW_LABEL, Row, View
        h3 = Hud(cfg_hud, click_through=True)
        h3.root.update()
        c.ok(h3._hwnd is not None, "测试窗口建起来了")
        v1 = View(visible=True, status="C4 已安放", status_color="#FFFFFF",
                  rows=(Row(ROW_LABEL, "12.3", "#FF3B30", big=True),))
        h3._apply(v1)
        first = h3.applies
        c.ge(first, 1, "第一次必须真的画上去")
        for _ in range(25):
            h3._apply(v1)
        c.eq(h3.applies, first, "内容完全一样时 25 次调用一次都不重排")
        v2 = View(visible=True, status="C4 已安放", status_color="#FFFFFF",
                  rows=(Row(ROW_LABEL, "12.2", "#FF3B30", big=True),))
        h3._apply(v2)
        c.eq(h3.applies, first + 1, "数值变了才重排一次")
        h3.stop()

        # ---- 面板样式：尺寸固定 / 深底上的主题色 / 位置拉回屏幕内 ----
        # 这几条是"改样式最容易悄悄改坏"的地方，而样式原本一条测试都没有。
        if verbose:
            print("\n[9c] HUD 面板：尺寸不随数字抽动、深底用白字、越界位置拉回")
        from c4timer.hud import _VALUE_ON_PANEL
        from c4timer.render import GRAY, WHITE
        h4 = Hud(cfg_hud, click_through=True)
        h4.root.update()
        v37 = View(visible=True, status="C4 已安放", status_color=WHITE,
                   rows=(Row(ROW_LABEL, "37", "#FF3B30", big=True),))
        h4._apply(v37)
        panel = (h4._panel_w, h4._panel_h)
        value_item = h4._items["value"]
        c.ge(panel[0], h4.f_status.measure("C4 已安放 · 未看到安放时刻")
             + 2 * h4.pad_x + 2 * h4.bar_w,
             "面板宽度按最长状态文案预留（够放「C4 已安放 · 未看到安放时刻」）")
        c.eq(h4.canvas.itemcget(value_item, "fill"), WHITE, "深底上的数值画成白字")
        v99 = View(visible=True, status="C4 已安放", status_color=WHITE,
                   rows=(Row(ROW_LABEL, "9.9", "#FF3B30", big=True),))
        h4._apply(v99)
        c.eq((h4._panel_w, h4._panel_h), panel, "数字从 37 变成 9.9：面板尺寸一个像素都不动")
        c.eq(h4.canvas.itemcget(value_item, "text"), f"{ROW_LABEL} 9.9", "数值行文字换了")
        v_zero = View(visible=True, status="C4 已安放", status_color=WHITE,
                      rows=(Row(ROW_LABEL, "0.0", GRAY, big=True),))
        h4._apply(v_zero)
        c.eq(h4.canvas.itemcget(value_item, "fill"), _VALUE_ON_PANEL[GRAY],
             "灰字压在深底上会被提亮（否则 0.0 / -- 会糊）")
        # 文案比预留还长时临时撑开，收回去要能回到预留宽度（预留宽度是地板）
        long_view = View(visible=True, status="C4 已安放 · 未看到安放时刻 · GSI 断连",
                         status_color=WHITE, rows=(Row(ROW_LABEL, "0.0", GRAY, big=True),))
        h4._apply(long_view)
        c.ok(h4._panel_w > panel[0], "超长文案（状态行叠加 GSI 断连）会把面板临时撑开")
        h4._apply(v99)
        c.eq((h4._panel_w, h4._panel_h), panel, "文案收回去后回到预留宽度（不永久变宽）")
        sw, sh = h4.root.winfo_screenwidth(), h4.root.winfo_screenheight()
        c.eq(h4._clamp(-9999, -9999, panel[0], panel[1]), (0, 0),
             "屏幕外的位置被拉回左上角")
        c.eq(h4._clamp(sw + 500, sh + 500, panel[0], panel[1]),
             (sw - panel[0], sh - panel[1]), "右下越界被拉回屏幕内")
        h4.stop()
    except Exception as exc:
        c.ok(False, f"HUD 编辑模式测试异常: {exc!r}")

    # ================================================================
    if verbose:
        print("\n[10] 配置读写：set_value 的类型转换与错误处理（纯函数，不碰用户的 config.json）")
    from c4timer import config as cfgmod
    probe = Config()
    cfgmod.set_value(probe, "ui.font_size", "44")
    c.eq(probe.ui.font_size, 44, "int 字段被转成 int")
    cfgmod.set_value(probe, "ui.scale", "1.3")
    c.eq(probe.ui.scale, 1.3, "float 字段被转成 float")
    cfgmod.set_value(probe, "ui.show_idle", "true")
    c.eq(probe.ui.show_idle, True, "bool 字段认 true")
    cfgmod.set_value(probe, "ui.show_idle", "false")
    c.eq(probe.ui.show_idle, False, "bool 字段认 false")
    # 新增配置项必须能被 set_value 改到
    cfgmod.set_value(probe, "timers.local_bang_grace_sec", "0.4")
    c.eq(probe.timers.local_bang_grace_sec, 0.4, "新增的 local_bang_grace_sec 可配置")
    cfgmod.set_value(probe, "ui.decimal_below_sec", "20")
    c.eq(probe.ui.decimal_below_sec, 20.0, "新增的 decimal_below_sec 可配置")
    cfgmod.set_value(probe, "gsi.stale_after_sec", "18")
    c.eq(probe.gsi.stale_after_sec, 18.0, "gsi.stale_after_sec 可配置（GSI 断连阈值）")
    cfgmod.set_value(probe, "timers.plant_latency_compensation", "1.7")
    c.eq(probe.timers.plant_latency_compensation, 1.7, "安放补偿值可配置（>0 表示归零提前）")
    # 音频段（2026-09-17 重新接入）：阈值/开关都要能配；段里没有的字段仍要报错
    cfgmod.set_value(probe, "audio.threshold", "0.8")
    c.eq(probe.audio.threshold, 0.8, "音频匹配阈值可配置")
    # 音频链路偏置（2026-09-18 从"烧进参考指纹"改成产品配置）：调时间不该需要重建参考
    cfgmod.set_value(probe, "audio.onset_bias_sec", "0.8")
    c.eq(probe.audio.onset_bias_sec, 0.8, "音频链路偏置可配置（>0 = 归零更晚）")
    # 指纹命中数阈值（2026-09-18 接成配置）：它是"漏检 vs 误触发"的旋钮，改它不该改代码
    cfgmod.set_value(probe, "audio.min_hits", "20")
    c.eq(probe.audio.min_hits, 20, "指纹命中数阈值可配置（int 转换）")
    cfgmod.set_value(probe, "audio.enabled", "false")
    c.eq(probe.audio.enabled, False, "音频开关可配置（false 表示完全不启用）")
    for bad, label in ((("nosuch", "1"), "不存在的配置段"),
                       (("ui.nosuch", "1"), "不存在的字段"),
                       (("font_size", "1"), "缺少段名"),
                       (("timers.defuse_no_kit", "5"), "已删除的拆弹配置字段"),
                       (("audio.nosuch", "1"), "音频段里不存在的字段")):
        try:
            cfgmod.set_value(probe, *bad)
            c.ok(False, f"{label}应当报错")
        except ValueError:
            c.ok(True, f"{label}正确报错")

        # ---- 音频检测器（2026-09-17 加，P3）：合成信号上必须命中、纯噪声上必须不报 ----
    # 用合成信号而不是真实录音：仓库里不放游戏音频（模板由用户本地标定生成）。
    # 这里验证的是**管线**：FFT 打分口径、滑窗位置换算、在线分块与离线一致、阈值门控。
    if verbose:
        print("\n[12] 音频检测器：合成'播报'必须命中、纯噪声必须不报")
    import numpy as np          # 音频链路的依赖（产品音频功能同样需要它）
    from c4timer.audio import plant as P
    from c4timer.audio import fingerprint as fp_mod
    rng = np.random.default_rng(7)

    def fake_voice(dur: float) -> np.ndarray:
        """合成一段"像人声"的信号：几个音调 + 包络 + 起伏。
        为什么不用白噪声：白噪声的频带能量在时间上几乎是平的，标准化后相关没有峰，
        没法验证打分口径（实测命中 0）。"""
        n = int(dur * P.ANALYSIS_RATE)
        t = np.arange(n) / P.ANALYSIS_RATE
        sig = (np.sin(2 * np.pi * 220 * t) + 0.6 * np.sin(2 * np.pi * 660 * t)
               + 0.4 * np.sin(2 * np.pi * 1320 * t) * (1 + np.sin(2 * np.pi * 4 * t)))
        env = np.hanning(n) ** 0.5
        return (sig * env * 0.3).astype(np.float32)

    voice = fake_voice(0.4)
    tmpl_feats = P.band_features(voice)
    tmpl = P.VoiceTemplate(name="synthetic", feats=tmpl_feats, onset_sec=0.0, source="self-test")
    quiet = rng.standard_normal(int(3.0 * P.ANALYSIS_RATE)).astype(np.float32) * 0.02
    stream = np.concatenate([quiet, voice, quiet])
    hits = P.scan_pcm(stream, [tmpl], threshold=0.7, template_sec=0.4)
    c.eq(len(hits), 1, "合成流里恰好命中一次")
    if hits:
        c.ok(abs(hits[0].t_stream - 3.0) <= 0.15,
             f"命中位置 = 播报起点（±0.15s）—— 实际 {hits[0].t_stream:.3f}s")
    noise = rng.standard_normal(int(3.0 * P.ANALYSIS_RATE)).astype(np.float32) * 0.3
    c.eq(P.scan_pcm(noise, [tmpl], threshold=0.7, template_sec=0.4), [],
         "纯噪声不报（阈值 0.7 下零命中）")
    d = P.PlantVoiceDetector([tmpl], threshold=0.7, template_sec=0.4)
    c.eq(d.active, True, "有模板时检测器启用")
    out = []
    step = int(0.25 * P.ANALYSIS_RATE)
    for s in range(0, len(stream), step):
        out += d.feed(stream[s:s + step])
    c.eq(len(out), 1, "在线分块喂入得到同样的唯一命中（与离线一致）")
    if out:
        c.ok(abs(out[0].t_stream - 3.0) <= 0.25,
             f"在线命中的流时间也落在播报起点附近 —— 实际 {out[0].t_stream:.3f}s")
    c.eq(P.PlantVoiceDetector([], threshold=0.7).active, False, "没有模板 -> 检测器不启用")
    c.eq(P.load_templates("no/such/dir"), [], "模板目录不存在 -> 空模板、不报错")

    # ---- 自成长：累积统计的门槛、方向与限幅（2026-09-18 加）----
    # 为什么单测它：这段逻辑会**自动改常数**，一旦方向反了或没限幅，会让所有局的计时整体偏掉。
    # ⚠️ 必须把它写到临时文件：否则自检会污染用户真实累积的 runtime/calibration_stats.json
    #    （实测踩过：跑一次自检就把统计写成 58 局、自动量顶到 -0.80s）。
    import tempfile
    from c4timer import tuning
    _real_stats = tuning.STATS_PATH
    _tmp_stats = Path(tempfile.gettempdir()) / "c4timer_selftest_stats.json"
    tuning.STATS_PATH = _tmp_stats
    _tmp_stats.unlink(missing_ok=True)
    try:
        tuning.reset_memory()
        c.eq(tuning.offset(), 0.0, "没有观测时自动修正量为 0（不影响原有行为）")
        for _ in range(tuning.MIN_SAMPLES - 1):
            tuning.observe(0.5, 0.3, "gsi+comp+audio")
        c.eq(tuning.offset(), 0.0, f"不足 {tuning.MIN_SAMPLES} 局时不生效")
        c.eq(tuning.observe(0.5, 5.0, "gsi+comp+audio"), None, "区间过宽的观测不计入（稀疏包局）")
        tuning.observe(0.5, 0.3, "gsi+comp+audio")
        c.ok(tuning.offset() < 0, "达到门槛后开始修正，方向是'把偏晚改早'（负值）")
        c.ok(abs(tuning.offset()) <= tuning.CLAMP + 1e-9, "累计修正被夹在上限内")
        c.ok(abs(tuning.offset()) <= tuning.MAX_STEP + 1e-9, "单次调整不超过单次上限")
        for _ in range(50):
            tuning.observe(1.5, 0.2, "gsi+comp+audio")     # 一直偏晚很多
        c.ok(abs(tuning.offset()) <= tuning.CLAMP + 1e-9, "连续偏晚也不会突破累计上限")
        tuning.reset_memory()
        c.ok(tuning.summary().startswith("自成长统计"), "摘要能生成（启动日志与体检都打它）")
    finally:
        tuning.STATS_PATH = _real_stats
        _tmp_stats.unlink(missing_ok=True)
        tuning.reset_memory()

    # ---- 参考实战统计：下滑判定必须真的判得出来（2026-09-18 加）----
    # 踩过的坑：原先按"每个参考最后命中时间"排序，同一秒内的多次命中分不出先后，
    # 演习里因此没判出下滑。现在按全局递增序号取最近 n 条。
    import tempfile as _tf
    from c4timer import refstats as _rs
    _real_stats = _rs.STATS_PATH
    _rs.STATS_PATH = Path(_tf.gettempdir()) / "c4timer_selftest_refstats.json"
    _rs.STATS_PATH.unlink(missing_ok=True)
    try:
        _rs.reset_memory()
        for _ in range(_rs.DEGRADE_SAMPLES):
            _rs.record("clip_A.wav", 40.0)
        _rs.update_baseline()
        c.eq(_rs.fired("clip_A.wav"), _rs.DEGRADE_SAMPLES, "被采纳次数按来源片段名累加")
        for _ in range(_rs.DEGRADE_SAMPLES):
            _rs.record("clip_B.wav", 12.0)
        _degraded, _cur, _base = _rs.degraded()
        c.ok(_degraded, f"分数整体下滑能判出来（近期 {_cur} vs 基线 {_base}）")
        c.ok("clip_" in _rs.summary(), "摘要能列出有成绩的参考")
    finally:
        _rs.STATS_PATH = _real_stats
        _rs.STATS_PATH.unlink(missing_ok=True)
        _rs.reset_memory()

    # ---- 指纹路径（2026-09-18 加）----
    # 为什么单独测：指纹路径是**另一条代码分支**（_feed_fingerprint），阈值/去重/时间换算
    # 都不一样；实测踩过的坑全在这条路径上：① 少一个常量 FP_BASELINE_MARGIN 直接抛异常，
    # 而当时的自检全绿；② stream_time 恒为 0 ⇒ 命中被映射到未来而被状态机丢掉（"实战零命中"的元凶）。
    # 这里用合成"播报"（音符序列，时频星座明显）+ 白噪声，验证：路径被选中、能命中、
    # 时间换算对、纯噪声不报、stream_time 会随喂入增长。
    def fake_line(dur: float = 2.4) -> np.ndarray:
        """合成"播报"：8 个**互不相同**的音依次排列（每个都带二三次谐波）。
        为什么音要不同：指纹的哈希键是 (f1,f2,Δt)，同一个音重复出现会落回同一批键，
        哈希表长不起来（实测 6 音重复的 3 秒版本只有 26 个哈希，8 个不同音有 88 个）。"""
        freqs = (380, 560, 900, 640, 1300, 480, 1100, 760)
        n = int(dur * P.ANALYSIS_RATE)
        t = np.arange(n) / P.ANALYSIS_RATE
        sig = np.zeros(n, dtype=np.float32)
        seg = n // len(freqs)
        for i, f in enumerate(freqs):
            a, b = i * seg, (i + 1) * seg
            tt = t[a:b] - t[a]
            env = np.hanning(b - a) ** 0.7
            sig[a:b] = (np.sin(2 * np.pi * f * tt) + 0.5 * np.sin(2 * np.pi * 2 * f * tt)
                        + 0.3 * np.sin(2 * np.pi * 3 * f * tt)) * env
        return (sig * 0.3).astype(np.float32)

    line = fake_line()
    tmpl_fp = P.VoiceTemplate(name="fp-synth", feats=P.band_features(line),
                              onset_sec=0.0, source="self-test", pcm=line)
    d2 = P.PlantVoiceDetector([tmpl_fp])
    c.ok(len(d2._fp_refs) == 1, "带 pcm 的模板会走指纹路径")
    # 专用随机源：不要复用上面的 rng（它的状态取决于前面抽了几次，会让自检忽绿忽红）
    rng2 = np.random.default_rng(11)
    gap = rng2.standard_normal(int(2.5 * P.ANALYSIS_RATE)).astype(np.float32) * 0.05
    stream2 = np.concatenate([gap, line, gap])
    out2 = []
    for s in range(0, len(stream2), step):
        out2 += d2.feed(stream2[s:s + step])
    c.eq(len(out2), 1, "指纹路径：合成播报在噪声流里恰好命中一次")
    if out2:
        c.ok(abs(out2[0].t_stream - 2.5) <= 0.4,
             f"指纹路径的命中时刻 = 播报起点（±0.4s）—— 实际 {out2[0].t_stream:.3f}s")
        c.ok(out2[0].score >= fp_mod.MIN_HITS,
             f"命中分数必须越过阈值 —— 实际 {out2[0].score:.0f} / {fp_mod.MIN_HITS}")
    c.ok(abs(d2.stream_time - len(stream2) / P.ANALYSIS_RATE) < 0.05,
         f"指纹路径下 stream_time 必须随喂入增长（2026-09-18 的 bug 会让它恒为 0）"
         f"—— 实际 {d2.stream_time:.3f}s")
    d3 = P.PlantVoiceDetector([tmpl_fp])
    noise2 = rng2.standard_normal(int(6.0 * P.ANALYSIS_RATE)).astype(np.float32) * 0.3
    out3 = []
    for s in range(0, len(noise2), step):
        out3 += d3.feed(noise2[s:s + step])
    c.eq(len(out3), 0, "指纹路径：纯噪声不报（阈值 MIN_HITS=16）")

    # ---- 迟到的播报候选也必须能确认（2026-09-18 实机 bug 回归）----
    # 实机事实：指纹要"听完"整句才够分 ⇒ 检测滞后 1.8~3.7 秒，候选经常**晚于** GSI
    # 揭示包 0.35~0.93 秒。原实现只在"揭示包到达的那一 tick"里确认候选（_reveal_mono
    # 用完即清）⇒ 迟到的候选永远等不到确认，6 秒后被丢弃。实测三次安放里分数 25 / 17
    # 的那两次就是这么白丢的，只有 16 的那次赶上 ⇒ 白丢两次真值、T0 少修正两局。
    import dataclasses
    app_a = _make_app()
    app_a._team = "CT"
    t0 = 1000.0
    app_a._on_gsi(dataclasses.replace(_BASE, recv_mono=t0, round_phase="live",
                                      map_round=1, bomb_state="planted"))
    c.eq(app_a.state.state, ARMED, "揭示包把状态机推到已安放")
    t_audio = t0 - 1.5                      # 播报比揭示包早 1.5 秒（在 −1.0~+3.5 窗口内）
    hit = P.PlantHit(t_stream=0.0, score=30.0, name="fp")
    before = app_a.state.planted_mono
    app_a._handle_audio_hit(t_audio, hit, now=t0 + 2.0)     # 候选**晚于**揭示包才出现
    c.ok(app_a.state.planted_mono != before, "迟到的播报候选也能修正 T0（不再等揭示包）")
    c.ok(app_a.state.planted_mono is not None
         and abs(app_a.state.planted_mono - t_audio) < 1e-6,
         f"T0 校准到播报时刻 —— 期望 {t_audio}，实际 {app_a.state.planted_mono}")
    c.eq(app_a._pending_audio, None, "确认后候选清空（不会 6 秒后再报一次过期）")
    # 反向：相差超出允许窗口时**不许**改 T0（宁可不改）
    app_b = _make_app()
    app_b._team = "CT"
    app_b._on_gsi(dataclasses.replace(_BASE, recv_mono=t0, round_phase="live",
                                      map_round=1, bomb_state="planted"))
    keep = app_b.state.planted_mono
    app_b._handle_audio_hit(t0 - 9.0, hit, now=t0 + 0.5)    # 差 9 秒，明显不合理
    c.eq(app_b.state.planted_mono, keep, "相差超出窗口的候选不采纳（宁可不改）")

    # ---- 阵营门控已放开（2026-09-19）：T 方也要能用音频 ----
    # 原来只有 CT 方启用，理由是"T 方自己下包本来就准"。实机复核发现那只覆盖了
    # "自己下包"：T 方队友下包时变的是队友的背包，你自己那一包数据没有任何变化，
    # 锚点同样退到常数补偿、同样有 ±1 秒误差。所以门控里不再看阵营。
    app_t = _make_app()
    app_t._team = "T"
    app_t._on_gsi(dataclasses.replace(_BASE, recv_mono=t0, round_phase="live",
                                      map_round=1, bomb_state="planted"))
    app_t._handle_audio_hit(t0 - 1.5, hit, now=t0 + 0.5)
    c.ok(app_t.state.planted_mono is not None
         and abs(app_t.state.planted_mono - (t0 - 1.5)) < 1e-6,
         f"T 方的播报候选同样被采纳（阵营门控已放开）—— 实际 {app_t.state.planted_mono}")

    # 阵营未知（比如刚启动就活着）也不该再挡住音频：门控里已经没有阵营这一条
    app_u = _make_app()
    app_u._team = None
    app_u._on_gsi(dataclasses.replace(_BASE, recv_mono=t0, round_phase="live",
                                      map_round=1, bomb_state="planted"))
    app_u._handle_audio_hit(t0 - 1.5, hit, now=t0 + 0.5)
    c.ok(app_u.state.planted_mono is not None
         and abs(app_u.state.planted_mono - (t0 - 1.5)) < 1e-6,
         "阵营未知时音频照样可用（不再被阵营挡掉）")

    # ---- 播报与锚点一致时保留锚点（2026-09-19 加）----
    app_k = _make_app()
    app_k._team = "CT"
    app_k._on_gsi(dataclasses.replace(_BASE, recv_mono=t0, round_phase="live",
                                      map_round=1, bomb_state="planted"))
    keep_k = app_k.state.planted_mono
    capk = _Capture()
    app_k.log = capk
    app_k._handle_audio_hit(keep_k - 0.05, hit, now=t0 + 0.5)   # 与锚点只差 0.05s
    c.eq(app_k.state.planted_mono, keep_k,
         "播报与锚点只差 0.05s -> 保留锚点，不被音频覆盖")
    # 实机日志里出现过「一致（差 +0.83s）」这种自相矛盾的行（2026-09-19 修）：
    # 那行报的是 `揭示包时刻 − 播报时刻`（GSI 自己的延迟），不是决定 kept 的那个差。
    kept_msg = [t for _tag, t in capk.lines if "锚点一致" in t]
    c.ok(kept_msg and "+0.05" in kept_msg[0],
         f"kept 那行报的差必须是 T0−播报时刻（+0.05），不是 GSI 揭示延迟 —— {kept_msg}")

    # 两个音频工具必须能被导入：本轮重构把 tools/record_audio.py 的 import 漏改了
    # （指向已删除的 legacy_config），自检却全绿 —— 因为自检从不 import 工具。
    # 这条断言就是为了让同类错误下次直接红。
    import importlib.util
    for tool in ("tools/record_audio.py", "tools/audio_calibrate.py"):
        spec = importlib.util.spec_from_file_location(tool.replace("/", "_"), tool)
        mod = importlib.util.module_from_spec(spec)
        if spec.loader is None:
            c.ok(False, f"工具 {tool} 无法加载")
            continue
        try:
            spec.loader.exec_module(mod)
            c.ok(hasattr(mod, "main"), f"工具 {tool} 可导入且有 main()")
        except Exception as exc:                       # noqa: BLE001
            c.ok(False, f"工具 {tool} 导入失败：{exc!r}")

# ---- 配置文件必须能读 BOM，而且读不了要说出来 ----
    # 真实事故：用记事本/Set-Content 写出的 config.json 带 BOM，
    # json.loads 抛错被静默吞掉 -> 整份配置被忽略、程序照默认值跑，
    # 用户却以为配置生效了（曾因此差点让两处修复白做）。
    tmp = ROOT / "runtime" / "_test_cfg_bom.json"
    tmp.parent.mkdir(exist_ok=True)
    body = '{"timers": {"plant_latency_compensation": 0.55}}'
    tmp.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))   # 显式写 BOM
    got = cfgmod.load(tmp)
    c.eq(got.timers.plant_latency_compensation, 0.55, "带 BOM 的 config.json 仍然生效")
    c.eq(cfgmod.LAST_LOAD_ERROR, None, "带 BOM 不算错误（utf-8-sig 兜住了）")

    tmp.write_bytes(b'{"timers": {"plant_latency_compensation": }}')   # 真语法错
    got = cfgmod.load(tmp)
    c.eq(got.timers.plant_latency_compensation,
         Config().timers.plant_latency_compensation,
         "语法错时回退到代码默认值")
    c.ok(cfgmod.LAST_LOAD_ERROR is not None, "语法错会被记下来（doctor 要打出来）")
    tmp.unlink(missing_ok=True)
    cfgmod.LAST_LOAD_ERROR = None

    if verbose:
        print("\n[10c] 日志文件只读时：要么自己清掉只读写进去，要么**报错**，绝不许静默")
    # 2026-09-16 实机：runtime/c4timer.log 被设成只读 -> SessionLog 静默 enabled=False
    # -> 用户打了三轮，日志一个字节没长，我据此误判"程序没在跑"。
    from c4timer.logs import SessionLog
    ro = ROOT / "runtime" / "_test_readonly.log"
    ro.parent.mkdir(exist_ok=True)
    ro.write_text("seed\n", encoding="utf-8")
    os.chmod(ro, stat.S_IREAD)                 # Windows：置只读属性
    try:
        log_ro = SessionLog(ro)
        c.ok(log_ro.enabled or log_ro.error is not None,
             f"只读不许静默：要么写得了、要么记了 error（实际 enabled={log_ro.enabled}, "
             f"error={log_ro.error}）")
        if log_ro.enabled:
            log_ro.write("TEST", "清掉只读之后确实写进去了")
            log_ro.close()
            c.ok(ro.stat().st_size > len("seed\n"),
                 "清掉只读属性后日志真的长长了")
        else:
            c.ok("Permission" in (log_ro.error or ""),
                 f"失败时 error 要说明原因（实际 {log_ro.error}）")
    finally:
        os.chmod(ro, stat.S_IWRITE)
        ro.unlink(missing_ok=True)

    if verbose:
        print("\n[11] 爆炸标定：GSI 报 exploded 时 app 必须写一行 CALIB（含误差与建议值）")
    app = _make_app()

    cap = _Capture()
    app.log = cap                      # 不落盘，只看写进去什么
    # 启动横幅：没有它就分不清"程序没跑"和"跑了但没收到 GSI"（2026-09-16 实机踩过）
    app._write_start_banner()
    banner = [t for tag, t in cap.lines if tag == "START"]
    c.eq(len(banner), 1, "启动横幅写一行")
    if banner:
        c.ok("pid=" in banner[0] and f"补偿={app.cfg.timers.plant_latency_compensation:.2f}"
             in banner[0], f"横幅里带 pid 与生效参数 —— {banner[0]}")
    cap.lines.clear()
    now = time.monotonic()
    comp = app.cfg.timers.plant_latency_compensation
    notify = app.cfg.timers.explosion_notify_latency
    lag = app.cfg.timers.explode_after_deadline_sec
    # 这一局"截止时刻"比本地归零晚 0.25s（也就是倒计时**偏快** 0.25s）：
    # 报爆炸时刻 = 安放报文时刻 + (40 − comp) + 0.25 + lag + notify
    app._on_gsi(replace(_BASE, recv_mono=now - 40.0, round_phase="live",
                        bomb_state="planted", round_bomb="planted"))
    c.eq(app.state.state, ARMED, "标定用例：先进入 ARMED")
    app._on_gsi(replace(_BASE, recv_mono=now - 40.0 + (40.0 - comp) + 0.25 + lag + notify,
                        round_phase="live", bomb_state="exploded", round_bomb=None))
    c.eq(app.state.state, EXPLODED, "标定用例：exploded -> EXPLODED")
    calib = [t for tag, t in cap.lines if tag == "CALIB"]
    c.eq(len(calib), 1, "恰好写一行 CALIB（不重复、不刷屏）")
    if calib:
        line = calib[0]
        # 这一局的"上一个报文"就是安放那包，隔了 ~39 秒 -> 区间必然很宽，
        # 所以**不许**给建议值（实机教训：把报文时刻当爆炸时刻，
        # 7 局全报"偏快"，差点据此把补偿值改成负数）
        c.ok("区间太宽" in line, f"报文间隔大 -> 只给区间不给建议 —— {line}")
        c.ok("建议=" not in line, f"宽区间不许出现建议值 —— {line}")
    # 拆弹成功不该写 CALIB
    cap.lines.clear()
    app._on_gsi(replace(_BASE, recv_mono=now + 5.0, round_phase="live", map_round=2,
                        bomb_state="defused", round_bomb=None))
    c.eq([t for tag, t in cap.lines if tag == "CALIB"], [], "拆弹成功不写 CALIB")

    # T0 来自服务端真值的回合：不打"建议补偿值"（补偿没参与对齐，打了是误导）
    cap.lines.clear()
    app2 = _make_app()
    app2.log = cap
    t0 = time.monotonic()
    app2._on_gsi(replace(_BASE, recv_mono=t0, round_phase="live", map_round=5,
                         observer=True, bomb_state="planted", round_bomb="planted",
                         explosion_remaining=30.0))
    # 观战时包很密：爆炸前 0.2 秒还有一个包 -> 区间才够窄，够资格谈"误差"
    app2._on_gsi(replace(_BASE, recv_mono=t0 + 29.8, round_phase="live", map_round=5,
                         observer=True, bomb_state="planted", round_bomb="planted",
                         explosion_remaining=0.2))
    app2._on_gsi(replace(_BASE, recv_mono=t0 + 30.0, round_phase="live", map_round=5,
                         observer=True, bomb_state="exploded", round_bomb=None))
    calib = [t for tag, t in cap.lines if tag == "CALIB"]
    c.eq(len(calib), 1, "真值路径同样写一行 CALIB（区间是有用信息）")
    if calib:
        c.ok("gsi+comp" not in calib[0], f"真值路径必须标成非 gsi+comp —— {calib[0]}")
        c.ok("建议=—" in calib[0], f"真值路径不打印建议补偿值 —— {calib[0]}")
        # 关键自检：下界应当恰好是 通知延迟 + 真爆滞后（= 1.12），说明常量用对了
        c.ok(f"[+{notify + lag:.2f}" in calib[0],
             f"真值对齐时误差下界应当恰好 = 通知延迟+真爆滞后 {notify + lag:.2f}s —— {calib[0]}")

    # [13] 落盘上限 + "攒够没有"的判定（2026-09-19 加）
    # 为什么两件事必须一起测：以前这两类文件**只增不减**（打一下午能堆几百 MB）；
    # 一旦给它们加上限，"片段总数"就不再增长 —— 而旧判定用的是总数，
    # 于是 `fresh` 恒为 0、**自动重标永久停摆**。上限与判定是一体的。
    from c4timer import autocal
    from c4timer.app import _prune_dumps

    tmp = ROOT / "runtime" / "_test_dumps"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        for i in range(1, 26):
            (tmp / f"plant_20260919_2230{i:02d}.wav").write_bytes(b"x")
        c.eq(len(list(tmp.glob("plant_*.wav"))), 25, "[13] 造 25 个片段当样本")

        gone = _prune_dumps("plant_*.wav", 20, directory=tmp)
        left = sorted(p.name for p in tmp.glob("plant_*.wav"))
        c.eq(gone, 5, "[13] 超上限的 5 个被删掉")
        c.eq(len(left), 20, "[13] 只留 20 个")
        c.ok(left[0].endswith("223006.wav"), f"[13] 删的是最旧的 5 个，现在最旧的是 {left[0]}")
        c.ok(left[-1].endswith("223025.wav"), f"[13] 最新的那个还在：{left[-1]}")
        c.eq(_prune_dumps("plant_*.wav", 20, directory=tmp), 0, "[13] 没超上限时一个都不删")
        c.eq(_prune_dumps("plant_*.wav", 0, directory=tmp), 0, "[13] keep<=0 当不限，不删")

        clips = sorted(tmp.glob("plant_*.wav"))
        newest = clips[-1].name
        c.eq(autocal.fresh_clips(clips, newest), 0, "[13] 刚建完 -> 0 个新片段")
        (tmp / "plant_20260919_223100.wav").write_bytes(b"x")
        clips = sorted(tmp.glob("plant_*.wav"))
        c.eq(autocal.fresh_clips(clips, newest), 1,
             "[13] 来一个新片段就算出 1 个（不依赖总数，削顶后照样能触发重标）")
        c.eq(autocal.fresh_clips(clips, ""), len(clips),
             "[13] 没有标记（首次 / 旧格式）-> 全算新片段")

        marker = tmp / ".build_marker"
        marker.write_text("131 1789814539\n", encoding="utf-8")
        c.eq(autocal._read_marker(marker), ("", 1789814539.0),
             "[13] 旧格式标记按「不知道用到了哪一段」处理，会重建一次")
        marker.write_text("plant_20260919_223025.wav 1789814539\n", encoding="utf-8")
        c.eq(autocal._read_marker(marker)[0], "plant_20260919_223025.wav",
             "[13] 新格式标记读得回来")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # [14] 游戏目录识别：扫盘 + 手填（2026-09-19 加）
    # 回归自一张真实工单：使用者的 Steam 装在 `D:\Program Files (x86)\Steam`，
    # 而当时代码只认五条**写死的完整路径**（`D:\Steam` 之类），一条都不沾
    # ⇒ 体检和「装GSI配置.bat」双双失效，只能手动装 cfg。
    # 教训写进断言里：**按规律找，不要按路径找**。
    from c4timer import cs2path

    saved_default = cfgmod.DEFAULT_PATH
    fake = ROOT / "runtime" / "_test_cs2"
    try:
        fake.mkdir(parents=True, exist_ok=True)
        game = fake / "Steam" / "steamapps" / "common" / cs2path.GAME_DIR_NAME
        (game / "game" / "csgo" / "cfg").mkdir(parents=True, exist_ok=True)
        (game / "game" / "core" / "cfg").mkdir(parents=True, exist_ok=True)
        want = [game]

        c.eq(cs2path.resolve_manual(str(game / "game" / "csgo" / "cfg")), want,
             "[14.1] 粘 cfg 目录本身也认")
        c.eq(cs2path.resolve_manual(str(game / "game" / "csgo")), want,
             "[14.2] 粘 csgo 目录也认")
        c.eq(cs2path.resolve_manual(str(game / "game")), want,
             "[14.3] 粘 game 目录也认")
        c.eq(cs2path.resolve_manual(str(game)), want, "[14.4] 粘游戏根目录也认")
        c.eq(cs2path.resolve_manual(f'"{game}"'), want,
             "[14.5] 带引号也认（拖拽进窗口就是这个形式）")
        c.eq(cs2path.resolve_manual(str(fake / "Steam")), want,
             "[14.6] 粘 Steam 根目录也认")
        c.eq(cs2path.resolve_manual(str(game.parent)), want,
             "[14.7] 粘 steamapps\\common 也认")

        # 判据必须是"下面真的有 game\csgo"，不能只判"这个目录存在"
        c.eq(cs2path.resolve_manual(str(fake)), [],
             "[14.8] 随便一个存在的目录不算数（否则会冒出「已记住：C:\\」这种东西）")
        c.eq(cs2path.resolve_manual(""), [], "[14.9] 空串不算数")
        c.eq(cs2path.resolve_manual(r"Z:\nope\nope"), [], "[14.10] 不存在的路径不算数")

        letters = cs2path._drive_letters()
        c.ok(letters and all(len(x) == 1 and x.isalpha() for x in letters),
             f"[14.11] 盘符枚举拿得到（原来只扫 C..H）：{letters}")

        # 手填的路径必须真的被 find_cfg_dirs 用上 —— 这是这个功能的全部意义
        cfgmod.DEFAULT_PATH = fake / "config.json"
        cfgmod.DEFAULT_PATH.write_text(
            json.dumps({"cs2": {"game_dir": str(game)}}), encoding="utf-8")
        c.eq(cs2path.manual_game_dir(), str(game), "[14.12] 手填的路径读得回来")
        dirs = cs2path.find_cfg_dirs()
        c.ok(game / "game" / "csgo" / "cfg" in dirs,
             "[14.13] find_cfg_dirs 真的用上了手填的路径（装GSI配置.bat 靠的就是它）")
        c.eq(dirs[0], game / "game" / "csgo" / "cfg",
             "[14.14] 手填的排在自动找的前面（用户说了算）")

        cfgmod.DEFAULT_PATH.unlink()
        c.eq(cs2path.manual_game_dir(), "", "[14.15] 没有配置时手填项为空，自动查找照常")

        # 日志查询必须能**只看一个安装**，而且必须能分清
        # "连日志都没有" 与 "有日志但没加载我们" —— 这是这次修输出的核心区别
        g1 = fake / "g1" / cs2path.GAME_DIR_NAME
        (g1 / "game" / "csgo").mkdir(parents=True, exist_ok=True)
        g2 = fake / "g2" / cs2path.GAME_DIR_NAME
        (g2 / "game" / "csgo").mkdir(parents=True, exist_ok=True)
        g3 = fake / "g3" / cs2path.GAME_DIR_NAME
        (g3 / "game" / "csgo").mkdir(parents=True, exist_ok=True)   # 有目录、没日志
        (g1 / "game" / "csgo" / "pwa_1_20260919.log").write_text(
            "09/19 13:10:29 Loading Game State Integration: "
            "gamestate_integration_other.cfg\n", encoding="utf-8")
        (g2 / "game" / "csgo" / "pwa_2_20260919.log").write_text(
            "09/19 13:10:29 Loading Game State Integration: "
            + cs2path.OUR_CFG_NAME + "\n", encoding="utf-8")

        only1 = cs2path.check_cfg_loaded(game=g1)
        c.eq(len(only1), 1, "[14.16] 只看一个安装时只返回那一个的日志")
        c.ok(not only1[0]["loaded"], "[14.17] 有日志但没加载我们 -> 有元素且 loaded=False")
        only2 = cs2path.check_cfg_loaded(game=g2)
        c.ok(bool(only2) and only2[0]["loaded"], "[14.18] 有日志且加载过 -> loaded=True")
        c.eq(cs2path.check_cfg_loaded(game=g3), [],
             "[14.19] 没日志的安装返回**空列表** —— 体检靠这个区分「没重启」与「找错安装」")

        # 体检的结论判据（纯函数，直接喂构造好的事实）
        # 这段就是 2026-09-19 把所有人带偏的地方：旧版只有一句"没有加载记录"，
        # 于是"cfg 装进了另一个安装"被说成了"用户忘了重启游戏"。
        from c4timer.__main__ import _cfg_verdict

        idx = {g1: 1, g2: 2}
        c.ok(_cfg_verdict([], [], [], {}, has_games=False)[0].startswith("[X]"),
             "[14.20] 一个安装都没找到 -> 让用户手动指定")
        c.ok(_cfg_verdict([], [g2], [g2], idx)[0].startswith("[!]"),
             "[14.21] 哪里都没有我们的 cfg -> 让他装一次")
        c.ok(_cfg_verdict([g2], [g2], [g2], idx)[0].startswith("[OK]"),
             "[14.22] cfg 所在安装加载过 -> 正常")
        c.ok(_cfg_verdict([g1], [g1], [], idx)[0].startswith("[!]"),
             "[14.23] cfg 所在安装有日志但没加载 -> 装完没重启")
        wrong = _cfg_verdict([g1], [g2], [], idx)
        c.ok(wrong[0].startswith("[X]") and "[1]" in wrong[0] and "[2]" in wrong[0],
             f"[14.24] cfg 在没日志的安装里、别的安装在玩 -> 明说装错了安装：{wrong[0]}")
        c.ok(_cfg_verdict([g1], [], [], idx)[0].startswith("[X]"),
             "[14.25] 所有安装都没日志 -> 找错了安装")
    finally:
        cfgmod.DEFAULT_PATH = saved_default
        shutil.rmtree(fake, ignore_errors=True)

    if verbose:
        print("\n" + "=" * 72)
        extra = f"  跳过 {c.skipped}" if c.skipped else ""
        print(f"P3 应用层自检完成: {'全部通过' if c.failed == 0 else '有失败'}  "
              f"[OK {c.passed} / X {c.failed}]{extra}")
        print("=" * 72)
    return c.passed, c.failed, c.failures


def main() -> int:
    _, failed, _ = run()
    return 1 if failed else 0
