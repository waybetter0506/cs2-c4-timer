"""悬浮窗：无边框、置顶、色键透明的**面板**，鼠标穿透、不抢焦点。

Win32 手法与 1 代一致（已实测截图确认）：
    WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    + Tk 的 -transparentcolor 色键负责面板以外全透明

面板样式（对齐游戏自己那个"炸弹已被安放"通知框的观感）：
    深灰底 + 左右各 4px 纯红竖条 + 居中两行白字（状态行小、数值行大）。

⚠️ 底色是**不透明**的，这是刻意的：色键透明只有"全透 / 全不透"，做不出 alpha，
   而"文字后面一块半透明黑底"要真 alpha 就得换 UpdateLayeredWindow 重写渲染
   （见 docs/HUD界面说明.md 第七节）。之所以不显黑：官方那块面板的合成色实测 ≈ (49,47,60)，
   固定深灰 #2E2E36 = (46,46,54) 在明亮画面上与它几乎一致；代价是暗背景上偏亮。

显示规则仍然全部住在 render.py（一行代码都没搬过来）。这里只做两件事：
    1. 把 Row/View 摆成两行居中（标签+数值合成一行）；
    2. **颜色主题映射**：深底上红字不如白字清楚，所以数值 RED->WHITE；
       灰字（0.0 / --）压在深底会糊，提亮成 #C7C7CC。语义色本身不动。

鼠标穿透 与 可拖动 天生冲突：穿透之后窗口收不到鼠标事件。
所以拖动只在"编辑模式"下开启（click_through=False），
正常运行时是穿透的、拖不动 —— 位置改一次就存进 config.json。
"""

from __future__ import annotations

import ctypes
import threading
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes

from .config import Config, save as save_config
from .render import (ANCHOR_UNKNOWN_TEXT, DEADLINE_PASSED_TEXT, GRAY, RED, ROW_LABEL,
                     UNKNOWN_TEXT, WHITE, View)
from .state import STATUS_TEXT

# 色键：面板上凡是这个颜色的像素一律透明。选一个几乎不可能出现在文字里的深色。
COLOR_KEY = "#010203"

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

# 面板内边距/红条宽度（逻辑像素，实际会乘 DPI 缩放）
PAD_X = 16
PAD_Y = 10
GAP = 4
BAR_W = 4
# 没拖过时的默认位置：水平居中、纵向 82% 处（对齐游戏自己那个通知框下方）
DEFAULT_Y_RATIO = 0.82

# 颜色主题映射（只影响"画在面板上"的颜色，改的是观感不是规则）
_VALUE_ON_PANEL = {RED: WHITE, GRAY: "#C7C7CC"}
_STATUS_ON_PANEL = {GRAY: "#C7C7CC"}

_user32 = ctypes.WinDLL("user32", use_last_error=True)
try:  # 64 位专用
    _get_long = _user32.GetWindowLongPtrW
    _set_long = _user32.SetWindowLongPtrW
except AttributeError:  # 32 位回退
    _get_long = _user32.GetWindowLongW
    _set_long = _user32.SetWindowLongW
_get_long.argtypes = [wintypes.HWND, ctypes.c_int]
_get_long.restype = ctypes.c_ssize_t
_set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_set_long.restype = ctypes.c_ssize_t


