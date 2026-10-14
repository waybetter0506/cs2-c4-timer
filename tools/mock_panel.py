#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""样品 v2：按拍板结果出图（C 深灰不透明 + 紧凑 + 下移避开 + 16pt 白数值 + 左右红条）。

⚠️ 一次性样品脚本：**不修改也不复制**产品的显示规则。
   - 只从 c4timer.render 读颜色常量、从 c4timer.hud 借色键与 Win32 样式；
   - 只往 runtime/ 写 mock_spec_*.png。

做法：开一块全屏窗口把真实实机截图当"桌面"，把真正的色键透明 Canvas 窗口压上去抓屏 ——
抓到的是真机制下的观感（ClearType 脏边、色键、红条都是真的）。

拍板规格（Q13~Q16）：
    底色     #2E2E36 不透明（在明亮画面上与官方 alpha≈0.63 的合成色几乎一致）
    红条     左右各 4px、纯 #FF0000
    数值     16pt 粗体、白色
    状态行   12pt、白色
    位置     水平居中、放在游戏自己那个通知框**下方**（避开重叠）
    尺寸     固定（按最宽的状态文案预留）—— 数字每秒变，面板不跟着抖
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from c4timer.hud import COLOR_KEY, _apply_ex_style  # noqa: E402
from c4timer.render import GRAY, GREEN, WHITE, YELLOW  # noqa: E402

OUT = ROOT / "runtime"
BG = OUT / "_bg_ingame.png"

# --- 拍板规格 ---
PANEL = "#2E2E36"
BAR = "#FF0000"
BAR_W = 4
VALUE_PT = 16
STATUS_PT = 12
PAD_X, PAD_Y = 16, 10
GAP = 4
FONT = "Microsoft YaHei UI"

# 游戏自己那个通知框在 1080p 下是 y=802..862；我们放在它下面，避开重叠
OFFICIAL_BOX = (757, 802, 411, 60)
POS_Y = OFFICIAL_BOX[1] + OFFICIAL_BOX[3] + 22      # = 884

# 五种状态（文案与颜色照 render.py 的显示规则，只是搬到面板里）
STATES = [
    ("armed_37", "C4 已安放", "炸弹时限 37", WHITE, WHITE),
    ("last15", "C4 已安放", "炸弹时限 9.9", WHITE, WHITE),
    ("deadline", "时限已到 · 即将爆炸", "炸弹时限 0.0", GRAY, WHITE),
    ("unknown", "C4 已安放 · 未看到安放时刻", "炸弹时限 时限未知", YELLOW, WHITE),
    ("defused", "拆弹成功 · 炸弹已解除", "炸弹时限 --", GRAY, GREEN),
]


