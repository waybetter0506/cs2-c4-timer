"""抓包夹具的读取与切分。

抓包探针是"追加"写盘的，而且每次启动 `elapsed` 会从 0 重新计。
所以这里按 `elapsed` 回退的位置把文件切成"多次抓包"，
每次抓包内部的时间轴才是连续的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .gsi import GsiSample, parse_payload
from .paths import app_root

DEFAULT_FIXTURE = app_root() / "fixtures" / "gsi_capture_20260915.jsonl"


@dataclass
class Capture:
    index: int
    records: list[dict]

    @property
    def wall_start(self) -> str:
        return self.records[0].get("wall", "?") if self.records else "?"

    @property
    def duration(self) -> float:
        return self.records[-1].get("elapsed", 0.0) if self.records else 0.0

    def samples(self) -> list[GsiSample]:
        """把每个包解析成 GsiSample，recv_mono 用 elapsed（确定性，便于回归）。"""
        out = []
        for rec in self.records:
            body = rec.get("body")
            if not isinstance(body, dict):
                continue
            out.append(
                parse_payload(body, float(rec.get("elapsed", 0.0)), rec.get("wall", ""))
            )
        return out


def load_captures(path: Path | str | None = None) -> list[Capture]:
    p = Path(path) if path else DEFAULT_FIXTURE
    if not p.exists():
        raise FileNotFoundError(
            f"找不到抓包文件 {p}；先双击 抓包.bat 抓一局，或用 --file 指定。"
        )
    records: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("body"), dict):
            records.append(rec)

    captures: list[list[dict]] = []
    cur: list[dict] = []
    for rec in records:
        if cur and float(rec.get("elapsed", 0.0)) < float(cur[-1].get("elapsed", 0.0)):
            captures.append(cur)
            cur = []
        cur.append(rec)
    if cur:
        captures.append(cur)

    return [Capture(i, c) for i, c in enumerate(captures, 1)]


def load_samples(path: Path | str | None = None) -> list[GsiSample]:
    out: list[GsiSample] = []
    for cap in load_captures(path):
        out.extend(cap.samples())
    return out
