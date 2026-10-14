#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""悬浮窗截图核对：把各个状态的 View 依次画出来并截图，供人眼确认。

用法:
    python tools/shot_hud.py            # 截图到 runtime/
    python tools/shot_hud.py --keep     # 截完不退出，方便手动看
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from c4timer.config import Config  # noqa: E402
from c4timer.hud import Hud, _VALUE_ON_PANEL  # noqa: E402
from c4timer.render import build  # noqa: E402
from c4timer.state import ARMED, DEFUSED, EXPLODED, IDLE, Snapshot  # noqa: E402

OUT = ROOT / "runtime"

_STATUS = {
    IDLE: "待机",
    ARMED: "C4 已安放",
    DEFUSED: "拆弹成功 · 炸弹已解除",
    EXPLODED: "炸弹已爆炸",
}


def snap(state: str, **kw) -> Snapshot:
    base = dict(
        state=state,
        status_text=_STATUS[state],
        explosion_remaining=None,
        source="gsi+comp",
        round_phase="live",
        map_round=2,
        gsi_live=True,
        gsi_age=0.12,
        planted_mono=0.0,
        anchor_unknown=False,
    )
    base.update(kw)
    return Snapshot(**base)


def scenarios(show_idle: bool) -> list[tuple[str, Snapshot]]:
    return [
        ("1_idle", snap(IDLE)),
        # 15 秒以上：整数（向上取整 = 游戏「还剩第几秒」）—— 32.9 应该显示 33
        ("2_armed_integer", snap(ARMED, explosion_remaining=32.9)),
        # 正好 15 秒附近：15.0 进入小数区
        ("3_fifteen", snap(ARMED, explosion_remaining=15.0)),
        # 最后 15 秒：一位小数
        ("4_last15", snap(ARMED, explosion_remaining=9.94)),
        # 时限已到、真爆前那约 1 秒
        ("5_deadline_passed", snap(ARMED, explosion_remaining=0.0)),
        # 回合中途启动、没看到安放 -> 不给数字
        ("6_anchor_unknown", snap(ARMED, anchor_unknown=True)),
        # GSI 断连
        ("7_gsi_lost", snap(ARMED, explosion_remaining=21.3, gsi_live=False, gsi_age=18.0)),
        ("8_defused", snap(DEFUSED)),
        ("9_exploded", snap(EXPLODED, explosion_remaining=0.0)),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="截完不退出")
    ap.add_argument("--debug", action="store_true", help="带上调试角标")
    args = ap.parse_args()

    cfg = Config()
    cfg.debug.print_events = False
    cfg.ui.show_idle = True          # 截图时把待机也画出来看看

    OUT.mkdir(exist_ok=True)
    try:
        from PIL import ImageGrab
    except ImportError:
        print("[X] 需要 Pillow 才能截图: pip install pillow")
        return 1

    hud = Hud(cfg, click_through=True)
    todo = scenarios(cfg.ui.show_idle)
    saved: list[str] = []

    def grab(name: str) -> None:
        hud.root.update_idletasks()
        hud.root.update()
        x, y = hud.root.winfo_x(), hud.root.winfo_y()
        w, h = hud.root.winfo_width(), hud.root.winfo_height()
        pad = 12
        box = (max(0, x - pad), max(0, y - pad), x + w + pad, y + h + pad)
        path = OUT / f"hud_{name}.png"
        ImageGrab.grab(bbox=box, all_screens=True).save(path)
        saved.append(str(path))
        print(f"  [OK] {path}  窗口 {w}x{h} @ ({x},{y})")

    def step(index: int) -> None:
        if index >= len(todo):
            print("\n截图完成:")
            for p in saved:
                print(f"  {p}")
            if not args.keep:
                hud.stop()
            return
        name, snapshot = todo[index]
        view = build(snapshot, show_idle=cfg.ui.show_idle, debug=args.debug,
                     decimal_below_sec=cfg.ui.decimal_below_sec)
        # 数值的**语义色**（render.py 给的）和**画在面板上的颜色**是两回事：
        # 深底上红字会画成白字、灰字会提亮（见 hud._VALUE_ON_PANEL）。
        # 两列都打出来，免得照着控制台核对却和截图对不上。
        src = view.rows[0].color if view.rows else "-"
        print(f"  {name:<20} 数值={view.rows[0].value if view.rows else '-':<10}"
              f" 语义色={src:<8} 面板色={_VALUE_ON_PANEL.get(src, src):<8} 状态={view.status}")
        hud.set_view(view)
        hud.root.after(60, lambda: grab(name))
        hud.root.after(260, lambda: step(index + 1))

    hud.set_view(build(todo[0][1], show_idle=True))
    hud.poll()
    hud.root.after(300, lambda: step(0))
    hud.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
