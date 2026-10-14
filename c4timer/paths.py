"""统一的路径解析：程序根目录与运行期数据目录。

为什么要单独一个模块（2026-09-19，为打包成 exe 做准备）
--------------------------------------------------
以前 `config.py` / `tuning.py` / `refstats.py` / `fixtures.py` 各写了一份

    ROOT = Path(__file__).resolve().parent.parent

源码运行时这四份算出来是同一个目录（仓库根），所以一直没出问题。但打包成 exe 之后
`__file__` 指向的是**打包内部**的路径，算出来的目录并不是用户放程序的那个目录，
运行期数据（参考指纹、统计、日志、录音落盘）就会写到别处、或者根本写不进去。

所以收口到这里：**冻结运行时用 exe 所在目录，源码运行时仍然是仓库根**。
源码那条路算出来的值和以前一模一样，行为零变化。

为什么用 `sys.argv[0]` 而不是 `__compiled__.containing_dir`：后者在 onefile 模式下
是解包出来的临时目录，程序一退出连目录带数据一起消失，不能用来放用户数据。
`sys.argv[0]` 在 standalone / onefile / PyInstaller 三种情况下都是**用户双击的那个 exe**，
正好是"绿色软件"该用的位置。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:                                  # 源码运行时的仓库根；冻结后这个值不会被用到
    _SOURCE_ROOT = Path(__file__).resolve().parent.parent
except (NameError, OSError, ValueError):        # pragma: no cover - 极端环境兜底
    _SOURCE_ROOT = Path.cwd()

# `sys.frozen` 是 PyInstaller / cx_Freeze 的约定，`__compiled__` 是 Nuitka 注入到
# 每个已编译模块 globals 里的对象。两个都认，是为了打包器换人时这里不用改。
_FROZEN = bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def is_frozen() -> bool:
    """当前是不是打包后的可执行文件在跑。"""
    return _FROZEN


def app_root() -> Path:
    """程序所在目录：冻结时是 exe 所在目录，源码运行时是仓库根。

    自检里模拟冻结的做法是直接改模块全局 `_FROZEN` 和 `sys.argv`（见 tests/test_paths.py），
    所以这里读的是**调用时**的全局值，不在导入时算死。
    """
    if not _FROZEN:
        return _SOURCE_ROOT
    for cand in ((sys.argv[0] if sys.argv else ""), getattr(sys, "executable", "")):
        if not cand:
            continue
        try:
            p = Path(cand).expanduser().resolve()
        except (OSError, ValueError):
            continue
        if p.name and p.parent.is_dir():
            return p.parent
    return Path.cwd()                 # 兜底：算不出来也别崩，至少给个能写的地方


def runtime_dir() -> Path:
    """运行期数据目录：参考指纹、统计文件、日志、录音落盘都在这儿。"""
    return app_root() / "runtime"
