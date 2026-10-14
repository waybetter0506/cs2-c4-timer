"""运行期日志：把 GSI、音频分数、状态迁移写到一个文件里，供事后诊断。

为什么要落盘而不是只打控制台：
"对拆包没有反应"这类问题**必须看现场数据**才能定位 —— 是没听到声音？
听到了但相似度不够？相似度够但被门控挡了？还是状态机没接住？
只靠用户口述无法区分，而这几种原因的修法完全不同。

日志里的关键一行是**门控期间见过的最高相似度**：
  - 一直是 0.3 左右  -> 音频根本没听到拆弹声（设备/音量/距离）
  - 到了 0.6~0.75    -> 听到了，但没到阈值（环境变了，需要重标定）
  - 到过 0.8+ 却没触发 -> 持续时间不够，或状态机/门控有问题
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024


class SessionLog:
    def __init__(self, path: Path | str, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.written = 0
        self.error: str | None = None
        if not enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                self.path.replace(self.path.with_suffix(".log.1"))
            self._fh = self._open()
        except OSError as exc:
            self.enabled = False
            self.error = repr(exc)

    def _open(self):
        """打开（追加）。只读属性会让 append 直接 PermissionError —— 老版本于是
        **静默**把日志关掉：实机为此白丢了三轮数据，我还据此误判"程序没在跑"
        （见 `docs/实机反馈与待办.md` 第十三节）。所以这里先尝试清掉只读再开一次。
        """
        try:
            return self.path.open("a", encoding="utf-8")
        except PermissionError:
            os.chmod(self.path, stat.S_IWRITE)      # Windows：去掉只读属性
            return self.path.open("a", encoding="utf-8")

    def write(self, tag: str, text: str) -> None:
        if not self.enabled:
            return
        stamp = time.strftime("%H:%M:%S") + f".{int(time.time() % 1 * 1000):03d}"
        try:
            self._fh.write(f"{stamp}  {tag:<6} {text}\n")
            self._fh.flush()
            self.written += 1
        except OSError as exc:
            self.enabled = False
            self.error = repr(exc)

    def close(self) -> None:
        if self.enabled:
            try:
                self._fh.close()
            except OSError:
                pass
            self.enabled = False
