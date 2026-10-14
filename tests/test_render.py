"""P1 自检：显示层规则。纯逻辑断言，不依赖 tkinter。

1 代的教训：显示 bug 只能靠截图肉眼看（行号算错、字号当像素用，
两个坑都是截图才发现的）。这里把"该画什么"变成可断言的东西，
截图只用来确认观感。

需求变更后显示层只剩**红字**（炸弹时限）：
原来测"红字 + 两条绿字"的 [3][4][5] 块整块删掉，改成红字自己的规则
（十进制阶梯、时限已到、T0 未知）。
"""

from __future__ import annotations

from c4timer.render import (
    DEADLINE_PASSED_TEXT,
    GRAY,
    RED,
    ROW_LABEL,
    UNKNOWN_TEXT,
    YELLOW,
    _fmt,
    build,
)
from c4timer.state import ARMED, DEFUSED, EXPLODED, IDLE, Snapshot

from .checks import Checks

_BASE = dict(
    state=IDLE,
    status_text="待机",
    explosion_remaining=None,
    source="gsi",
    round_phase="live",
    map_round=1,
    gsi_live=True,
    gsi_age=0.2,
    planted_mono=0.0,
    anchor_unknown=False,
)


def snap(**kw) -> Snapshot:
    data = dict(_BASE)
    data.update(kw)
    if "status_text" not in kw:
        data["status_text"] = {
            IDLE: "待机", ARMED: "C4 已安放",
            DEFUSED: "拆弹成功 · 炸弹已解除", EXPLODED: "炸弹已爆炸",
        }[data["state"]]
    return Snapshot(**data)


