#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包入口（PyInstaller 用）。

不带参数时等价于 `python -m c4timer run`，这样用户双击 exe 也能跑。
由 `tools/package_release.py` 在构建时使用，不参与产品运行链路。
"""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    # 打好的 exe 里如果将来用到多进程，没有这一行会反复重启自己。
    multiprocessing.freeze_support()
    from c4timer.__main__ import main

    sys.exit(main(sys.argv[1:] or ["run"]))
