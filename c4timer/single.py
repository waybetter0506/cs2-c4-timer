"""单实例保护：Windows 命名互斥体。

为什么不用"端口是否被占用"来判断重复启动：
端口是**资源**，不是**身份**。本项目的采集工具与主程序用的是同一个 GSI 端口
（30077），那是资源冲突，不是"同一个程序启动了两次"。用端口判断会把
"采集工具在跑"误报成"程序已在运行"。

命名互斥体由内核持有，进程一死就自动释放，不会留下需要手工清理的锁文件
（锁文件的经典问题是崩溃后留下一个"永远被占用"的假锁）。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

DEFAULT_NAME = "Local\\c4timer-single-instance"


class SingleInstance:
    """`with SingleInstance() as si: if not si.acquire(): ...`

    `Local\` 前缀 = 每个登录会话一个，不会因为"另一个用户"而互相挡住。
    """

    def __init__(self, name: str = DEFAULT_NAME) -> None:
        self.name = name
        self.already_running = False
        self._handle = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        ctypes.set_last_error(0)
        handle = _kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            return False
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            # 别人已经持有这个名字的互斥体
            _kernel32.CloseHandle(handle)
            self.already_running = True
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
