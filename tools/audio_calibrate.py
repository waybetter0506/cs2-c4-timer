#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从一局录音里标定"安放播报"模板（工具，不属于产品运行链路）。

输入：`tools/record_audio.py` 录出来的会话目录（audio.wav + gsi.jsonl，样本级对齐）。
输出：`runtime/audio_templates/<name>.npz`（**不进版本库**——模板来自游戏音频），
      外加一个同名 .wav 供人耳确认"切出来的确实是那句播报"。

为什么需要它：模板必须来自**用户自己的**录音（语音包/语言/音量都不同）。
自动对齐原理：同一句播报在每次安放都会出现，而枪声/脚步是随机的 ⇒
把各次安放的窗口两两对齐，"每次都出现的那一段"就是播报。
（注意：播报相对 **GSI 揭示包**的位置是**随机**的（GSI 延迟 0.03~2.6s 随机），
 所以搜索必须在"相对真实安放"上做 —— 这也是早期盲搜失败的原因。）

用法：
    python tools/audio_calibrate.py sessions/session_20260917_000852 --name zh
    python tools/audio_calibrate.py <会话> --name en --templates runtime/audio_templates
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from c4timer.audio.plant import (  # noqa: E402
    ANALYSIS_RATE, HOP, THRESHOLD, VoiceTemplate, band_features, load_templates,
    save_template, scan_pcm, zscore,
)

PRE, POST = 4.5, 1.0          # 每次安放取的窗口（相对 GSI 揭示包）
# 对齐用的片段长度：必须**短**才锐利 —— 用 3.2 秒对齐时会被响的段落带偏
# （实测：英文模板 0/5 命中）。
SEG = 2.0
# 但**存下来/试听**的模板要够长，否则整句播报被截断（用户听出来只有前半句）。
# 检测器仍然只用前 config.audio.template_sec 秒去匹配，所以存长不影响判定。
SAVE = 3.2
VOICE = (300.0, 3000.0)       # 估计"播报起点"用的频段


