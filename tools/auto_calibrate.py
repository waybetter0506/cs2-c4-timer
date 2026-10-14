#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动标定（供 `启动.bat` 调用）—— 命令行壳。

逻辑本体在 `c4timer/autocal.py`（2026-09-19 搬进包里，原因同 `build_fingerprints.py`：
发行版不能带 .py，而自动标定必须在 exe 里）。参数与行为都没变。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from c4timer import autocal  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="有素材又没参考时自动标定")
    ap.add_argument("--dry-run", action="store_true", help="只说明会做什么，不写文件")
    args = ap.parse_args()
    return int(autocal.run(dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
