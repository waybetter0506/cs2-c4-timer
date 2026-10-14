"""显示层：Snapshot -> 要画什么（行、颜色、文案）。纯逻辑，不依赖 tkinter。

把它和 hud.py 分开，是为了让"显示规则"可以被测试，
而不是只能靠截图肉眼看（1 代有两个 bug 只能靠截图发现，代价很高）。

需求变更后这里只剩**红字**（炸弹时限）：拆弹的绿字两条已移除。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .state import ARMED, DEFUSED, EXPLODED, IDLE, Snapshot

# 颜色
RED = "#FF3B30"
GREEN = "#32D74B"
GRAY = "#8E8E93"
WHITE = "#FFFFFF"
YELLOW = "#FFD60A"

# 这一行的标签。语义是"拆弹最后时限"（= 游戏自己那个 40 秒计时器归零），
# 不是真正的爆炸瞬间（真爆比它晚约 1 秒）—— 所以不能叫"爆炸剩余"。
ROW_LABEL = "炸弹时限"
# 时限到了、但还没确认爆炸时的状态文案
DEADLINE_PASSED_TEXT = "时限已到 · 即将爆炸"
# T0 不可信（回合中途才启动、没看到安放那一刻）时的状态行文案。
# 它是所有状态文案里**最长**的一条，hud.py 靠它预留面板宽度 ——
# 所以提成常量，别再退回内联字面量（否则面板宽度会悄悄算小）。
ANCHOR_UNKNOWN_TEXT = "C4 已安放 · 未看到安放时刻"
# T0 不可信时的数值
UNKNOWN_TEXT = "时限未知"


@dataclass(frozen=True)
class Row:
    """一行：左边标签 + 右边数值。

    `big` 是历史字段：hud.py **只在**字号规则里用 color/label，从不读它；
    现在只有 `test_render.py` 在读（两处断言）。保留是为了不动既有断言，
    确实要清理就连测试一起删（视觉零影响）。
    """

    label: str
    value: str
    color: str
    big: bool = False


@dataclass(frozen=True)
class View:
    visible: bool
    status: str
    status_color: str
    rows: tuple[Row, ...]
    # 调试角标（一行小字，显示 GSI 状态/回合/来源）
    debug: str = ""
    # GSI 已断连（只在有事件时提示；待机时不吵）
    gsi_lost: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.status and not self.rows


# 结束文案停留期间/终局的文案（与 state.py 的 status_text 一致，这里再兜一层）
_STATUS_COLOR = {
    IDLE: GRAY,
    ARMED: WHITE,
    DEFUSED: GREEN,
    EXPLODED: RED,
}


def build(snapshot: Snapshot, show_idle: bool = False, debug: bool = False,
          decimal_below_sec: float = 15.0) -> View:
    rows: list[Row] = []
    # 锚点不可信时**不声称**"时限已到"：数字都不知道，时序也没意义。
    # （两处判断必须用同一个量，否则会出现"数值写时限未知、状态行写时限已到"的自相矛盾。）
    deadline_passed = snapshot.deadline_passed and not snapshot.anchor_unknown

    if snapshot.state == IDLE:
        if not show_idle:
            return View(visible=False, status="", status_color=GRAY, rows=())
        rows.append(Row("C4 计时器", "待机", GRAY))
        rows.append(Row("", "等待安放炸弹", GRAY))

    elif snapshot.state == ARMED:
        if snapshot.anchor_unknown:
            # 中途启动/重连：不知道游戏计时器已经走了多久，宁可不给数字
            rows.append(Row(ROW_LABEL, UNKNOWN_TEXT, YELLOW, big=False))
        elif deadline_passed:
            # 时限已到、真爆前的那约 1 秒：灰 0.0，不再倒数
            rows.append(Row(ROW_LABEL, "0.0", GRAY, big=True))
        else:
            rows.append(Row(ROW_LABEL,
                            _fmt(snapshot.explosion_remaining, decimal_below_sec),
                            RED, big=True))

    elif snapshot.state == DEFUSED:
        rows.append(Row(ROW_LABEL, "--", GRAY))

    elif snapshot.state == EXPLODED:
        rows.append(Row(ROW_LABEL, "0.0", GRAY, big=True))

    # GSI 断连提示：只在有事件（非待机）时出现。
    # 待机时窗口本来就隐藏，再挂一条提示只是噪声。
    gsi_lost = (not snapshot.gsi_live) and snapshot.state != IDLE
    if snapshot.state == ARMED and deadline_passed:
        status = DEADLINE_PASSED_TEXT
    elif snapshot.state == ARMED and snapshot.anchor_unknown:
        status = ANCHOR_UNKNOWN_TEXT
    else:
        status = snapshot.status_text if snapshot.visible else ""
    status_color = _STATUS_COLOR.get(snapshot.state, WHITE)
    if gsi_lost:
        status = f"{status} · GSI 断连"
        status_color = YELLOW

    dbg = ""
    if debug:
        age = "--" if snapshot.gsi_age is None else f"{snapshot.gsi_age:.1f}s"
        rnd = snapshot.map_round if snapshot.map_round is not None else "-"
        dbg = (
            f"GSI{'OK' if snapshot.gsi_live else '断'} {age} | "
            f"{snapshot.round_phase or '-'} r{rnd} | src={snapshot.source}"
        )

    return View(
        visible=snapshot.visible or show_idle,
        status=status,
        status_color=status_color,
        rows=tuple(rows),
        debug=dbg,
        gsi_lost=gsi_lost,
    )


def _fmt(value: float | None, decimal_below_sec: float = 15.0) -> str:
    """剩余时间 -> 字符串。

    > decimal_below_sec 用**向上取整**的整数，口径是"还剩第几秒"——
    和游戏自己那个 C4 计时器一致：还剩 39.9 秒时游戏显示 40，还剩 19.9 秒时显示 20。

    为什么从"向下取整"改成"向上取整"（2026-09-15 实机反馈）：
    用户报"倒计时比实际快，固定差 1 秒，活着和观战都一样"。
    观战那一路我们的钟是**锁死在服务端倒计时**上的（同时刻 CALIB 误差只有 ±0.07 秒），
    所以那 1 秒不可能是计时误差，只能是**数字印低了**：旧代码 `int(值)` = 向下取整，
    于是整段倒数永远比游戏少 1（19.9 我们印 19、游戏印 20），
    并且在整数档里"显示 0 的那一秒"也提前了一秒。
    最后 decimal_below_sec 秒给一位小数（比游戏的整数更细，这是刻意要的精度）。
    """
    if value is None:
        return "--"
    value = max(0.0, value)
    if value > decimal_below_sec:
        # 1e-9：正好落在整数上时别被浮点误差顶到上一秒
        return str(math.ceil(value - 1e-9))
    return f"{value:.1f}"
