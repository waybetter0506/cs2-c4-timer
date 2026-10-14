"""离线评测指纹参考：把一批 wav 逐个跟参考比对，打印分数分布（真身 vs 误报）。

为什么需要它（2026-09-18 教训）：自检 `python -m c4timer test` **不覆盖指纹路径的真实参考**，
所以"改完到底行不行"必须拿真实落盘数据量。这个脚本就是那把尺子：
真身（含安放语音的片段）与误报（不含的片段）各打多少分，阈值该定在哪。
度量纪律：**每个参考各自打分再取最大**，不要把所有参考的哈希并成一张表
（并表会让误报虚高，2026-09-17 踩过）。

用法：
    python tools/fp_eval.py                     # 默认扫 runtime/audio_dumps 全部 wav
    python tools/fp_eval.py 文件1.wav 文件2.wav  # 指定文件
    python tools/fp_eval.py --dir 某目录
文件名里带 `plant_` 或 `pos_` 的算正样本，其余算负样本。
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from c4timer.audio import fingerprint as fp  # noqa: E402
from c4timer.audio.plant import ANALYSIS_RATE, load_templates  # noqa: E402

DEFAULT_DIRS = [ROOT / "runtime" / "audio_dumps", ROOT / "runtime" / "audio_templates"]


def read_wav(path: Path) -> np.ndarray | None:
    """读 wav 的**第一个声道**并重采样到 ANALYSIS_RATE（只支持 16bit PCM）。"""
    try:
        with wave.open(str(path), "rb") as w:
            ch, width, rate, n = (w.getnchannels(), w.getsampwidth(),
                                  w.getframerate(), w.getnframes())
            raw = w.readframes(n)
    except (OSError, wave.Error):
        return None
    if width != 2:
        return None
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x[: len(x) // ch * ch].reshape(-1, ch)[:, 0]
    if rate != ANALYSIS_RATE and len(x) > 1:
        m = int(len(x) * ANALYSIS_RATE / rate)
        x = np.interp(np.linspace(0, len(x) - 1, m), np.arange(len(x)), x).astype(np.float32)
    return x


def score_file(pcm: np.ndarray, refs: list[tuple[str, dict]]) -> tuple[int, str, float]:
    """返回 (最高命中, 命中的参考名, 该箱偏移秒)。逐参考打分后取最大。"""
    q = fp.hashes_of(fp.peaks_of(pcm))
    best, best_name, best_off = 0, "", 0.0
    for name, table in refs:
        hits, off = fp.best_offset_hits(q, table)
        if hits > best:
            best, best_name, best_off = hits, name, off
    return best, best_name, best_off


def run_detector(files: list[Path]) -> int:
    """把 wav 喂进**真正的产品链路**（PlantVoiceDetector），看它报不报。

    为什么必须有这一步（2026-09-18 教训）：离线手算 `best_offset_hits` 不等于产品行为 ——
    产品还有 need（动态阈值）、REFRACTORY 去重、10 秒滑动窗、0.4 秒扫描间隔。
    """
    from c4timer.audio.plant import PlantVoiceDetector, load_templates

    tmpl_dir = ROOT / "runtime" / "audio_templates"
    tmpls = load_templates(tmpl_dir)
    if not tmpls:
        print("[err] 没有模板")
        return 2
    chunk = int(0.1 * ANALYSIS_RATE)
    print(f"检测器：模板 {[t.name for t in tmpls]}，窗口 {10.0}s，MIN_HITS={fp.MIN_HITS}")
    for f in files:
        pcm = read_wav(f)
        if pcm is None:
            print(f"[skip] {f.name}")
            continue
        det = PlantVoiceDetector(tmpls)
        hits = []
        for i in range(0, len(pcm), chunk):
            hits.extend(det.feed(pcm[i:i + chunk]))
        best, need, base = getattr(det, "last_scores", (0, 0, 0))
        verdict = "HIT" if hits else "no "
        print(f"  {verdict} {f.name:44s} 命中={len(hits)} "
              f"t={[round(h.t_stream, 2) for h in hits]} "
              f"best={best:.0f} need={need:.0f} 基线={base:.1f}")
    return 0


def main(argv: list[str]) -> int:
    # --band LO HI：临时改指纹的取峰频带（调参用；产品默认在 fingerprint.py 里）
    band = None
    if "--band" in argv:
        i = argv.index("--band")
        band = (float(argv[i + 1]), float(argv[i + 2]))
        del argv[i:i + 3]
    if band:
        fp.F_LO, fp.F_HI = band
    detector = "--detector" in argv
    if detector:
        argv.remove("--detector")
    files: list[Path] = []
    dirs: list[Path] = []
    for a in argv:
        p = Path(a)
        if p.is_dir():
            dirs.append(p)
        elif p.is_file():
            files.append(p)
        else:
            print(f"[skip] 找不到 {a}")
    if not dirs and not files:
        dirs = DEFAULT_DIRS
    for d in dirs:
        if d.is_dir():
            files.extend(sorted(d.glob("*.wav")))
    if detector:
        return run_detector(files)

    tmpl_dir = ROOT / "runtime" / "audio_templates"
    tmpls = [t for t in load_templates(tmpl_dir) if t.pcm is not None]
    if not tmpls:
        print(f"[err] {tmpl_dir} 里没有带 pcm 的参考（.npz 必须存了 pcm 才能用指纹路径）")
        return 2
    refs = [(t.name, fp.reference_table(np.asarray(t.pcm, dtype=np.float32))) for t in tmpls]
    print(f"参考: " + ", ".join(f"{n}({len(t)} hashes)" for n, t in refs))
    print(f"参数: NFFT={fp.NFFT} HOP={fp.HOP} 峰/帧={fp.PEAKS_PER_FRAME} "
          f"MIN_HITS={fp.MIN_HITS} 频带={fp.F_LO:.0f}-{fp.F_HI:.0f}\n")

    pos, neg = [], []
    for f in files:
        pcm = read_wav(f)
        if pcm is None or len(pcm) < fp.NFFT * 2:
            continue
        hits, name, off = score_file(pcm, refs)
        secs = len(pcm) / ANALYSIS_RATE
        is_pos = ("plant_" in f.name) or f.name.startswith("pos_")
        (pos if is_pos else neg).append((hits, f.name, name, off, secs))

    def show(title: str, rows: list) -> None:
        print(f"== {title} ({len(rows)}) ==")
        for hits, nm, ref, off, secs in sorted(rows, reverse=True):
            flag = "HIT " if hits >= fp.MIN_HITS else "    "
            print(f"  {flag}{hits:4d}  off={off:6.2f}s  len={secs:5.1f}s  {nm}  <- {ref}")
        if rows:
            v = np.array([r[0] for r in rows])
            print(f"  min={v.min()} 中位={int(np.median(v))} max={v.max()}\n")

    show("正样本（含安放语音）", pos)
    show("负样本（不含安放语音）", neg)
    print(f"== OVER-THRESHOLD (>={fp.MIN_HITS}) ==")
    for hits, nm, ref, off, secs in sorted(pos + neg, reverse=True):
        if hits >= fp.MIN_HITS:
            tag = "POS" if (hits, nm, ref, off, secs) in pos else "NEG"
            print(f"  {hits:4d}  {tag}  {nm}")
    if pos and neg:
        p_min = min(r[0] for r in pos)
        n_max = max(r[0] for r in neg)
        # ASCII 汇总行：中文在 GBK 控制台会被打乱，调参时要能直接 grep
        print(f"SUMMARY band={fp.F_LO:.0f}-{fp.F_HI:.0f} pos={sorted(r[0] for r in pos)} "
              f"pos_min={p_min} neg_max={n_max} neg_med={int(np.median([r[0] for r in neg]))}")
        print(f"正样本最低 {p_min} / 负样本最高 {n_max} "
              f"=> {'可分' if p_min > n_max else '重叠（需更好的特征或更好的参考）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
