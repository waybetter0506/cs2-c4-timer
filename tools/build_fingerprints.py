#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从自动落盘的安放片段生成指纹参考 —— 命令行壳。

逻辑本体在 `c4timer/buildrefs.py`（2026-09-19 搬进包里）。搬家的原因很实际：
发行版是编译过的 exe、**不能带任何 .py**（带了就等于把指纹算法送出去），
而"打完第一局自动建参考"是产品在全新机器上唯一能长出音频精度的路径，必须在 exe 里。
这个壳只是让 `python tools/build_fingerprints.py` 照旧能用，参数与行为都没变。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from c4timer import buildrefs  # noqa: E402


def main() -> int:
    # `--screen-negatives`：用"误报上限"筛掉会让误报变高的参考（默认关闭，见模块头注释）
    return int(buildrefs.build(screen_negatives="--screen-negatives" in sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
