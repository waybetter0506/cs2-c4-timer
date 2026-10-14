"""参考的实战统计：哪个参考在实战里真的被采纳过、分数多少。

用途（2026-09-18 与用户定稿的"参考成长"）：
- 重建参考时**优先保留被采纳过的**（两段式排序：先看有没有实战成绩，再看互证总分）
- 之后用来判断"分数整体下滑"（换语音包 / 换设备 / 改音量）时自动重标

键是**来源片段名**，而参考 npz 的 `source` 字段里本来就带着它（形如
`plant_20260917_212925.wav-aligned-len3.2s`），所以不需要给参考改数据结构。

与 tuning.py 同一个惯例：配置里不存统计，统计单独放 `runtime/reference_stats.json`，
删掉即重置；按参考集分代（参考一换，旧成绩作废，因为分数不可跨代比较）。
"""

from __future__ import annotations

import json
import statistics
import time

from .paths import runtime_dir
from .tuning import era_id

STATS_PATH = runtime_dir() / "reference_stats.json"
DEGRADE_RATIO = 0.6      # 近期中位数掉到基线的这个比例以下 => 认为"整体下滑"
DEGRADE_SAMPLES = 6      # 判断下滑至少要这么多近期样本
MAX_SCORES = 50          # 每个参考最多留这么多条分数

_state: dict | None = None


def _load() -> dict:
    global _state
    if _state is not None:
        return _state
    data: dict = {"refs": {}, "baseline": None, "era": era_id(), "seq": 0}
    try:
        if STATS_PATH.exists():
            raw = json.loads(STATS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update({k: raw[k] for k in data if k in raw})
    except (OSError, ValueError):
        pass
    if data.get("era") != era_id():          # 参考集换过了 => 旧成绩作废
        data = {"refs": {}, "baseline": None, "era": era_id(), "seq": 0}
    _state = data
    return data


def _save() -> None:
    if _state is None:
        return
    try:
        STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATS_PATH.write_text(json.dumps(_state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass                                  # 统计写不了绝不影响计时


def reset_memory() -> None:
    """只清内存态（自检用；不动磁盘）。"""
    global _state
    _state = {"refs": {}, "baseline": None, "era": era_id(), "seq": 0}


def record(ref: str, score: float) -> None:
    """记一次**被采纳**的命中（只有真的用于校正 T0 的才算）。"""
    if not ref:
        return
    d = _load()
    item = d.setdefault("refs", {}).setdefault(ref, {"adopted": 0, "scores": []})
    item["adopted"] = int(item.get("adopted", 0)) + 1
    d["seq"] = int(d.get("seq", 0)) + 1
    item.setdefault("scores", []).append([d["seq"], round(float(score), 2)])
    item["scores"] = item["scores"][-MAX_SCORES:]
    item["last"] = round(time.time(), 1)
    _save()


def fired(ref: str) -> int:
    """这个参考在实战里被采纳过几次（用于重建时的优先排序）。"""
    return int(_load().get("refs", {}).get(ref, {}).get("adopted", 0))


def recent_median(n: int = DEGRADE_SAMPLES) -> float | None:
    """最近 n 次被采纳命中的分数中位数（跨参考合并，代表"当前整体水平"）。"""
    d = _load()
    rows = []
    for ref, item in d.get("refs", {}).items():
        for s in item.get("scores", []):
            if isinstance(s, (list, tuple)) and len(s) == 2:
                rows.append((int(s[0]), float(s[1])))
            else:                              # 兼容旧格式（只存了分数）
                rows.append((0, float(s)))
    if len(rows) < n:
        return None
    rows.sort(key=lambda kv: kv[0])            # 按全局序号，而不是"每个参考最后时间"近似
    return float(statistics.median([s for _, s in rows[-n:]]))


def baseline() -> float | None:
    b = _load().get("baseline")
    return None if b is None else float(b)


def update_baseline() -> float | None:
    """把当前水平记为本代基线（第一次够样本时调用；重标后由 era 变化自动清空）。"""
    cur = recent_median()
    if cur is None:
        return None
    d = _load()
    d["baseline"] = round(cur, 2)
    _save()
    return cur


def degraded() -> tuple[bool, float | None, float | None]:
    """返回 (是否整体下滑, 近期中位数, 基线)。"""
    cur = recent_median()
    base = baseline()
    if cur is None or base is None or base <= 0:
        return False, cur, base
    return (cur < base * DEGRADE_RATIO), cur, base


def summary() -> str:
    d = _load()
    refs = d.get("refs", {})
    if not refs:
        return "参考实战统计：还没有被采纳过的命中"
    top = sorted(refs.items(), key=lambda kv: -int(kv[1].get("adopted", 0)))[:3]
    parts = [f"{name} {int(item.get('adopted', 0))} 次" for name, item in top]
    base = d.get("baseline")
    tail = f"，基线中位 {float(base):.1f}" if base is not None else ""
    return f"参考实战统计：{len(refs)} 个参考有成绩，最多的 " + "、".join(parts) + tail