def enable_dpi_awareness() -> None:
    """声明进程 DPI 感知。**必须在创建任何 Tk 窗口之前调用**（之后调会失败）。

    不声明的话 Windows 会把窗口当成 96dpi 再整体位图放大 —— 125%/150% 缩放下
    字全是糊的（本机 100% 时是空操作，所以这条只能靠换机器验证）。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _apply_ex_style(hwnd: int, click_through: bool) -> None:
    style = _get_long(wintypes.HWND(hwnd), GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    if click_through:
        style |= WS_EX_TRANSPARENT
    else:
        style &= ~WS_EX_TRANSPARENT
    _set_long(wintypes.HWND(hwnd), GWL_EXSTYLE, style)


class Hud:
    """必须由主线程创建与运行（tkinter 的硬约束）。"""

    def __init__(self, cfg: Config, click_through: bool | None = None) -> None:
        self.cfg = cfg
        # click_through=None -> 用配置默认（穿透）；编辑模式显式传 False 才能拖动
        self.click_through = True if click_through is None else bool(click_through)
        self._lock = threading.Lock()
        self._view = View(visible=False, status="", status_color=WHITE, rows=())
        self._saved_geometry = (None, None)
        self._drag = (0, 0)
        self._last_size = (0, 0)
        # 上一次真正画上去的 View。内容没变就不碰窗口 ——
        # 每次 poll 都 configure+geometry 会让窗口每 50ms 重排一次，
        # 读秒就会一顿一顿的（实机反馈）。
        self._applied: View | None = None
        self.applies = 0          # 真正重画的次数（自检用）
        self._items: dict[str, int] | None = None
        self._items_key: tuple | None = None

        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("c4timer")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=COLOR_KEY)
        try:
            self.root.wm_attributes("-transparentcolor", COLOR_KEY)
        except tk.TclError:
            pass
        self.root.withdraw()

        # DPI 缩放：字号用的是**磅值**，Tk 会自己按 DPI 放大；只有像素量（留白、红条）
        # 需要我们自己乘。100% 缩放时 self.dpi = 1.0，一切照旧。
        self.dpi = max(1.0, float(self.root.winfo_fpixels("1i")) / 96.0)
        self.pad_x = max(1, int(round(PAD_X * self.dpi)))
        self.pad_y = max(1, int(round(PAD_Y * self.dpi)))
        self.gap = max(1, int(round(GAP * self.dpi)))
        self.bar_w = max(0, int(round(float(cfg.ui.panel_bar_width) * self.dpi)))

        ui = cfg.ui
        scale = max(0.5, float(ui.scale))
        self.f_status = tkfont.Font(
            family=ui.font_family, size=max(8, int(ui.sub_font_size * scale))
        )
        self.f_value = tkfont.Font(
            family=ui.font_family, size=max(8, int(ui.font_size * scale)), weight="bold"
        )
        self.f_debug = tkfont.Font(family=ui.mono_font, size=max(7, int(11 * scale)))
        self.panel_bg = ui.panel_color
        self.bar_color = ui.panel_bar_color

        self.canvas = tk.Canvas(self.root, bg=COLOR_KEY, highlightthickness=0, bd=0)
        self.canvas.pack()

        # 面板尺寸：宽度一次预留到位（数字每秒变，面板绝不能跟着抖），
        # 预留依据是"可能同时出现的最长文案"；万一出现更长的（例如叠加了 GSI 断连后缀）
        # 就**只增不减**地变宽一次，避免来回抽动。
        # 面板尺寸：宽度按"可能同时出现的最长文案"预留到位（数字每秒变，面板绝不能跟着抖）。
        # 出现比预留还长的文案时（状态行叠加 GSI 断连、待机那种两行数据）就临时撑开，
        # 文案收回去再回到预留宽度 —— 预留宽度是**地板**，所以读秒期间一次都不会变。
        self._panel_h = self._line_h()
        self._seed_w = self._seed_width()
        self._panel_w = self._seed_w

        self.root.update_idletasks()
        self._apply_win32()
        if not self.click_through:
            self._enable_drag()

    # ------------------------------------------------------------------

    def _line_h(self) -> int:
        """两行文字 + 上下留白 = 面板高度。"""
        return (self.f_status.metrics("linespace") + self.gap
                + self.f_value.metrics("linespace") + 2 * self.pad_y)

    def _seed_width(self) -> int:
        """面板固定宽度：按 render/state 里**实际可能出现的文案**量出来的，不写死数字。

        故意不算"状态行 + · GSI 断连"那种叠加（那会比正常宽 90px），
        真出现时由 _apply 临时撑开。
        """
        statuses = list(STATUS_TEXT.values()) + [DEADLINE_PASSED_TEXT, ANCHOR_UNKNOWN_TEXT]
        widths = [self.f_status.measure(s) for s in statuses]
        widths.append(self.f_value.measure(f"{ROW_LABEL} {UNKNOWN_TEXT}"))
        widths.append(self.f_value.measure(f"{ROW_LABEL} 00"))
        return max(widths) + 2 * self.pad_x + 2 * self.bar_w

    def _apply_win32(self) -> None:
        try:
            hwnd = self.root.winfo_id()
            # winfo_id 给的是子窗口句柄，要往上找到顶层窗口
            parent = _user32.GetParent(wintypes.HWND(hwnd))
            if parent:
                hwnd = parent
            self._hwnd = hwnd
            _apply_ex_style(hwnd, self.click_through)
        except Exception:
            self._hwnd = None

    # ------------------------------------------------------------------
    # 拖动（仅编辑模式）
    # ------------------------------------------------------------------

    def _enable_drag(self) -> None:
        def start(event):
            self._drag = (event.x_root - self.root.winfo_x(),
                          event.y_root - self.root.winfo_y())

        def move(event):
            x = event.x_root - self._drag[0]
            y = event.y_root - self._drag[1]
            self.root.geometry(f"+{x}+{y}")

        def end(_event):
            self._persist_position()

        for widget in (self.root, self.canvas):
            widget.bind("<Button-1>", start)
            widget.bind("<B1-Motion>", move)
            widget.bind("<ButtonRelease-1>", end)

    def _persist_position(self) -> None:
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if (x, y) == self._saved_geometry:
            return
        self._saved_geometry = (x, y)
        self.cfg.ui.pos_x, self.cfg.ui.pos_y = int(x), int(y)
        try:
            save_config(self.cfg)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------

    def set_view(self, view: View) -> None:
        """线程安全：GSI/音频线程往这里塞最新的 View。"""
        with self._lock:
            self._view = view

    def _take_view(self) -> View:
        with self._lock:
            return self._view

    @staticmethod
    def _value_line(view: View) -> tuple[str, str]:
        """把 rows 拼成一行"标签 数值"，颜色取第一行的语义色。

        现在只有一行数据行（绿字已删）；待机那种两行只会在 --show-idle/截图时出现，
        拼在一起显示即可。
        """
        if not view.rows:
            return "", WHITE
        text = "   ".join(f"{r.label} {r.value}".strip() for r in view.rows)
        return text, view.rows[0].color

    def _apply(self, view: View) -> None:
        # 内容一模一样就什么都不做。窗口每帧重排是"读秒一顿一顿"的元凶：
        # tkinter 的 geometry()+update_idletasks() 会强制重新布局并 SetWindowPos。
        if view == self._applied and not self._needs_deiconify():
            return
        self._applied = view
        self.applies += 1

        if not view.visible:
            if self.root.state() != "withdrawn":
                self.root.withdraw()
            return

        value, value_src = self._value_line(view)
        status_color = _STATUS_ON_PANEL.get(view.status_color, view.status_color)
        value_color = _VALUE_ON_PANEL.get(value_src, value_src)

        needed = max(self.f_status.measure(view.status),
                     self.f_value.measure(value)) + 2 * self.pad_x + 2 * self.bar_w
        self._panel_w = max(self._seed_w, needed)   # 预留宽度是地板，见 __init__ 的注释

        self._draw(view.status, value, value_color, status_color, view.debug)

        height = self._panel_h + (self.f_debug.metrics("linespace") if view.debug else 0)
        x, y = self._resolve_position(self._panel_w, height)
        # 尺寸和位置都没变就别再调 geometry()（那等于每帧搬一次窗口）
        if (self._panel_w, height, x, y) != self._last_size:
            self._last_size = (self._panel_w, height, x, y)
            self.root.geometry(f"{self._panel_w}x{height}+{x}+{y}")
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self._apply_win32()

    def _draw(self, status: str, value: str, value_color: str, status_color: str,
              debug: str) -> None:
        """只改文字与颜色；尺寸没变就不重建画布上的元素（避免每秒重画整个窗口）。"""
        width = self._panel_w
        debug_h = self.f_debug.metrics("linespace") if debug else 0
        key = (width, self._panel_h, debug_h)
        c = self.canvas
        if key != self._items_key:
            c.configure(width=width, height=self._panel_h + debug_h)
            c.delete("all")
            items: dict[str, int] = {}
            items["bg"] = c.create_rectangle(0, 0, width - 1, self._panel_h - 1,
                                             fill=self.panel_bg, outline="")
            if self.bar_w:
                items["bar_l"] = c.create_rectangle(0, 0, self.bar_w - 1, self._panel_h - 1,
                                                    fill=self.bar_color, outline="")
                items["bar_r"] = c.create_rectangle(width - self.bar_w, 0, width - 1,
                                                    self._panel_h - 1,
                                                    fill=self.bar_color, outline="")
            items["status"] = c.create_text(
                width // 2, self.pad_y + self.f_status.metrics("linespace") // 2,
                text="", anchor="c", font=self.f_status)
            items["value"] = c.create_text(
                width // 2, self._panel_h - self.pad_y - self.f_value.metrics("linespace") // 2,
                text="", anchor="c", font=self.f_value)
            if debug_h:
                items["debug"] = c.create_text(
                    self.pad_x, self._panel_h + 2, text="", anchor="nw",
                    font=self.f_debug, fill=GRAY, width=max(240, width - 2 * self.pad_x))
            self._items = items
            self._items_key = key
        items = self._items or {}
        c.itemconfigure(items["status"], text=status, fill=status_color)
        c.itemconfigure(items["value"], text=value, fill=value_color)
        if "debug" in items:
            c.itemconfigure(items["debug"], text=debug)

    def _needs_deiconify(self) -> bool:
        """窗口被收起来了就必须重画（否则内容不变时会一直藏着不出来）。"""
        try:
            return self.root.state() == "withdrawn"
        except tk.TclError:
            return False

    def _resolve_position(self, width: int, height: int) -> tuple[int, int]:
        """位置来自配置（编辑模式拖出来的绝对坐标），默认水平居中、纵向 82% 处。

        坐标是绝对像素，换显示器/改分辨率后可能整个跑到屏幕外 ——
        而这个窗口运行时是穿透、点不到的，跑出去就再也找不回来，
        所以这里统一拉回可见区域。
        """
        ui = self.cfg.ui
        if ui.pos_x is not None and ui.pos_y is not None:
            x, y = int(ui.pos_x), int(ui.pos_y)
        else:
            screen_h = self.root.winfo_screenheight()
            x = (self.root.winfo_screenwidth() - width) // 2
            y = int(round(screen_h * DEFAULT_Y_RATIO))
        return self._clamp(x, y, width, height)

    def _clamp(self, x: int, y: int, width: int, height: int) -> tuple[int, int]:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        return (min(max(0, x), max(0, screen_w - width)),
                min(max(0, y), max(0, screen_h - height)))

    # ------------------------------------------------------------------

    def poll(self, interval_ms: int = 10) -> None:
        def tick():
            # Ctrl+C 打断 Tk 的回调时，异常会从 tkinter 内部穿出来打一大段 traceback
            # （实机 2026-09-18：用户按 Ctrl+C 退出，屏幕上出现看起来像崩溃的调用栈）。
            # 退出路径上的中断/窗口已销毁都当"该收摊了"处理，不再重排下一帧。
            try:
                self._apply(self._take_view())
            except (KeyboardInterrupt, tk.TclError):
                return
            try:
                self.root.after(interval_ms, tick)
            except (KeyboardInterrupt, tk.TclError):
                return
        self.root.after(interval_ms, tick)

    def run(self) -> None:
        self.poll()
        self.root.mainloop()

    def stop(self) -> None:
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass
