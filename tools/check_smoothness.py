"""读秒流畅度实测：把真实 HUD 里的数字变化时刻全记下来，看间隔抖动。

"一卡一卡"= 数字变化的间隔忽长忽短。这个工具直接量它：
    python tools/check_smoothness.py            # 跑 3 秒
    python tools/check_smoothness.py --seconds 5
退出码 0 = 间隔抖动在阈值内。
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from c4timer.app import C4TimerApp                                    # noqa: E402
from c4timer.config import Config                                     # noqa: E402
from c4timer.gsi import GsiSample                                     # noqa: E402
from c4timer.hud import Hud                                           # noqa: E402


def base_sample(t: float, planted: bool = True) -> GsiSample:
    return GsiSample(
        recv_mono=t, recv_wall="", observer=False, observed_steamid="me",
        self_steamid="me", observing_other=False, round_phase="live", map_round=1,
        map_name="de_dust2", map_mode="competitive",
        bomb_state="planted" if planted else None,
        bomb_state_raw=None,
        round_bomb="planted" if planted else None,
        explosion_remaining=None,
        defuse_remaining=None, pcd_phase=None, pcd_ends_in=None, bomb_countdown=None,
        bomb_player=None, defuser_kit=None, top_keys=(), allplayers_count=0,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=6.0,
                    help="采样时长；红字每秒变一次，太短了量不出抖动")
    ap.add_argument("--max-jitter-ms", type=float, default=60.0,
                    help="相邻两次数字变化的间隔，相对中位数允许的最大偏差。"
                         "别调到 25ms 以下：ticker 20ms + HUD poll 10ms 的量化本身就有 ±30ms 的底噪")
    args = ap.parse_args()

    cfg = Config()
    cfg.debug.no_log = True          # 不污染 runtime 日志
    cfg.ui.show_idle = True
    app = C4TimerApp(cfg, enable_hud=True, debug=False)
    app.make_hud()                   # tkinter 必须在主线程建
    assert app.hud is not None
    app.start()                      # 起 GSI + 50Hz ticker
    time.sleep(0.3)

    now = time.monotonic()
    # ⚠️ 必须先喂一包"回合已开始、炸弹还没安放"的包：状态机靠"本回合见过安放之前的报文"
    #    判断 T0 可不可信，只喂一包 planted 会被当成"中途启动"，界面就写「时限未知」不动了。
    app._on_gsi(base_sample(now - 1.0, planted=False))
    time.sleep(0.05)
    app._on_gsi(base_sample(now))    # 再喂"已安放"，红字开始走
    time.sleep(0.2)

    hud: Hud = app.hud
    hud.poll()                       # 排上 HUD 自己的定时器（root.update() 会驱动它）

    def current_value() -> str:
        """读面板上"数值行"当前的文字（面板画在 Canvas 上，不再是 Label）。"""
        items = hud._items or {}
        if "value" not in items:
            return ""
        return str(hud.canvas.itemcget(items["value"], "text"))

    # 预热：等面板上出现读数 **并跳过一次秒** 再开始计时。
    # 第一次变化是"面板出现"（它可能发生在一秒的中间，于是后面那个间隔只有几百 ms——
    # 那是相位噪声不是抖动），第二次变化才是真正的跳秒；从它之后每个间隔都该正好 1000ms。
    seen, prev = 0, current_value()
    deadline = time.monotonic() + 4.0
    while seen < 2 and time.monotonic() < deadline:
        hud.root.update()
        now_val = current_value()
        if now_val != prev:
            seen, prev = seen + 1, now_val
        time.sleep(0.002)

    stamps: list[tuple[float, str]] = []
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        hud.root.update()             # 必须在主线程里驱动 tk
        txt = current_value()
        if not stamps or stamps[-1][1] != txt:
            stamps.append((time.monotonic(), txt))
        time.sleep(0.002)
    hud.stop()
    app.stop()

    if len(stamps) < 5:
        print(f"[X] 只采到 {len(stamps)} 次数字变化，没测出来（把 --seconds 调大）")
        return 1
    gaps = [b[0] - a[0] for a, b in zip(stamps, stamps[1:])]
    med = sorted(gaps)[len(gaps) // 2]
    worst = max(abs(g - med) for g in gaps)
    print(f"数字变化 {len(stamps)} 次   中位间隔 {med * 1000:.1f}ms   "
          f"最大偏差 {worst * 1000:.1f}ms")
    print("逐次: " + " ".join(f"{t[1]}@{g * 1000:.0f}ms"
                              for t, g in list(zip(stamps[1:], gaps))[:12]))
    ok = worst * 1000 <= args.max_jitter_ms
    print(f"[{'OK' if ok else 'X'}] 抖动量 {worst * 1000:.1f}ms "
          f"{'<=' if ok else '>'} 阈值 {args.max_jitter_ms:.0f}ms")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
