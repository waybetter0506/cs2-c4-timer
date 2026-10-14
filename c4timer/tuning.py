"""自成长：把每局爆炸时的 CALIB 观测累积起来，自动微调音频时间偏置。

设计（2026-09-18 与用户定稿）
---------------------------
1. **配置里永远只存手动值**（`audio.onset_bias_sec`）。自动修正量单独存在
   `runtime/calibration_stats.json`，运行时相加。好处：手调值不会被覆盖，一眼能看出
   "程序自己贡献了多少"，删掉这个文件就立刻回到手动值（这就是"清空"）。
2. **门槛**（都满足才生效）：有效观测 >= 8 局、每局误差区间 <= 0.8 秒、均值标准误 <= 0.08 秒。
3. **限幅**：单次调整不超过 0.3 秒，累计修正夹在正负 0.8 秒内。
4. **按参考集分代**：观测里记着当时的参考集指纹；参考一换，旧观测作废（分数不可跨代比较）。

符号约定：误差 = 我们的归零 − 真实时限（正 = 我们偏晚）=> 偏置要**减小**，
所以自动量 = −clamp(平均误差)。
"""

from __future__ import annotations

import hashlib
import json
import math
import time

from .paths import runtime_dir

STATS_PATH = runtime_dir() / "calibration_stats.json"
REF_DIR = runtime_dir() / "audio_templates"

MIN_SAMPLES = 8          # 至少这么多局有效观测才动手
MAX_INTERVAL = 1.2       # 单局误差区间宽于这个就当没测准，不计入
                         # 2026-09-19 由 0.8 放宽到 1.2：实测 87 局里只有 5 局 <= 0.8s
                         # （这台机器上 GSI 报文常隔数秒），导致自成长几十局都攒不到 8 局观测。
MAX_STDERR = 0.08        # 均值的标准误超过这个就继续攒
MAX_STEP = 0.3           # 单次调整上限（秒）
CLAMP = 0.8              # 累计修正上限（秒）

_state: dict | None = None


def era_id() -> str:
    """当前参考集的指纹（文件名 + 大小）。参考一换，旧观测就不再适用。"""
    try:
        items = sorted((p.name, p.stat().st_size) for p in REF_DIR.glob("fp_*.npz"))
    except OSError:
        items = []
    return hashlib.sha1(repr(items).encode("utf-8")).hexdigest()[:12] if items else "none"


def _load() -> dict:
    global _state
    if _state is not None:
        return _state
    data: dict = {"observations": [], "auto": 0.0, "era": era_id(), "samples": 0}
    try:
        if STATS_PATH.exists():
            raw = json.loads(STATS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update({k: raw[k] for k in data if k in raw})
    except (OSError, ValueError):
        pass
    if data.get("era") != era_id():          # 参考集换过了 => 旧观测作废
        data = {"observations": [], "auto": 0.0, "era": era_id(), "samples": 0}
    _state = data
    return data


def _save() -> None:
    if _state is None:
        return
    try:
        STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATS_PATH.write_text(json.dumps(_state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass                                  # 写不了就算了：统计失败绝不影响计时


def offset() -> float:
    """当前生效的自动修正量（秒）。加到手动的音频偏置上使用。

    2026-09-18 的修法：增量形式（new = old − 残差均值）**加上**每次调整后清空观测窗口。
    两项缺一不可 —— 只用增量会在旧残差上继续推，只用清窗会在绝对值形式上跳。
    用带反馈的模拟验证过：真实偏晚 0.5 秒时收敛到 −0.50，之后真实偏晚改为 0.1 秒，
    自动量跟着收敛到 −0.10，不会冲到上限。
    """
    return float(_load().get("auto", 0.0))


def reset_memory() -> None:
    """只清内存态（自检用；不动磁盘上的文件）。"""
    global _state
    _state = {"observations": [], "auto": 0.0, "era": era_id(), "samples": 0}


def observe(error: float, width: float, source: str = "") -> str | None:
    """记一局观测；满足门槛时更新自动量，返回一行给日志的说明（没更新就返回 None）。

    调用方负责"缺局排除"：没 CALIB 的局不会走到这里；旁观位真值（`gsi-truth`）与
    音频没生效的局由调用方按 `source` 过滤。这里再查一次区间宽度。
    """
    if not math.isfinite(error) or not math.isfinite(width) or width > MAX_INTERVAL:
        return None
    d = _load()
    d.setdefault("observations", []).append(
        {"t": round(time.time(), 1), "err": round(error, 3), "w": round(width, 3),
         "src": source})
    d["observations"] = d["observations"][-200:]      # 只留最近 200 局
    d["samples"] = len(d["observations"])
    errs = [o["err"] for o in d["observations"]]
    if len(errs) < MIN_SAMPLES:
        _save()
        return None
    mean = sum(errs) / len(errs)
    var = sum((x - mean) ** 2 for x in errs) / max(1, len(errs) - 1)
    stderr = math.sqrt(var / len(errs))
    d["mean"] = round(mean, 3)
    d["stderr"] = round(stderr, 3)
    if stderr > MAX_STDERR:
        _save()
        return None
    old = float(d.get("auto", 0.0))
    # 观测到的误差是"**已经带上了当前自动量**之后的残差"，所以按增量修正：
    # new = old − mean（把残差抵消掉）。若写成 new = −mean（绝对值形式），旧的观测会让它
    # 每局都继续往同方向推 => 振荡。2026-09-18 的真实节奏模拟演习抓到过这一点
    # （20 局里从第 8 局起每局都在调，−0.30 → −0.50 → −0.47 → …）。
    target = max(-CLAMP, min(CLAMP, old - mean))
    step = max(-MAX_STEP, min(MAX_STEP, target - old))
    new = max(-CLAMP, min(CLAMP, old + step))
    if abs(new - old) < 1e-4:
        _save()
        return None
    d["auto"] = round(new, 3)
    # 窗口用掉了：这些残差是对**旧**偏置量测出来的，留着会让下一轮继续往同方向推
    # （2026-09-18 的真实节奏模拟抓到过：-0.30 -> -0.50 -> -0.47 -> ... 一路冲到上限）。
    d["observations"] = []
    d["samples"] = 0
    _save()
    return (f"自动微调音频偏置：{old:+.2f}s -> {new:+.2f}s"
            f"（{len(errs)} 局观测，平均误差 {mean:+.3f}s，标准误 {stderr:.3f}s）")


def summary() -> str:
    """给启动日志/体检用的一行摘要。"""
    d = _load()
    n = int(d.get("samples", 0) or 0)
    auto = float(d.get("auto", 0.0))
    if not n:
        return "自成长统计：还没有有效观测（每局爆炸后自动累积）"
    extra = ""
    if "mean" in d:
        extra = f"，平均误差 {float(d['mean']):+.3f}s，标准误 {float(d.get('stderr', 0)):.3f}s"
    return (f"自成长统计：{n} 局有效观测{extra}，自动修正量 {auto:+.2f}s"
            f"（删掉 runtime/calibration_stats.json 即归零）")