def run(verbose: bool = True) -> tuple[int, int, list[str]]:
    c = Checks()
    if verbose:
        print("=" * 72)
        print("P1 自检：显示层规则")
        print("=" * 72)

    if verbose:
        print("\n[1] 待机：默认不常驻（需求：待机只为观察程序用）")
    v = build(snap(state=IDLE), show_idle=False)
    c.ok(not v.visible, "show_idle=False -> 不可见")
    c.eq(v.rows, (), "show_idle=False -> 无行")
    v = build(snap(state=IDLE), show_idle=True)
    c.ok(v.visible, "show_idle=True -> 可见")
    c.eq(len(v.rows), 2, "待机两行（标题 + 等待安放）")
    c.ok(all(r.color == GRAY for r in v.rows), "待机文字为灰色")

    if verbose:
        print("\n[2] 已安放：只有一行红字，标签是「炸弹时限」")
    v = build(snap(state=ARMED, explosion_remaining=32.44))
    c.ok(v.visible, "已安放可见")
    c.eq(len(v.rows), 1, "只有一行（绿字已随需求删除）")
    c.eq(v.rows[0].label, ROW_LABEL, "标签是「炸弹时限」（不是「爆炸剩余」）")
    c.eq(v.rows[0].label, "炸弹时限", "标签文案 = 炸弹时限")
    c.eq(v.rows[0].value, "33", ">15s 用向上取整的整数（32.44 -> 33，口径=游戏「还剩第几秒」）")
    c.eq(v.rows[0].color, RED, "红色")
    c.ok(v.rows[0].big, "大字号")
    c.eq(v.status, "C4 已安放", "状态文案")
    c.ok(not snap(state=ARMED, explosion_remaining=32.44).deadline_passed,
         "还有 32s -> deadline_passed 为假")

    if verbose:
        print("\n[3] 时限已到（红字归零但还没确认爆炸）：灰色 0.0 + 到期文案")
    v = build(snap(state=ARMED, explosion_remaining=0.0))
    c.eq(len(v.rows), 1, "仍然只有一行")
    c.eq(v.rows[0].label, ROW_LABEL, "标签保持「炸弹时限」")
    c.eq(v.rows[0].value, "0.0", "红字夹在 0.0")
    c.eq(v.rows[0].color, GRAY, "时限已到 -> 数值转灰（不再告急红）")
    c.eq(v.status, DEADLINE_PASSED_TEXT, "状态行写「时限已到 · 即将爆炸」")
    c.eq(v.status, "时限已到 · 即将爆炸", "到期文案与需求一致")
    c.ok(snap(state=ARMED, explosion_remaining=0.0).deadline_passed,
         "deadline_passed 为真（状态机与显示层口径一致）")
    # 负数（时钟微抖）同样算已到
    c.eq(build(snap(state=ARMED, explosion_remaining=-0.4)).rows[0].value, "0.0",
         "负数也夹到 0.0")
    c.eq(build(snap(state=ARMED, explosion_remaining=-0.4)).status, DEADLINE_PASSED_TEXT,
         "负数同样算「时限已到」")

    if verbose:
        print("\n[4] T0 未知（回合中途启动）：不给数字，写「时限未知」")
    v = build(snap(state=ARMED, explosion_remaining=31.0, anchor_unknown=True))
    c.eq(len(v.rows), 1, "仍然只有一行")
    c.eq(v.rows[0].label, ROW_LABEL, "标签保持「炸弹时限」")
    c.eq(v.rows[0].value, UNKNOWN_TEXT, "数值写「时限未知」（宁可不给数字）")
    c.eq(v.rows[0].value, "时限未知", "未知文案 = 时限未知")
    c.eq(v.rows[0].color, YELLOW, "未知用黄色提示（不是红色告急）")
    c.ok(not v.rows[0].big, "未知不是大字号")
    c.ok("未看到安放时刻" in v.status, f"状态行说明原因（实际 {v.status!r}）")

    if verbose:
        print("\n[5] 终局文案")
    v = build(snap(state=DEFUSED))
    c.eq(v.status, "拆弹成功 · 炸弹已解除", "拆除成功文案")
    c.eq(v.rows[0].value, "--", "拆除成功后不再显示红字数值")
    c.eq(v.rows[0].color, GRAY, "红字行转灰")
    v = build(snap(state=EXPLODED, explosion_remaining=0.0))
    c.eq(v.status, "炸弹已爆炸", "爆炸文案")
    c.eq(v.rows[0].value, "0.0", "爆炸显示 0.0")
    c.eq(v.rows[0].color, GRAY, "爆炸后数值转灰（不再是跳动中的红字）")

    if verbose:
        print("\n[6] 调试角标")
    v = build(snap(state=ARMED, explosion_remaining=10.0), debug=False)
    c.eq(v.debug, "", "关调试时无角标")
    v = build(snap(state=ARMED, explosion_remaining=10.0), debug=True)
    c.ok("GSIOK" in v.debug, "开调试时显示 GSI 状态")
    c.ok("r1" in v.debug, "带回合号")

    if verbose:
        print("\n[7] _fmt 十进制阶梯（>15 整数向上取整，否则一位小数）")
    c.eq(_fmt(16.9), "17", "16.9 > 15 -> 向上取整 17（口径=游戏「还剩第几秒」）")
    c.eq(_fmt(16.0), "16", "16.0 -> 16（正好整数不被顶到 17）")
    c.eq(_fmt(39.9), "40", "39.9 -> 40（游戏这时也显示 40 —— 旧版向下取整会印 39）")
    c.eq(_fmt(15.01), "16", "15.01 > 15 -> 16（整数档，不是 15.0）")
    c.eq(_fmt(15.0), "15.0", "边界 15.0 不大于 15 -> 一位小数 15.0")
    c.eq(_fmt(9.94), "9.9", "9.94 -> 9.9（一位小数）")
    c.eq(_fmt(39.96), "40", "39.96 -> 40")
    c.eq(_fmt(0.0), "0.0", "0.0 -> 0.0")
    c.eq(_fmt(-3.0), "0.0", "负数夹到 0.0")
    c.eq(_fmt(None), "--", "None -> --")
    c.eq(_fmt(60.4, decimal_below_sec=100.0), "60.4",
         "阈值可配：decimal_below_sec=100 时 60.4 走小数分支")
    c.eq(_fmt(120.7, decimal_below_sec=100.0), "121",
         "阈值可配：120.7 > 100 -> 向上取整 121")

    if verbose:
        print("\n[8] 数值格式化边界（经 build 走一遍）")
    c.eq(build(snap(state=ARMED, explosion_remaining=None)).rows[0].value, "--",
         "红字为 None 时显示 --")
    c.eq(build(snap(state=ARMED, explosion_remaining=-3.0)).rows[0].value, "0.0",
         "红字负数夹到 0.0")
    c.eq(build(snap(state=ARMED, explosion_remaining=39.96)).rows[0].value, "40",
         "39.96 -> 40（整数档向上取整）")
    c.eq(build(snap(state=ARMED, explosion_remaining=14.96)).rows[0].value, "15.0",
         "14.96 -> 15.0（小数档四舍五入成 15.0，仍显示一位小数）")
    c.eq(build(snap(state=ARMED, explosion_remaining=10.0),
               decimal_below_sec=5.0).rows[0].value, "10",
         "decimal_below_sec 参数生效：10.0 > 5 -> 整数 10")

    if verbose:
        print("\n[9] GSI 断连提示（只在有事件时出现，待机不吵）")
    v = build(snap(state=ARMED, explosion_remaining=20.0, gsi_live=False, gsi_age=8.0))
    c.ok(v.gsi_lost, "ARMED + GSI 断连 -> 标记 gsi_lost")
    c.ok("GSI 断连" in v.status, f"状态行带提示（实际 {v.status!r}）")
    c.eq(v.status_color, YELLOW, "状态色变黄")
    c.eq(v.rows[0].value, "20", "红字照常显示（本地时钟继续走，不清零）")
    v = build(snap(state=IDLE, gsi_live=False), show_idle=True)
    c.ok(not v.gsi_lost, "待机时不提示（窗口本来隐藏，提示只是噪声）")
    v = build(snap(state=ARMED, explosion_remaining=20.0, gsi_live=True))
    c.ok(not v.gsi_lost, "GSI 正常时无提示")
    c.eq(v.status, "C4 已安放", "GSI 正常时状态行保持干净")
    # 时限已到时，断连提示要接在到期文案后面，不许把到期文案吃掉
    v = build(snap(state=ARMED, explosion_remaining=0.0, gsi_live=False))
    c.ok(v.status.startswith(DEADLINE_PASSED_TEXT),
         f"断连时到期文案仍然在最前面（实际 {v.status!r}）")

    if verbose:
        print("\n" + "=" * 72)
        print(f"P1 显示层自检完成: {'全部通过' if c.failed == 0 else '有失败'}  "
              f"[OK {c.passed} / X {c.failed}]")
        print("=" * 72)
    return c.passed, c.failed, c.failures


def main() -> int:
    _, failed, _ = run()
    return 1 if failed else 0
