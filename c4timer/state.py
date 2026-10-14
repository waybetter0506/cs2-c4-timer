"""状态机：把 GSI 样本 + 本地时钟揉成一个可显示的 Snapshot。

设计铁律：**观战真值 > GSI 事件 > 本地兜底**（只有 GSI 一条链路 —— 音频那一路
已在 2026-09-16 整条移除，见 docs/实机反馈与待办.md 第十五节）

- 观战真值：`bomb.countdown` / `bomb.state`（你死了在观战时才有）
- GSI 事件：`round.bomb` 的 planted/defused/exploded、`round.phase` 的回合边界（活着也有）
- 本地兜底：时限归零后再等 `explode_after_deadline_sec + local_bang_grace_sec` 自判爆炸

本模块**不做任何 I/O、不读时钟**：所有时间都由调用方以 `now`（time.monotonic 口径）
传进来。这样状态机的全部行为都可以用确定性测试覆盖，不需要游戏、不需要睡觉。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .config import Config
from .gsi import TERMINAL_STATES, GsiSample

# ---- 内部状态 ----
# 只剩"红字"（炸弹时限）之后的四个状态。拆弹不再是一个状态：
# 拆弹中红字照常走，拆弹成功只是本回合的一个终局（DEFUSED）。
IDLE = "idle"
ARMED = "armed"
DEFUSED = "defused"
EXPLODED = "exploded"

OUTCOME_STATES = (DEFUSED, EXPLODED)

# 状态行的文案。放在模块级是为了让 hud.py 能据此**预留面板宽度**
# （面板宽度按最长文案量出来，不是拍一个数字）——两边共用同一份，不会各改各的。
STATUS_TEXT = {
    IDLE: "待机",
    ARMED: "C4 已安放",
    DEFUSED: "拆弹成功 · 炸弹已解除",
    EXPLODED: "炸弹已爆炸",
}

# 判过终局之后，多久之内拒绝同一回合的"迟到 planted"。
#
# 用途是挡住"爆炸前生成、爆炸后才到"的包（实测晚了约 0.8 秒）。
# 但它同时是 `_round_resolved` 的**时间兜底**：
# `_round_resolved` 正常情况下靠"over -> freezetime"来清除，
# 万一 GSI 因为丢包/合并跳过了 `over`，这个标志就再也清不掉，
# 那一回合之后就**再也不会有倒计时**（静默失败，用户只会报告"有时候没反应"）。
# 实测 407 个包 / 15 次回合切换里 `over` 从未被跳过，但静默失败的代价太大，
# 所以加这条时间兜底让它绝不可能卡死。
LATE_PLANT_GUARD = 8.0

# 标定只认"离本地归零足够近"的那一个 exploded 报文。
# 为什么需要这个窗口：GSI 在观战时会把 exploded 状态**反复重发**
# （真实抓包里同一局有 5 个 exploded 包：1 个 phase=over 的首次通知
#  + 4 个 freezetime 里的残留），而只有首次那个才代表真实爆炸时刻。
# 实测首次通知落在本地归零后 0.07~0.4 秒内，重复包则在数秒之外。
CALIB_WINDOW = 2.0
# 误差区间窄于这个值，才认为"可以给点估计与建议补偿值"。
# 为什么需要它：活着时 GSI 只在**内容变化**时发包，实测包间隔 0.3~10 秒
# （10 秒是 heartbeat）。所以「exploded 报文晚到 1.8 秒」既可能是我们倒计时快了
# 1.8 秒，也可能只是报文本来就隔了 2 秒才发 —— 两者**无法区分**。
# 第一版把报文时刻当成了爆炸时刻，于是每次爆炸都报一个点误差，
# 实机跑出 7 局全"偏快"，差点据此把补偿值改成负数。教训：只能给区间。
CALIB_TIGHT = 0.30

# ---- 活着那一路的锚点（见 C4StateMachine._comp_for_this_plant）----
# "上一包就是被安放事件触发的那一包"时，真实安放 ≈ 上一包时刻 − 这个值。
# 实测：残差 +0.102s、标准差 0.007s（2026-09-16 那 5 局）；
# 夹具两局用**另一种方法**独立测的 L_p（0.92 / 1.41）也落在 ±0.08s 内。
ANCHOR_LEAD = 0.10
# 锚点只在这个间隔内可信：超过就说明"上一包"不是被安放触发的那一包。
# 取值依据 = 安放通知延迟本身的上界（Valve 社区实测 0.5~2.2s，我们实测最长的**有效**锚点
# 是 03:47 那局间隔 2.52s ⇒ L_p ≈ 2.60s）。既然揭示包必然落在"安放 + 通知延迟"之内，
# 上一包又只比安放早约 0.1s，那么**有效**锚点的间隔不可能超过 ≈2.5s。
#
# 取值历史（都是 2026-09-16）：
#   · 2.5  → **太紧**：把 03:47 那局（间隔 2.52s、L_p ≥ 2.60s、锚点本来是对的）挡掉了，
#            退回常数补偿反而晚了 1.10s ⇒ 放宽到 8.0（只凭这一局）。
#   · 8.0  → **太松**：当晚 21:32/21:36 两局实测间隔 4.22/5.26s，锚点给出 4.32/5.36，
#            而真实 L_p 只有 0.75~1.08 / 1.82~2.15s（由"爆炸报文落在本地归零后
#            4.67s、被 ±2.0s 标定窗挡掉"反推）⇒ 红字**早 3.2~3.6 秒**，本地兜底
#            还比真爆早 2.7~3.0 秒喊"炸弹已爆炸" —— 用户报的"快了将近四秒"。
#            同一形态在旧日志里已有 4 例（间隔 4.31/5.05/5.29/5.77s，真实 L_p ≈ 0.2s）。
#   · 3.0（当前）→ 既留住 03:47 那局（2.52+0.10=2.62 ≤ 3.0），又挡住全部 6 个坏例
#            （最小 4.22 ⇒ 4.32 > 3.0）。间隔 2.9~4.2 这段没有样本，留着观察。
ANCHOR_COMP_MAX = 3.0

# 音频（"安放播报"）检测结果最多能有多旧还采信（秒）。
# 检测器要等模板长度（2.2 秒）的音频才能出结果 ⇒ 命中天然滞后 2.2~2.5 秒，
# 所以这个窗口必须比它大；再大连"上一回合的命中"都可能被误用。
AUDIO_MAX_AGE = 3.5
# 音频说的时间与 GSI 锚点之差超过这个范围就不采信（修 T0 时用）。
AUDIO_REFINE_WINDOW = (-1.0, 3.5)
# 两者本来就一致时**别动**（2026-09-19 加）。
# 锚点实际上只有两种状态：认出"安放触发那一包"时准到 0.02 秒级；认不出时退到常数
# 补偿、偏 0.3~2.4 秒。所以"音频与锚点差不到这个数"基本就等于"锚点走的是准的那条路"
# —— 这时拿音频（±0.05~0.1 秒）去换掉它只会更钝。延迟那条路偏 0.5 秒以上，拦不住。
AUDIO_KEEP_ANCHOR_SEC = 0.3


def calibration_error(cfg: "Config", local_zero_mono: float | None,
                      gsi_exploded_mono: float,
                      prev_gsi_mono: float | None = None,
                      ) -> tuple[float, float] | None:
    """本局计时误差的**区间**（不是点估计）。

    误差 E = 本地归零 − 真实**截止时刻**。真实爆炸落在 `(prev_gsi_mono, gsi_exploded_mono]`：
      - 上一个报文里炸弹还没爆（还是 planted），所以爆炸在它**之后**；
      - 这个报文里已经是 exploded，所以爆炸在它**之前**（或就是它触发的这次发送）。
    再叠两个常量：爆炸最晚 = 报文时刻 − `explosion_notify_latency`（实测 0.07s）；
    而红字的语义是**拆弹最后时限**，它比真爆还早 `explode_after_deadline_sec`（实测 1.05s）。

    于是 E ∈ [本地归零 − (报文 − 延迟 − 真爆滞后), 本地归零 − 上一个报文 + 真爆滞后]。
    区间宽度 ≈ 报文间隔。**观战时包很密（0.2s），区间就窄**；
    活着时包可能隔几秒，区间就宽 —— 宽区间只能说明"这一局证明不了什么"。

    误差 > 0 表示本地归零**晚于**截止时刻（倒计时偏慢）；< 0 表示偏快。
    要让归零提前 E，补偿值要**加上** E（comp 被减掉，所以 comp 越大归零越早）。

    返回 (下界, 上界)；没有上一个报文时退化为点估计；没建立过 T0 时返回 None。
    """
    if local_zero_mono is None:
        return None
    lag = cfg.timers.explode_after_deadline_sec
    lo = local_zero_mono - (gsi_exploded_mono - cfg.timers.explosion_notify_latency - lag)
    if prev_gsi_mono is None or prev_gsi_mono >= gsi_exploded_mono:
        return lo, lo
    hi = local_zero_mono - prev_gsi_mono + lag
    return lo, max(lo, hi)


@dataclass(frozen=True)
class Snapshot:
    """状态机对外的唯一输出。render / hud 只认这个。"""

    state: str
    status_text: str
    explosion_remaining: float | None
    source: str                      # gsi / gsi+comp / gsi-truth / -
    round_phase: str | None
    map_round: int | None
    gsi_live: bool
    gsi_age: float | None
    planted_mono: float | None
    # T0 未知（回合中途才启动，没看到安放那一刻）-> 界面显示"时限未知"
    anchor_unknown: bool = False

    @property
    def is_idle(self) -> bool:
        return self.state == IDLE

    @property
    def is_outcome(self) -> bool:
        return self.state in OUTCOME_STATES

    @property
    def planted(self) -> bool:
        return self.state == ARMED

    @property
    def visible(self) -> bool:
        return self.state != IDLE

    @property
    def deadline_passed(self) -> bool:
        """红字已归零、但还没确认爆炸：界面要写"时限已到 · 即将爆炸"。"""
        return self.state == ARMED and self.explosion_remaining is not None \
            and self.explosion_remaining <= 0.0


@dataclass
class Event:
    mono: float
    text: str
    # 单调递增序号：app 靠它把"新事件"写进日志文件。
    # 不能用下标 —— events 满了会从头部裁掉（见 _log）。
    seq: int = 0


@dataclass
class C4StateMachine:
    """全部状态都在这里，没有隐藏的全局量。"""

    cfg: Config
    state: str = IDLE
    # 安放时刻（monotonic）。炸弹时限剩余 = planted_mono + bomb_seconds - now
    planted_mono: float | None = None
    # 本局实际用的补偿值（间隔夹取之后可能与配置常数不同）
    planted_comp: float | None = None
    # T0 是否可信。回合中途才启动/重连时，我们拿到"已安放"报文的那一刻
    # 游戏计时器可能已经走了很久 —— 这时**不给数字**，界面显示"时限未知"。
    anchor_unknown: bool = False
    # 终局出现的时刻
    outcome_mono: float | None = None
    outcome: str | None = None
    # 来源标记
    source: str = "-"
    # GSI 活性
    last_gsi_mono: float | None = None
    last_plant_source: str | None = None
    # 回合
    round_phase: str | None = None
    map_round: int | None = None
    _round_open: bool = False
    _last_round_seen: int | None = None
    # 本回合是否已经判过终局。一旦判过，就拒绝同一回合的"安放"事件 ——
    # 否则一个**迟到**的 planted 包（爆炸前生成、爆炸后才到）会把刚判定的终局
    # 复活成 ARMED，造成"安放后 1 秒又爆炸"的假闪烁（demo 在真实报文上抓到过：
    # 320.47s 的 live/planted 迟于 319.5s 本地计时器判定的爆炸）。
    _round_resolved: bool = False
    # 判定终局的时刻。**不随 reset() 清空** —— 时间兜底要靠它。
    _resolved_mono: float | None = None
    # 本回合是否见过"安放之前"的报文。用来判断 T0 可不可信（见 anchor_unknown）。
    _saw_round_before_plant: bool = False
    events: list[Event] = field(default_factory=list)
    # 每局标定用的数据。**不随 reset() 清空** —— 因为 GSI 报爆炸的包常常在我们
    # 自己（本地计时器）判完爆炸之后才到，那时 reset 已经跑过、planted_mono 没了。
    # 只在**下一次安放**时被覆盖。
    calib_zero_mono: float | None = None
    calibration_source: str | None = None
    last_calibration: tuple[float, float] | None = None
    _calib_reported: bool = False
    # 上一个报文的时刻（标定要拿它给出误差区间）
    prev_gsi_mono: float | None = None
    # 上一个报文是不是"内容变化触发"的（heartbeat 两者都空）。
    # 用途：活着那一路的锚点研究 —— 安放事件会**立刻**触发一包（0.11 秒后），
    # 但那包的炸弹状态还是旧的；带 planted 的那包要等到下一次发包机会。
    # 所以"安放前最后一包"的时刻比"报安放那包"更接近真实安放，
    # 前提是那一包确实是内容变化触发的（见 docs/实机反馈与待办.md 第十六节）。
    _last_delta: bool = False
    _prev_delta: bool = False
    # 上一包/再上一包改动的键（诊断用，见 docs 第二十节）：用来在日志里记录
    # "安放前最后一包到底改了什么"，从而验证它能不能从内容上被认出来。
    _last_changed: tuple[str, ...] = ()
    _prev_changed: tuple[str, ...] = ()
    # 事件序号（app 用来只写"新事件"）
    _event_seq: int = 0

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _log(self, now: float, text: str) -> None:
        self._event_seq += 1
        self.events.append(Event(now, text, self._event_seq))
        if len(self.events) > 500:
            del self.events[: len(self.events) - 500]
        if self.cfg.debug.print_events:
            print(f"[{now:9.2f}s] {text}", flush=True)

    def gsi_live(self, now: float) -> bool:
        if self.last_gsi_mono is None:
            return False
        return (now - self.last_gsi_mono) <= self.cfg.gsi.stale_after_sec

    def gsi_age(self, now: float) -> float | None:
        if self.last_gsi_mono is None:
            return None
        return now - self.last_gsi_mono

    @property
    def planted(self) -> bool:
        """炸弹是否处于"已安放、正在等时限"的活跃状态。"""
        return self.state == ARMED

    # ------------------------------------------------------------------
    # 重置
    # ------------------------------------------------------------------

    def reset(self, now: float, reason: str) -> None:
        if self.state != IDLE:
            self._log(now, f"重置: {reason}")
        self.state = IDLE
        self.planted_mono = None
        self.anchor_unknown = False
        self._saw_round_before_plant = False
        self.outcome_mono = None
        self.outcome = None
        self.source = "-"
        self.last_plant_source = None

    # ------------------------------------------------------------------
    # 安放
    # ------------------------------------------------------------------

    def _plant(self, now: float, source: str, t0: float, unknown: bool = False,
               detail: str = "") -> None:
        self.planted_mono = t0
        # 本局**实际用的**补偿值（= 报文时刻 − T0）。用配置常数会骗人：
        # 间隔上界夹取之后本局可能不是 1.5 秒（见 _comp_for_this_plant）。
        self.planted_comp = now - t0
        self.anchor_unknown = unknown
        self.state = ARMED
        self.source = source
        self.last_plant_source = source
        self.outcome = None
        self.outcome_mono = None
        # 钉下本局的"本地归零时刻"与它的来源，供 exploded 报文到达时标定
        self.calib_zero_mono = t0 + self.cfg.timers.bomb_seconds
        self.calibration_source = source
        self.last_calibration = None
        self._calib_reported = False
        if unknown:
            self._log(now, "安放：本回合没见过「安放之前」的报文 -> T0 不可信，"
                           "界面显示「时限未知」")
        else:
            self._log(now, f"安放({source})  时限 {self.cfg.timers.bomb_seconds:.1f}s 起{detail}")

    def plant_allowed(self, now: float) -> bool:
        if self.planted:
            return False
        if self._round_resolved:
            # 时间兜底：防止 `over` 被跳过时这个标志永远清不掉（见 LATE_PLANT_GUARD）
            if self._resolved_mono is not None and \
                    (now - self._resolved_mono) > LATE_PLANT_GUARD:
                self._round_resolved = False
                self._resolved_mono = None
            else:
                return False        # 本回合刚判过终局 -> 迟到的 planted 不算数
        # 有回合信息时，回合已结束就不接受残留的安放事件（铁律 5）
        if self.round_phase is not None:
            return self._round_open
        # 完全没有回合信息（例如只收到裸的 round.bomb），放行
        return True

    # ------------------------------------------------------------------
    # 终局
    # ------------------------------------------------------------------

    def _go_outcome(self, now: float, outcome: str) -> None:
        if self.state in OUTCOME_STATES:
            return
        self.state = outcome
        self.outcome = outcome
        self.outcome_mono = now
        self._round_resolved = True
        self._resolved_mono = now
        self._log(now, "拆弹成功" if outcome == DEFUSED else "炸弹爆炸")

    # ------------------------------------------------------------------
    # GSI
    # ------------------------------------------------------------------

    def feed_gsi(self, sample: GsiSample) -> None:
        now = sample.recv_mono
        # 上一个报文的时刻：标定要用它给出**不用任何假设**的误差区间
        # （爆炸发生在"上一个说 planted 的包"和"这个说 exploded 的包"之间）。
        self.prev_gsi_mono = self.last_gsi_mono
        self._prev_delta = self._last_delta
        self._last_delta = sample.carried_delta
        self._prev_changed = self._last_changed
        self._last_changed = sample.changed
        # 收到任何合法包都算 GSI 活着（1 代教训：不能用"收到特定内容"判断连通性）
        self.last_gsi_mono = now

        # --- 回合记录（只更新，不重置）---
        if sample.round_phase is not None:
            prev_phase = self.round_phase
            self.round_phase = sample.round_phase
            self._round_open = (sample.round_phase == "live")
            # 回合真的翻页了：先 over、再 freezetime，此时才解锁"已判定终局"
            if prev_phase == "over" and sample.round_phase == "freezetime":
                self._round_resolved = False
                self._resolved_mono = None
        if sample.map_round is not None:
            if self._last_round_seen is not None and sample.map_round != self._last_round_seen:
                self._round_resolved = False
                self._resolved_mono = None
                # 新回合：还没见过它"安放之前"的样子
                self._saw_round_before_plant = False
            self.map_round = sample.map_round

        # T0 可信性：本回合是否见过"安放之前"的报文（见 anchor_unknown）。
        # 必须放在终局/重置分支**之前**，否则 freezetime 那一包会被提前 return 掉。
        if sample.bomb_state not in ("planting", "planted", "defusing"):
            self._saw_round_before_plant = True

        # --- 终局：必须排在**任何重置之前** ---
        # 实测教训：`map.round` 变化的那一包同时带着 `defused`。
        # 如果先执行"回合变更 -> 重置"，planted_mono 会被清空，
        # 终局的"本回合必须见过安放"守卫就会把它挡掉 —— 结果 DEFUSED 永远不出现。
        if sample.bomb_state in TERMINAL_STATES:
            # 标定必须在这里做，而且**不能**要求"还没判过终局"：
            # 本地计时器通常先归零、先把状态推到 EXPLODED，GSI 的 exploded 报文
            # 才晚 0.07~0.4 秒到达 —— 用 out-of-outcome 当条件的话，
            # 恰恰在最常见的情况下什么都测不到（实机跑出来就是这样）。
            if sample.bomb_state == EXPLODED and not self._calib_reported:
                zero = (self.planted_mono + self.cfg.timers.bomb_seconds
                        if self.planted_mono is not None else self.calib_zero_mono)
                if zero is not None and abs(now - zero) <= CALIB_WINDOW:
                    # abs 窗口：挡掉观战时反复重发的残留 exploded 包（见 CALIB_WINDOW）
                    self.last_calibration = calibration_error(
                        self.cfg, zero, now, self.prev_gsi_mono)
                    self._calib_reported = True
            if self.planted_mono is not None and self.state not in OUTCOME_STATES:
                self._go_outcome(now, sample.bomb_state)
            self._last_round_seen = self.map_round
            return

        # --- 回合变更 -> 重置 ---
        if (self.map_round is not None and self._last_round_seen is not None
                and self.map_round != self._last_round_seen):
            if self.state != IDLE:
                self.reset(now, f"回合变更 {self._last_round_seen}->{self.map_round}")
        self._last_round_seen = self.map_round

        # --- 回合结束但没有炸弹事件 -> 重置 ---
        if sample.round_phase in ("over", "freezetime"):
            if self.state != IDLE:
                self.reset(now, f"回合{sample.round_phase}")
            return

        # --- 真值：服务端倒计时（观战时唯一准的锚点）---
        if sample.explosion_remaining is not None:
            # E2 实测（2026-09-15 23:35:35）：归零之后游戏会有一包把倒计时从 0.0
            # **直跳到 −1.9**（0.07 秒内跳 1.9 秒，全日志出现 2 次，纯假值）。
            # 照单全收的后果：T0 被推后 1.9 秒 -> 本地兜底爆炸阈值瞬间"早已过点"
            # -> "炸弹已爆炸"比真爆早约 0.9 秒出现（真爆在 exploded 报文前 0.07 秒）。
            # 负值只说明"时限已过"，不含更多信息，所以夹到 0：
            # 归零后的锚点就是"现在归零"，本地兜底仍按 归零+1.05+宽限 等，GSI 的
            # exploded 报文（归零后约 1.1 秒）通常先到，正常接管。
            truth_remaining = max(0.0, sample.explosion_remaining)
            target_t0 = now - (self.cfg.timers.bomb_seconds - truth_remaining)
            if self.planted_mono is None:
                self._plant(now, "gsi", target_t0)
            else:
                self.anchor_unknown = False      # 拿到真值 -> T0 可信了
                delta = target_t0 - self.planted_mono
                # 本局原本是"活着靠 round.bomb + 补偿"锚定的？那这次拿到服务端真值
                # 就是**唯一一次能直接量出本局补偿误差**的机会：
                #   delta = target_t0 − planted_mono = 补偿 − 真实安放延迟 L_p
                # 所以 L_p ≈ 补偿 − delta。活着时"面板跳出就 38 秒"这类观感，
                # 到底是我们慢/快多少，就靠这一行在实机上攒样本（2026-09-16 加）。
                came_from_comp = self.calibration_source == "gsi+comp"
                if abs(delta) >= self.cfg.timers.snap_tolerance_sec:
                    self.planted_mono = target_t0
                    # T0 现在来自服务端真值了 —— 标定那一行的"建议补偿值"就失去意义
                    # （补偿根本没参与对齐），必须把来源改掉，否则会误导。
                    self.calibration_source = "gsi-truth"
                    self._log(now, f"对时(直接对齐) 偏差 {delta:+.2f}s")
                elif abs(delta) > 1e-3:
                    step = max(-self.cfg.timers.smooth_step_sec,
                               min(self.cfg.timers.smooth_step_sec, delta))
                    self.planted_mono += step
                    self.calibration_source = "gsi-truth"
                if came_from_comp:
                    # 用**本局实际用的**补偿值算，别用配置常数（夹取后两者可能不同）
                    comp = (self.planted_comp
                            if self.planted_comp is not None
                            else self.cfg.timers.plant_latency_compensation)
                    self._log(now, f"切到服务端真值：本局补偿误差 {delta:+.2f}s"
                                   f"（补偿={comp:.2f}，实际安放延迟 L_p≈{comp - delta:.2f}s）")

        # --- 拆弹（`defusing`）不再是一个状态：红字照常走，什么都不用做 ---
        if sample.bomb_state == "defusing":
            return

        # --- 仅凭 round.bomb 得知已安放（活着时的主路径）---
        if sample.bomb_state in ("planting", "planted") and self.plant_allowed(now):
            # 活着时拿不到服务端倒计时，只能用"安放通知延迟"补偿。
            # 注意：Valve 故意让这个通知晚到（2015-12-17 补丁："Bomb plant timer
            # Game State Integration event is now slightly delayed when playing on
            # Terrorist or Counter-Terrorist team."，社区实测 0.5~2.2 秒随机），
            # 所以活着时的数字天生有秒级误差 —— 这是游戏设计，不是实现缺陷。
            comp, gap, how = self._comp_for_this_plant(now)
            t0 = now - comp
            # 上包Δ（诊断用，2026-09-16 晚加）：锚点是否可靠，就取决于"上一包到底改了什么"。
            # 收几局数据就能验证"安放触发包"能否从内容上认出来（见 docs 第二十节）。
            prev_keys = ",".join(self._prev_changed) if self._prev_changed else "-"
            detail = (f"  {how} 间隔={gap:.2f}s chg={int(self._prev_delta)}"
                      f" 本局补偿={comp:.2f} 上包Δ={prev_keys}")
            # T0 可信性：本回合见过"安放之前"的报文才算可信
            self._plant(now, "gsi+comp", t0, unknown=not self._saw_round_before_plant,
                        detail=detail)

    # ------------------------------------------------------------------
    # 音频真值（"安放播报"检测，2026-09-17 加）
    # ------------------------------------------------------------------

    def feed_audio_plant(self, plant_mono: float, now: float, *,
                         score: float = 0.0, name: str = "-") -> str:
        """用"安放播报"的检测结果定 T0。

        调用方负责**回合门控**（必须是 live、且还没被揭示包确认过）。
        阵营门控**2026-09-19 取消**：原来只有 CT 方用，理由是"T 方自己下包本来就准"，
        但那只覆盖了"自己下包" —— **T 方队友下包时变的是队友的背包，你自己那一包数据
        没有任何变化**，锚点同样要退到常数补偿、同样有 ±1 秒误差。现在两队都用。

        本方法只做"时间合不合理"这一层：
          · 太旧（> AUDIO_MAX_AGE）不采信 —— 检测器天然滞后 2.2~2.5 秒；
          · 本局还不知道安放 ⇒ 直接用它建立 T0（这就是"比 GSI 早 0.03~2.6 秒"的来源）；
          · 已经知道安放 ⇒ 相差在 AUDIO_REFINE_WINDOW 内就修正 T0；
            但若两者本来就一致（差 < AUDIO_KEEP_ANCHOR_SEC），保留锚点不动
            —— 那说明锚点本来就是准的那条路，它比音频更准。
          · 相差超出窗口 ⇒ 说明两者里有一个不对，宁可不改。

        返回 'planted' / 'refined' / 'kept' / 'ignored'（调用方据此写日志）。
        """
        if not (0.0 <= now - plant_mono <= AUDIO_MAX_AGE):
            return "ignored"
        if self.state == IDLE:
            if not self.plant_allowed(now) or self.round_phase != "live":
                return "ignored"
            self._plant(now, "audio", plant_mono, unknown=False,
                        detail=f"  播报命中 分数={score:.2f} 参考={name}")
            return "planted"
        if self.state == ARMED and self.planted_mono is not None:
            delta = self.planted_mono - plant_mono          # >0 = 音频说得更早
            if abs(delta) < AUDIO_KEEP_ANCHOR_SEC:
                return "kept"                               # 两者一致：锚点更准，不动它
            if AUDIO_REFINE_WINDOW[0] <= delta <= AUDIO_REFINE_WINDOW[1]:
                self.planted_mono = plant_mono
                # 2026-09-18：把"音频已经改过 T0"记进来源。原来 CALIB 那行永远印
                # "gsi+comp"，音频修正过也看不出来（实机诊断时被这条误导过）。
                # 副作用是 `came_from_comp` 判据变 False ⇒ "本局补偿误差"那行不再打印，
                # 这是**想要的**：T0 已经不来自补偿了，再拿它算 L_p 只会误导。
                self.calibration_source = ((self.calibration_source + "+audio")
                                          if self.calibration_source else "audio")
                self._log(now, f"音频修正 T0 {delta:+.2f}s（分数={score:.2f} 参考={name}）")
                return "refined"
        return "ignored"

    def _comp_for_this_plant(self, now: float) -> tuple[float, float, str]:
        """本局用哪个补偿值（返回 (补偿, 距上一包的间隔, 用了哪条路)）。

        **① 锚点**（首选）：实测发现 Valve 延迟的不是"发包"，而是把 `planted` 状态藏起来 ——
        安放后约 0.10 秒游戏确实发了一包（被安放事件触发的，`chg=1`），但那包的炸弹状态
        还是旧的；带 `added: {round: {bomb: true}}` 的包要等下一次发包机会。
        所以 **真实安放 ≈ 上一包时刻 − 0.10s** ⇒ 本局补偿 = `间隔 + 0.10`。

        实测（2026-09-16，n=22：17 局旧数据 + 本轮 5 局）：
          · 残差 (L_p − 间隔) = **+0.102s，标准差 0.007s**（本轮 5 局）
          · 用这条锚点预测，误差平均 −0.01s、最大 0.02s （对比：常数补偿 0.43s）
          · 夹具里两局（另一种方法独立测的 L_p = 0.92 / 1.41）也落在 ±0.08s 内
        guard：只有"上一包确实带内容变化（`chg=1`）"时才用，否则退回 ②。
        （心跳包做上一包时，唯一硬界是 `L_p ≤ 间隔`，锚点会把安放算早最多一个间隔 ⇒ 不能用；
        2026-09-16 实测：心跳做上一包的局，真实 L_p 只有 0.3~0.8 秒。）
        guard 2：`间隔 + 0.10 ≤ ANCHOR_COMP_MAX`（3.0）—— 揭示包必然落在"安放 + 通知延迟
        （≤2.6s）"之内，所以**有效**锚点的间隔不可能超过约 2.5s；间隔更大时那一包只是碰巧的
        内容变化包，拿它当锚点会把"回合里的其他动静"整段算成安放延迟
        （2026-09-16 21:32 那局：间隔 4.22s ⇒ 补偿 4.32，而真实 L_p 只有 0.75~1.08s，
        红字早 3.2~3.6 秒；见 docs/实机反馈与待办.md 第十九节）。

        **② 硬上界夹取**（兜底）：上一包里炸弹还没安放 ⇒ 真实安放必然在它之后
        ⇒ `L_p ≤ 间隔`，间隔明显小于常数补偿时用 `间隔 + 0.2`。
        实测把平均绝对误差 0.43 → 0.21 秒、最坏偏早 −0.79 → −0.10 秒。

        常数补偿（`timers.plant_latency_compensation`）只在 ② 也用不了时（第一包就是
        planted、没有上一包）单独生效，同时是 ② 的天花板 —— 2026-09-16 由 1.5 下调到 1.0：
        "心跳做上一包"的两局实测 L_p 只有 0.76 / 0.3~0.8 秒，吃 1.5 会系统偏早 0.7~1.3 秒，
        而这正是用户报的"归零过早"（偏晚是刻意选的取舍方向）。
        """
        comp = self.cfg.timers.plant_latency_compensation
        prev = self.prev_gsi_mono
        if prev is None:
            return comp, float("nan"), "常数"
        gap = now - prev
        if self._prev_delta and gap + ANCHOR_LEAD <= ANCHOR_COMP_MAX:
            return max(0.05, gap + ANCHOR_LEAD), gap, "锚点"
        # 余量 0.2 秒：GSI 数据本身有约 0.1 秒滞后（两条独立证据链的常数差 K≈+0.10），
        # 再留一点抖动；保底 0.3 秒防"陈旧滞留包"让上界失效时把补偿压到 0。
        return min(comp, max(0.3, gap + 0.2)), gap, "夹取"

    # ------------------------------------------------------------------
    # 时钟推进
    # ------------------------------------------------------------------

    def tick(self, now: float) -> None:
        if self.state == IDLE:
            return

        # 终局文案停留 hide_after_end_sec 后回到 IDLE
        if self.state in OUTCOME_STATES:
            if self.outcome_mono is not None and \
                    (now - self.outcome_mono) >= self.cfg.timers.hide_after_end_sec:
                self.reset(now, "结束文案停留")
            return

        if self.planted_mono is None:
            return

        # 截止时刻（红字归零）到了：**不在这里判爆炸**。
        # 红字的零是"拆弹最后时限"，真爆比它晚 explode_after_deadline_sec（实测 1.05s），
        # 而且最后一秒里拆弹仍可能成功（那会先收到 defused 报文）。
        # 所以本地兜底要等到"时限 + 真爆滞后 + 宽限"才敢判爆炸。
        zero = self.planted_mono + self.cfg.timers.bomb_seconds
        local_bang = zero + self.cfg.timers.explode_after_deadline_sec \
            + self.cfg.timers.local_bang_grace_sec
        if now >= local_bang:
            self._go_outcome(now, EXPLODED)
            return

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------

    def snapshot(self, now: float) -> Snapshot:
        explosion: float | None = None

        if self.state == EXPLODED:
            explosion = 0.0
        elif self.planted_mono is not None and self.state == ARMED:
            explosion = max(0.0, self.planted_mono + self.cfg.timers.bomb_seconds - now)

        status = STATUS_TEXT.get(self.state, self.state)

        return Snapshot(
            state=self.state,
            status_text=status,
            explosion_remaining=explosion,
            source=self.source,
            round_phase=self.round_phase,
            map_round=self.map_round,
            gsi_live=self.gsi_live(now),
            gsi_age=self.gsi_age(now),
            planted_mono=self.planted_mono,
            anchor_unknown=self.anchor_unknown,
        )
