"""音频指纹（Shazam 式）：判断"是不是**这一句**安放语音"，而不是"像不像人声"。

**为什么换掉 MFCC/频带能量**（2026-09-17 实测，见 docs/实机反馈与待办.md 23.6/23.8）：
用语音特征时，系统实际在问"这段话像不像人声"——于是队友报点、击杀播报、其它播报
都会得分接近；实测真身与误报完全重叠（AUC 0.5~0.63）。指纹只看时频图上的**峰值点**
（星座图），枪声/脚步/底噪撼动不了峰值位置，而不同语音的峰值分布模式完全不同：

    真身命中数 24/27/40/52/52    误报 3~15（中位 8，仅 1 个 30 越界）   AUC 0.98

**机制**（按用户给的参数：FFT 4096 / hop 1470，48kHz 下 85ms 窗、30.6ms 步长）：
1. 每帧在 200~6000Hz 内取局部极大峰，最多留最强 PEAKS_PER_FRAME 个；
   （归一化/白化试过，会让分辨力崩掉，见 peaks_of 的注释）
2. 每个峰与**前方扇形区**内的峰配对，量化成 `(f1_bin, f2_bin, Δt)` 哈希；
3. 实时片段同样处理，命中哈希按**时间偏移分箱**统计 —— 真命中会排成一条斜线
   （所有命中落在同一个偏移箱里），所以"最高箱的计数"就是分数；
4. 阈值不写死：用"没有安放语音的正常对局"量出的背景基线（中位≈8）动态抬高。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from . import ANALYSIS_RATE

# 采样率沿用包根的唯一定义（这里保留 `RATE` 这个短名，模块内用得最多）
RATE = ANALYSIS_RATE
NFFT, HOP = 4096, 1470
F_LO, F_HI = 200.0, 6000.0
PEAKS_PER_FRAME = 4
NEIGH_F, NEIGH_T = 12, 5
FAN_MIN, FAN_MAX = 1, 40
FAN_DF = 60
F_BIN = 8
MAX_DF_BINS = 24

# 分数是"最高时间偏移箱里的命中哈希数"（不是相关系数）——与旧口径不是一个量纲。
# 2026-09-18 标定（参考换成**完整一句**、3.2 秒，见 tools/build_fingerprints.py）：
#   · 真身：对齐好的实例 31 / 55（同一句的两次落盘），被枪声盖住一半的那次 12；
#     实机日志里的真检测（播报候选）是 16~25，安放时刻那个 10 秒窗口的峰值能到 30~43。
#   · 背景：从实机日志里取"距任何安放/爆炸 ≥15 秒"的窗口共 308 个样本 ⇒
#     中位 0、p90 4、p95 6、**最大 10**（最保守的 4 参考 era：最大 7）。
#   · ⚠️ 曾经的"中文播报虚假碰撞 13 分"是**错判**：用户说明自己打的一直是中文局，
#     那两个文件其实是**同一句的弱命中**（真身偏弱），不是跨语言误报。
#     所以真实背景上限比 13 低得多，16 这个阈值是**偏保守**的（代价是偶尔漏检）。
# 取 16 的取舍：宁可漏掉"被枪声盖住一半"的那次，也不误触发（用户要求"确定不是误报再显示"）。
# 漏掉的局仍走 GSI 原路径，不会算错。阈值现在可以在配置里改：`audio.min_hits`。
# 待办：最新 era 的背景样本只有 3.7 分钟，上尾还不够厚 ⇒ 攒够数据后再评估要不要降到 10~12。
MIN_HITS = 16

# 动态阈值：背景基线的多少倍才算"明显高于基线"（用户第 4 点）
BASELINE_FACTOR = 3.0


def peaks_of(pcm: np.ndarray) -> np.ndarray:
    """(帧号, 频点) 的峰值点：局部极大 + 每帧只留最强的几个 + 相对本段的阈值。

    **试过并否掉的方案：谱白化**（2026-09-18）。动机是实战片段更吵（RMS 0.028~0.046
    vs 参考 0.013），怀疑播报的峰被枪声挤掉。实测（tools/_diag_fp.py，4 段正样本 48 段负样本）：
    时间方向白化 + 10 峰/帧时**负样本最高分从 8 涨到 108** —— 因为哈希的频率量化只有
    8 个频点/格（≈94Hz），白化把峰值数量放大后大量虚假碰撞堆进同一个时间偏移箱，
    分数完全失去分辨力。频率方向白化同样没有改善。⇒ 保持原始谱，不动归一化。
    """
    n = 1 + max(0, (len(pcm) - NFFT)) // HOP
    if n < 4:
        return np.zeros((0, 2), dtype=np.int32)
    win = np.hanning(NFFT).astype(np.float32)
    idx = HOP * np.arange(n, dtype=np.int64)[:, None] + np.arange(NFFT)[None, :]
    spec = np.abs(np.fft.rfft(pcm[idx] * win, axis=1))[:, :NFFT // 2 + 1]
    freqs = np.fft.rfftfreq(NFFT, 1 / RATE)
    lo = int(np.searchsorted(freqs, F_LO))
    hi = int(np.searchsorted(freqs, F_HI))
    mag = spec[:, lo:hi]
    out = []
    for t in range(n):
        a, b = max(0, t - NEIGH_T), min(n, t + NEIGH_T + 1)
        row = mag[t]
        thr = mag[a:b].mean() * 1.5
        for f in np.argsort(row)[::-1][:PEAKS_PER_FRAME * 2]:
            if row[f] < thr:
                break
            f0, f1 = max(0, f - NEIGH_F), min(mag.shape[1], f + NEIGH_F + 1)
            if row[f] >= mag[a:b, f0:f1].max():
                out.append((t, f))
    return np.array(out, dtype=np.int32) if out else np.zeros((0, 2), dtype=np.int32)


def hashes_of(peaks: np.ndarray) -> dict[tuple[int, int, int], list[int]]:
    """(f1_bin, f2_bin, Δt) -> 出现过的起始帧号列表。"""
    table: dict[tuple[int, int, int], list[int]] = {}
    for i, (t1, f1) in enumerate(peaks):
        for t2, f2 in peaks[i + 1:]:
            dt = int(t2 - t1)
            if dt < FAN_MIN:
                continue
            if dt > FAN_MAX:
                break
            df = int(f2 - f1)
            if abs(df) > FAN_DF:
                continue
            table.setdefault((int(f1) // F_BIN, int(f2) // F_BIN, dt), []).append(int(t1))
    return table


def best_offset_hits(query_hashes: dict, ref: dict) -> tuple[int, float]:
    """返回 (最高箱的命中数, 该箱对应的时间偏移秒)。

    真命中时所有命中点落在同一个偏移（斜率一致的那条线）⇒ 分数就是那个箱的高度。
    """
    hist: dict[int, int] = {}
    for key, q_times in query_hashes.items():
        ref_times = ref.get(key)
        if not ref_times:
            continue
        for qt in q_times:
            for rt in ref_times:
                off = qt - rt
                hist[off] = hist.get(off, 0) + 1
    if not hist:
        return 0, 0.0
    best_off, best = 0, 0
    for off in hist:
        s = hist.get(off, 0) + hist.get(off - 1, 0) + hist.get(off + 1, 0)
        if s > best:
            best_off, best = off, s
    return best, best_off * HOP / RATE


def reference_table(pcm: np.ndarray) -> dict:
    return hashes_of(peaks_of(pcm))