class Sample:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=COLOR_KEY)
        try:
            self.root.wm_attributes("-transparentcolor", COLOR_KEY)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(self.root, bg=COLOR_KEY, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.root.update_idletasks()
        self.root.withdraw()

        self.SW = self.root.winfo_screenwidth()
        self.SH = self.root.winfo_screenheight()

        self.f_status = tkfont.Font(family=FONT, size=STATUS_PT)
        self.f_value = tkfont.Font(family=FONT, size=VALUE_PT, weight="bold")

        self.bg_top = tk.Toplevel(self.root)
        self.bg_top.overrideredirect(True)
        self.bg_top.geometry(f"{self.SW}x{self.SH}+0+0")
        self.photo = tk.PhotoImage(file=str(BG))
        tk.Label(self.bg_top, image=self.photo, bd=0, highlightthickness=0).pack()

        # 固定尺寸：按最宽/最高的状态文案预留，数字变化时面板不动
        w = max(max(self.f_status.measure(s), self.f_value.measure(v)) for _, s, v, _, _ in STATES)
        h = (self.f_status.metrics("linespace") + GAP
             + self.f_value.metrics("linespace") + 2 * PAD_Y)
        self.W = w + 2 * PAD_X + 2 * BAR_W
        self.H = h
        self.X = (self.SW - self.W) // 2
        print(f"固定面板尺寸 = {self.W}x{self.H} @ ({self.X},{POS_Y})")

    # ------------------------------------------------------------------

    def draw(self, status: str, value: str, vcolor: str, scolor: str) -> None:
        W, H = self.W, self.H
        self.canvas.delete("all")
        self.canvas.create_rectangle(0, 0, W - 1, H - 1, fill=PANEL, outline="")
        # 左右两条红竖条（官方只有左右，上/下没有）
        self.canvas.create_rectangle(0, 0, BAR_W - 1, H - 1, fill=BAR, outline="")
        self.canvas.create_rectangle(W - BAR_W, 0, W - 1, H - 1, fill=BAR, outline="")
        cx = W // 2
        self.canvas.create_text(cx, PAD_Y + self.f_status.metrics("linespace") // 2,
                                text=status, fill=scolor, anchor="c", font=self.f_status)
        self.canvas.create_text(cx, H - PAD_Y - self.f_value.metrics("linespace") // 2,
                                text=value, fill=vcolor, anchor="c", font=self.f_value)

    def show(self, x: int, y: int) -> None:
        self.canvas.configure(width=self.W, height=self.H)
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")
        self.root.deiconify()
        # 先抬背景、再抬面板（lower() 会把背景降到浏览器下面，那是错的）
        self.bg_top.attributes("-topmost", False)
        self.bg_top.lift()
        self.bg_top.update_idletasks()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        self.root.update()
        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass

    def grab(self, name: str, box: tuple[int, int, int, int] | None = None) -> Path:
        from PIL import ImageGrab

        self.root.update_idletasks()
        self.root.update()
        if box is None:
            pad = 20
            box = (max(0, self.X - pad), max(0, POS_Y - pad),
                   min(self.SW, self.X + self.W + pad), min(self.SH, POS_Y + self.H + pad))
        path = OUT / f"mock_spec_{name}.png"
        ImageGrab.grab(bbox=box, all_screens=False).save(path)
        print(f"  [OK] {path.name}")
        return path

    def done(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def stitch(name: str, items: list[tuple[str, Path]], font_path="C:/Windows/Fonts/msyh.ttc") -> None:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(font_path, 18)
    ims = [(label, Image.open(p).convert("RGB")) for label, p in items]
    w = max(im.width for _, im in ims)
    h = sum(im.height + 26 for _, im in ims)
    out = Image.new("RGB", (w, h), (18, 18, 22))
    d = ImageDraw.Draw(out)
    y = 0
    for label, im in ims:
        d.text((8, y + 4), label, fill=(235, 235, 235), font=font)
        y += 26
        out.paste(im, (0, y))
        y += im.height
    out.save(OUT / f"mock_spec_{name}.png")
    print(f"  [OK] mock_spec_{name}.png")


def main() -> int:
    if not BG.exists():
        print(f"[X] 缺少背景图 {BG}（先跑一次 tools\\mock_panel.py 会生成）")
        return 1
    OUT.mkdir(exist_ok=True)
    s = Sample()
    crops: list[tuple[str, Path]] = []

    # 1) 每个状态各抓一张（同一位置、同一固定尺寸）
    for name, status, value, vcolor, scolor in STATES:
        s.show(s.X, POS_Y)
        s.draw(status, value, vcolor, scolor)
        crops.append((f"{name}: {status} / {value}", s.grab(name)))

    # 2) 全屏一张：看位置关系（我们 vs 游戏自己的通知框）
    s.show(s.X, POS_Y)
    s.draw(*STATES[0][1:])
    s.grab("full", box=(0, 0, s.SW, s.SH))

    # 3) 数字跳动演示：40 -> 9.9 -> 0.0 面板尺寸/位置都不变
    digits: list[tuple[str, Path]] = []
    for tag, v in (("d40", "炸弹时限 40"), ("d99", "炸弹时限 9.9"), ("d00", "炸弹时限 0.0")):
        s.show(s.X, POS_Y)
        s.draw("C4 已安放", v, WHITE, WHITE)
        digits.append((v, s.grab(tag)))

    # 4) 暗背景对照：挪到小地图那片暗区（位置只为看对比，不是最终位置）
    dark: list[tuple[str, Path]] = []
    s.show(24, 24)
    s.draw(*STATES[0][1:])
    dark.append(("暗背景（小地图区）", s.grab("dark", box=(0, 0, 460, 120))))

    s.done()
    stitch("states", crops)
    stitch("digits", digits)
    stitch("dark", dark)
    print("\n看这几张：mock_spec_full / mock_spec_states / mock_spec_digits / mock_spec_dark")
    return 0


if __name__ == "__main__":
    sys.exit(main())