def load_session(d: Path):
    w = wave.open(str(d / "audio.wav"), "rb")
    assert w.getframerate() == ANALYSIS_RATE, f"采样率应为 {ANALYSIS_RATE}"
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    recs = [json.loads(l) for l in
            (d / "gsi.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    plants = []
    for i, r in enumerate(recs):
        before = recs[i - 1]["bomb_state"] if i else None
        if (r["bomb_state"] in ("planting", "planted")
                and before not in ("planting", "planted", "defusing")):
            plants.append(float(r["t_audio"]))
    return pcm, plants


def seg_feats(pcm: np.ndarray, t0: float, dur: float = SEG) -> np.ndarray:
    s = int(t0 * ANALYSIS_RATE)
    return band_features(pcm[max(0, s):s + int(dur * ANALYSIS_RATE)])


def align(pcm: np.ndarray, plants: list[float]):
    """返回 [(plant_t, 片段起点相对 plant_t 的偏移, 相似度)]（第一局为基准）。"""
    # 每局的搜索窗口是整个 [−PRE, +POST] 段（比片段长，留出滑动的余地）
    wins = [seg_feats(pcm, t - PRE, PRE + POST) for t in plants]
    # 帧数按 band_features 的公式算（1 + (样本数 - NFFT)//HOP），别自己拍
    win_frames = 1 + (int(SEG * ANALYSIS_RATE) - 1024) // HOP
    max_off = max(0, len(wins[0]) - win_frames)

    def best_pair(a: np.ndarray, ref_off: int, b: np.ndarray):
        """固定 a 的偏移，找 b 里最匹配的位置。"""
        ref = zscore(a[ref_off:ref_off + win_frames])
        best = (-9.9, 0)
        for off in range(0, max_off + 1):
            if off + win_frames > len(b):
                break
            w = b[off:off + win_frames]
            w = w - w.mean(axis=0, keepdims=True)
            s = float((w * ref).mean() / (w.std() + 1e-6))
            if s > best[0]:
                best = (s, off)
        return best

    # 二维搜索：基准局片段起点 × 第二局片段起点
    coarse = max(1, int(0.1 * ANALYSIS_RATE / HOP))
    best = (-9.9, 0, 0)
    for a_off in range(0, max_off + 1, coarse):
        for b_off in range(0, max_off + 1, coarse):
            ref = wins[0][a_off:a_off + win_frames]
            w = wins[1][b_off:b_off + win_frames]
            if len(ref) < win_frames or len(w) < win_frames:
                continue
            r = zscore(ref)
            w = w - w.mean(axis=0, keepdims=True)
            s = float((w * r).mean() / (w.std() + 1e-6))
            if s > best[0]:
                best = (s, a_off, b_off)
    _, a_off, b_off = best
    # 细化（±0.15s，20ms 步长）
    fine = max(1, int(0.02 * ANALYSIS_RATE / HOP))
    ref = zscore(wins[0][a_off:a_off + win_frames])
    for cand in range(max(0, b_off - 8), min(max_off, b_off + 8) + 1, fine):
        w = wins[1][cand:cand + win_frames]
        if len(w) < win_frames:
            continue
        w = w - w.mean(axis=0, keepdims=True)
        s = float((w * ref).mean() / (w.std() + 1e-6))
        if s > best[0]:
            best = (s, a_off, cand)
    _, a_off, b_off = best

    out = [(plants[0], a_off * HOP / ANALYSIS_RATE - PRE, float(best[0]))]
    ref = zscore(wins[0][a_off:a_off + win_frames])
    for k in range(1, len(plants)):
        s, off = best_pair(wins[0], a_off, wins[k])
        if k == 1:
            s, off = best[0], b_off
        out.append((plants[k], off * HOP / ANALYSIS_RATE - PRE, s))
    return out, wins, win_frames


def voice_onset(feats: np.ndarray) -> float:
    """在片段里估计"播报起点"：人声频段包络第一次明显抬起来的位置（秒）。"""
    # 用 24 个频带里靠近人声的那几段（对数频带 100~5000Hz，人声约在第 8~17 带）
    env = feats[:, 8:18].mean(axis=1)
    floor = np.percentile(env, 20)
    thresh = floor + 0.25 * (env.max() - floor)
    idx = np.argmax(env > thresh)
    return float(idx * HOP / ANALYSIS_RATE)


def main() -> int:
    ap = argparse.ArgumentParser(description="从一局录音标定安放播报模板")
    ap.add_argument("session", type=Path, help="会话目录（含 audio.wav / gsi.jsonl）")
    ap.add_argument("--name", required=True, help="模板名（例如 zh / en）")
    ap.add_argument("--templates", type=Path, default=ROOT / "runtime" / "audio_templates")
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    args = ap.parse_args()

    if not (args.session / "audio.wav").exists():
        print(f"[X] 找不到 {args.session/'audio.wav'}")
        return 1
    pcm, plants = load_session(args.session)
    print(f"会话 {args.session.name}：音频 {len(pcm)/ANALYSIS_RATE:.1f}s，安放 {len(plants)} 次")
    if len(plants) < 2:
        print("[X] 至少需要 2 次安放才能自动对齐（1 次无法区分播报与枪声）")
        return 1

    aligned, wins, win_frames = align(pcm, plants)
    print("\n=== 对齐结果（负号 = 早于 GSI 揭示包）===")
    for (t, off, s), k in zip(aligned, range(1, len(plants) + 1)):
        print(f"  plant{k}: t={t:8.2f}s  片段起点 {off:+.2f}s  相似度 {s:.3f}")
    if min(a[2] for a in aligned[1:]) < 0.45:
        print("[X] 相似度过低 —— 这一局可能没有播报（被盖住/语言不同/音量太小）。")
        return 1

    segs = [seg_feats(pcm, t + off) for t, off, _ in aligned]
    segs = [s[:win_frames] for s in segs]
    # 存模板/试听用更长的跨度（同一对齐起点往后延），保证整句播报都在
    save_frames = 1 + (int(SAVE * ANALYSIS_RATE) - 1024) // HOP
    long_segs = [seg_feats(pcm, t + off)[:save_frames] for t, off, _ in aligned]
    long_segs = [s for s in long_segs if len(s) == save_frames] or segs

    # ---- 用三次的**平均**当模板（单用某一局的原始片段太脏：另两局只到 0.70）----
    mean_tmpl = zscore(np.mean(segs, axis=0))

    def match_all(w: np.ndarray, ref_z: np.ndarray, near: int | None = None) -> tuple[float, int]:
        """在窗口 w 里找与 ref_z 最匹配的片段起点（给了 near 就只在附近搜）。"""
        lo = 0 if near is None else max(0, near - 10)
        hi = len(w) - win_frames if near is None else min(len(w) - win_frames, near + 10)
        best = (-9.9, lo)
        for off in range(lo, hi + 1):
            x = w[off:off + win_frames]
            if len(x) < win_frames:
                break
            x = x - x.mean(axis=0, keepdims=True)
            s = float((x * ref_z).mean() / (x.std() + 1e-6))
            if s > best[0]:
                best = (s, off)
        return best

    refined, scores = [], []
    for k, w in enumerate(wins):
        s, off = match_all(w, mean_tmpl)
        refined.append(off * HOP / ANALYSIS_RATE - PRE)
        scores.append(s)
    print("\n=== 用均值模板重新对齐（并用它打分）===")
    for k, (t, off, s) in enumerate(zip(plants, refined, scores), 1):
        print(f"  plant{k}: t={t:8.2f}s  片段起点 {off:+.2f}s  相似度 {s:.3f}")

    # ---- 绝对偏置不在这里猜 ----
    # 对齐只能保证三次都在同一相对位置，而模板起点相对**真实安放**还有
    # 一个未知常数（播报本身可能比安放晚一点点）。这个常数留给
    # config.audio.onset_bias_sec（2026-09-18 起；>0 = 归零更晚），用实机对照标定。
    onset = 0.0
    for k, (t_, off, s) in enumerate(zip(plants, refined, scores), 1):
        print(f"  plant{k}: 片段起点相对 GSI 揭示包 {off:+.2f}s（相似度 {s:.3f}）")

    tmpl = VoiceTemplate(name=args.name, feats=np.mean(long_segs, axis=0).astype(np.float32),
                         onset_sec=onset, source=args.session.name)
    out = args.templates / f"{args.name}.npz"
    save_template(out, tmpl)
    wav_out = args.templates / f"{args.name}_template.wav"
    raw = pcm[int((plants[0] + refined[0]) * ANALYSIS_RATE):][:int(SAVE * ANALYSIS_RATE)]
    w = wave.open(str(wav_out), "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(ANALYSIS_RATE)
    w.writeframes((np.clip(raw, -1, 1) * 32767).astype(np.int16).tobytes()); w.close()
    print(f"\n模板内播报起点 ≈ 片段开始后 {onset:.2f}s")
    print(f"已写出 模板 {out}")
    print(f"        试听 {wav_out}（确认是那句播报）")

    print("\n=== 用刚标定的模板扫整段录音（产品代码路径）===")
    hits = scan_pcm(pcm, load_templates(args.templates), threshold=args.threshold)
    for h in hits:
        near = min(plants, key=lambda p: abs(p - h.t_stream))
        tag = "命中" if abs(near - h.t_stream) <= 4.0 else "误报"
        print(f"  t={h.t_stream:8.2f}s  分数 {h.score:.3f}  模板 {h.name}  {tag}")
    n_hit = sum(1 for h in hits if min(abs(p - h.t_stream) for p in plants) <= 4.0)
    print(f"合计：{n_hit}/{len(plants)} 次安放命中，误报 {len(hits)-n_hit} 处")
    print("\n把上面这几行贴给我即可（尤其：相似度、留一法、误报数）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
