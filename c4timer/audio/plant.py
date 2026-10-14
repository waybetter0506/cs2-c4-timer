"""听"安放播报语音"来拿真实安放时刻（2026-09-17 加）。

**它解决什么**：活着或死后看队友时，Valve 故意把 GSI 的"已安放"事件延迟
0.03~2.6 秒（随机）；而**安放播报由客户端自己的游戏状态触发，不延迟**。
所以播报一响，我们就知道真实安放时刻 —— 这是目前唯一能在 CT 方拿到真值的客户端手段。

**参数是量出来的，不是拍的**（2026-09-17，366 秒真实录音 / 3 次安放）：

| 模板长度 | 阈值 | 命中 | 误报 | 裕度（真身最低 − 误报最高） |
|---|---|---|---|---|
| 1.0s | 0.80 | 3/3 | 4 | **−0.002**（不可靠） |
| 1.2s | 0.80 | 3/3 | 2 | +0.012（不可靠） |
| 1.4s | 0.80 | 2/3 | 1 | −0.01（不可靠） |
| **1.8s** | **0.80** | **3/3** | **0** | **+0.03** |
| **2.2s** | **0.78** | **3/3** | **0** | **+0.06** ← 当前默认 |

取 **2.2 秒 + 0.78**：命中 3/3 且零误报，裕度最大（真身最低 0.82 对最强误报 0.76）。
代价：出结果有 ~1.4 秒延迟（与 GSI 那个延迟的中位数同量级），换来的是 **T0 精确到 ±0.05 秒**
—— 这正是用户抱怨的"有时过早有时过晚"的根治点。

**门控由调用方负责**（2026-09-19 修订）：只看"回合是不是 live、GSI 有没有揭示过安放"，
**阵营不再参与**（原为"CT 方全流程启用、T 方全流程不用"，2026-09-19 放开 ——
T 方队友下包时变的是队友的背包，你自己那一包没有变化，锚点同样有 ±1 秒误差）。

**这一层不碰游戏**：输入只是 WASAPI 环回采到的自己的系统声音（与 OBS 录音同类）。
检测不到就什么都不做，调用方继续用 GSI 估算 —— 绝不因为音频失效让面板变差。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

# 采样率从包根取（唯一定义处）；此处保留同名导入，让 `from .plant import ANALYSIS_RATE` 继续可用
from . import ANALYSIS_RATE

NFFT = 1024
HOP = 240                 # 5ms
BANDS = 24
F_LO, F_HI = 100.0, 5000.0

TEMPLATE_SEC = 2.2        # 参与匹配的模板长度（秒）：本机型实测最优（见下表）
# 旧频带路径的候选阈值：与 config.audio.threshold 的默认值保持一致（从前一个 0.70
# 一个 0.72，是历史遗留）。指纹路径不看它 —— 那边用 `fingerprint.MIN_HITS`。
THRESHOLD = 0.70
                          # （原始误报 7 处全在 live 之外 —— 回合结束播报/主菜单，
                          #  门控 CT+live+未揭示 本来就会挡掉；所以门控是**必须**的）
REFRACTORY_SEC = 1.0      # 同一个事件不重复报
# 指纹路径：扫多长的音频、多久扫一次（每轮要算峰值+配对，别太频繁）
FP_WINDOW_SEC = 10.0
FP_INTERVAL_SEC = 0.4
# 动态阈值从"基线 × 3"改成"基线 + 固定余量"（2026-09-18）：参考只有 1.4 秒（半句）时
# 命中天花板本来就低，×3 会把 need 抬到 12，而实测真身只有 8/10/13 ⇒ 挡住真命中。
# 改成 +4：基线 ~1~4 ⇒ need = MIN_HITS，恰好落在真身与误报之间；
# 背景变吵（基线升高）时它仍会自动抬高，保留"动态阈值"的本意。
FP_BASELINE_MARGIN = 4.0
# need 的天花板（实测 2026-09-18：参考换成完整一句后真身 31/55、被枪声盖住一半的 12
# ⇒ 30 会在最吵的那次挡住真身，抬到 60）
FP_NEED_MAX = 60.0


_BAND_MASKS: list[np.ndarray] | None = None


def _band_masks() -> list[np.ndarray]:
    """各对数频带覆盖的 rfft 频点（只算一次）。"""
    global _BAND_MASKS
    if _BAND_MASKS is None:
        freqs = np.fft.rfftfreq(NFFT, 1 / ANALYSIS_RATE)
        edges = np.geomspace(F_LO, F_HI, BANDS + 1)
        _BAND_MASKS = [
            np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0] for i in range(BANDS)
        ]
    return _BAND_MASKS


def band_features(pcm: np.ndarray) -> np.ndarray:
    """24 个对数频带的能量（log1p），帧步长 HOP。

    向量化实现（一次批量 rfft 覆盖所有帧），并按 CHUNK 分块控制内存：
    366 秒录音的全部帧一次性 rfft 要 300MB，分块后几十 MB 且快得多
    （逐帧 Python 循环版实测慢到"整段扫描超时"）。
    """
    pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if len(pcm) < NFFT:
        return np.zeros((0, BANDS), dtype=np.float32)
    n = 1 + (len(pcm) - NFFT) // HOP
    masks = _band_masks()
    win = np.hanning(NFFT).astype(np.float32)
    offsets = np.arange(NFFT, dtype=np.int64)
    out = np.empty((n, BANDS), dtype=np.float32)
    chunk = 16384
    for s in range(0, n, chunk):
        e = min(n, s + chunk)
        idx = HOP * np.arange(s, e, dtype=np.int64)[:, None] + offsets[None, :]
        spec = np.abs(np.fft.rfft(pcm[idx] * win, axis=1))
        for b, m in enumerate(masks):
            out[s:e, b] = spec[:, m].mean(axis=1) if len(m) else 0.0
    return np.log1p(out * 100.0)


def zscore(mat: np.ndarray) -> np.ndarray:
    return (mat - mat.mean(axis=0, keepdims=True)) / (mat.std(axis=0, keepdims=True) + 1e-6)


def score_at(feats: np.ndarray, pos: int, tmpl_z: np.ndarray) -> float:
    """单点打分（自检/小数据用；实时走 sliding_scores 的向量化路径）。"""
    w = feats[pos:pos + len(tmpl_z)]
    w = w - w.mean(axis=0, keepdims=True)
    return float((w * tmpl_z).mean() / (w.std() + 1e-6))


def sliding_scores(feats: np.ndarray, tmpl_z: np.ndarray) -> np.ndarray:
    """逐带滑动点积，返回 (候选位置, 频带数)。向量化（滑窗视图 + tensordot）。

    ⚠️ 这只是**分子**：真正的分数还要除以窗口自身的标准差（见 corr_scores）。
    实测教训：少了那一步（改成"全局标准化 + 纯点积"）以后，响的段落凭音量拿高分，
    真身命中率从 3/3 掉到 0/3。
    """
    n, f = feats.shape[0], tmpl_z.shape[0]
    if n < f:
        return np.zeros((0, tmpl_z.shape[1]), dtype=np.float32)
    win = np.lib.stride_tricks.sliding_window_view(feats, f, axis=0)   # (pos, 24, f)
    win = np.moveaxis(win, 1, 2)                                       # (pos, f, 24)
    # 逐带点积必须用 einsum（tensordot 会多留一个轴）：win 是视图，einsum 不整体物化，
    # 所以内存安全。注意 einsum 在这里比"先乘后 sum"省一个 (P,f,B) 的临时数组。
    return np.einsum("pfb,fb->pb", win, tmpl_z, optimize=False).astype(np.float32)


def corr_scores(feats: np.ndarray, tmpl_z: np.ndarray, start: int = 0) -> np.ndarray:
    """与标定时**同一口径**的分数 = 窗口内逐带中心化后的归一化相关（再对帧、带取平均）。

    分子（逐带点积）用 **FFT 相关**算：24 个频带各一次变换，整段 366 秒录音 0.1~0.3 秒扫完；
    逐个窗口做 einsum 要 4.9 亿次乘加，实测直接扫不完（超时）。
    分母是窗口整体标准差，用累积和 O(1) 每位置算出来。

    start > 0 时只返回从 start 开始的部分（在线增量扫描用，避免重复计算）。
    """
    n, B = feats.shape
    f = tmpl_z.shape[0]
    if n < f or B == 0:
        return np.zeros(0, dtype=np.float32)
    x = feats.astype(np.float64)
    # --- 分子：逐带 FFT 相关，等价 np.correlate(x[:,b], tz[:,b], 'valid') ---
    L = 1 << int(np.ceil(np.log2(n + f)))          # 2 的幂，FFT 最快
    acc = np.zeros(n - f + 1, dtype=np.float64)
    for b in range(B):
        cross = np.fft.rfft(x[:, b], L) * np.conj(np.fft.rfft(tmpl_z[:, b], L))
        acc += np.fft.irfft(cross, L)[:n - f + 1]
    # --- 分母：滑窗方差（累积和）---
    z = np.zeros((1, B), dtype=np.float64)
    c1 = np.cumsum(np.vstack([z, x]), axis=0)
    c2 = np.cumsum(np.vstack([z, x ** 2]), axis=0)
    s1 = c1[f:] - c1[:-f]
    s2 = c2[f:] - c2[:-f]
    var = np.maximum(s2 / f - (s1 / f) ** 2, 0.0)
    den = np.sqrt(var.mean(axis=1)) + 1e-6
    scores = (acc / (f * B) / den).astype(np.float32)
    return scores[max(0, start):]


@dataclass(frozen=True)
class VoiceTemplate:
    """一套播报模板（每种语言/语音包一套）。"""

    name: str                 # 例如 "zh" / "en"
    feats: np.ndarray         # (frames, BANDS)，未标准化（指纹路径不用，保留兼容）
    onset_sec: float          # 模板内"播报起点"相对模板起点的位置（秒）
    source: str = ""          # 从哪个会话标定出来的（便于追溯）
    # 指纹参考的原始音频（2026-09-17 加）。**给了它就优先走指纹路径** ——
    # 实测指纹 AUC 0.98，而语音特征（频带能量/MFCC）只有 0.5~0.63。
    pcm: np.ndarray | None = None

    def trimmed(self, seconds: float = TEMPLATE_SEC) -> np.ndarray:
        n = max(4, int(seconds * ANALYSIS_RATE / HOP))
        return zscore(self.feats[:n])


def save_template(path: Path, tmpl: VoiceTemplate) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, feats=tmpl.feats, onset_sec=tmpl.onset_sec,
                        name=tmpl.name, source=tmpl.source)


def load_templates(directory: Path | str | None) -> list[VoiceTemplate]:
    """读目录下所有 *.npz 模板；目录不存在/没有模板就返回空列表（调用方据此不启用）。"""
    if directory is None:
        return []
    d = Path(directory)
    if not d.is_dir():
        return []
    out: list[VoiceTemplate] = []
    for f in sorted(d.glob("*.npz")):
        try:
            with np.load(f) as z:
                pcm = np.asarray(z["pcm"], dtype=np.float32) if "pcm" in z.files else None
                out.append(VoiceTemplate(
                    name=str(z["name"]), feats=np.asarray(z["feats"], dtype=np.float32),
                    onset_sec=float(z["onset_sec"]), source=str(z["source"]), pcm=pcm))
        except (OSError, KeyError, ValueError):
            continue
    return out


@dataclass(frozen=True)
class PlantHit:
    """一次播报命中。t_stream = 真实安放时刻，按"喂进来的音频流时间"计（秒）。"""

    t_stream: float
    score: float
    name: str


def scan_pcm(pcm: np.ndarray, templates: list[VoiceTemplate],
             threshold: float = THRESHOLD, template_sec: float = TEMPLATE_SEC) -> list[PlantHit]:
    """离线扫描整段音频（自检、标定复核用）。t_stream 是相对本段音频的秒数。"""
    if not templates:
        return []
    feats = band_features(pcm)
    tmpls = [(t, t.trimmed(template_sec)) for t in templates]
    return _scan_features(feats, tmpls, threshold)


def _scan_features(feats: np.ndarray, tmpls: list[tuple[VoiceTemplate, np.ndarray]],
                   threshold: float, from_pos: int = 0, self_offset: int = 0) -> list[PlantHit]:
    """在特征序列上找 ≥ 阈值的局部峰（同一事件 1 秒内不重复报）。

    只检查 pos ≥ from_pos 的位置（在线增量扫描用）；self_offset 是"这段特征在整条
    音频流里的起始帧号"，用于把 pos 换算回流时间。
    """
    hits: list[PlantHit] = []
    if len(feats) == 0 or not tmpls:
        return hits
    scores = np.full(len(feats), -9.0, dtype=np.float32)
    which = np.zeros(len(feats), dtype=np.int32)
    for k, (tmpl, tz) in enumerate(tmpls):
        if len(tz) > len(feats):
            continue
        s = corr_scores(feats, tz)
        if len(s) == 0:
            continue
        better = s > scores[:len(s)]
        scores[:len(s)] = np.where(better, s, scores[:len(s)])
        which[:len(s)] = np.where(better, k, which[:len(s)])
    last_t = -1e9
    for pos in range(max(0, from_pos), len(scores)):
        v = float(scores[pos])
        if v < threshold:
            continue
        if pos > 0 and scores[pos - 1] > v:
            continue
        if pos + 1 < len(scores) and scores[pos + 1] > v:
            continue
        tmpl = tmpls[int(which[pos])][0]
        t_stream = (pos + self_offset) * HOP / ANALYSIS_RATE + tmpl.onset_sec
        if t_stream - last_t >= REFRACTORY_SEC:
            hits.append(PlantHit(t_stream=t_stream, score=v, name=tmpl.name))
            last_t = t_stream
    return hits


def _standardize(feats: np.ndarray) -> np.ndarray:
    """按整个序列的每列均值/方差标准化（离线扫描用；在线走运行统计）。"""
    return (feats - feats.mean(axis=0, keepdims=True)) / (feats.std(axis=0, keepdims=True) + 1e-6)


def _ref_id(tmpl) -> str:
    """参考的对外标识：优先用来源片段名（build_fingerprints 写在 source 里），
    退回到模板名。实战统计就是按这个键累加的。"""
    src = getattr(tmpl, "source", "") or ""
    return src.split("-aligned")[0] or getattr(tmpl, "name", "fp")


class PlantVoiceDetector:
    """流式检测：喂 PCM，吐出"真实安放时刻"。缓冲区自动增长，内部只保留最近若干秒。"""

    def __init__(self, templates: list[VoiceTemplate], threshold: float = THRESHOLD,
                 template_sec: float = TEMPLATE_SEC, keep_sec: float = 12.0,
                 min_hits: int | None = None):
        self.templates = list(templates)
        self.threshold = threshold
        self.template_sec = template_sec
        # 指纹路径的命中数阈值（None = 用 `fingerprint.MIN_HITS` 的默认值）。
        # 2026-09-18 接成参数/配置项：它是"漏检 vs 误触发"的唯一旋钮，改它不该需要改代码。
        self.min_hits = min_hits
        self.keep_frames = int(keep_sec * ANALYSIS_RATE / HOP)
        self._pcm = np.zeros(0, dtype=np.float32)
        self._feats = np.zeros((0, BANDS), dtype=np.float32)
        self._pcm_frames_done = 0        # 已经算过特征的样本数
        self._next_pos = 0               # 下一个要检查的帧位置
        self._origin_frames = 0          # 被裁掉的帧数（用于把 pos 换算成流时间）
        self._last_hit_t = -1e9          # 上一次报出的命中（跨 feed 去重）
        self._tmpls: list[tuple[VoiceTemplate, np.ndarray]] = []
        # --- 指纹路径（2026-09-17）：有参考 pcm 的模板优先走它 ---
        self._fp_refs: list[tuple[VoiceTemplate, dict]] = []
        self._fp_pcm = np.zeros(0, dtype=np.float32)
        self._fp_fed = 0
        self._fp_last_scan = -(10 ** 9)
        self._fp_baseline = 4.0
        self.start()                     # 构造即可用（自检抓到的坑：忘了调 start 就永远 0 命中）

    # --- 生命周期 ---
    @property
    def active(self) -> bool:
        """没模板就不启用（调用方据此跳过采集，省 CPU）。"""
        return bool(self.templates)

    def start(self) -> None:
        self._tmpls = []
        self._fp_refs = []
        from . import fingerprint as fp
        for t in self.templates:
            tz = t.trimmed(self.template_sec)
            if len(tz) >= 4:
                self._tmpls.append((t, tz))
            if t.pcm is not None and len(t.pcm) > fp.NFFT:
                self._fp_refs.append((t, fp.reference_table(np.asarray(t.pcm, dtype=np.float32))))

    @property
    def stream_time(self) -> float:
        """当前已喂入的音频流总时长（秒）。

        指纹路径看 `_fp_fed`（样本数）；旧的频带路径看 `_pcm_frames_done`（帧数、步长 HOP）。
        ⚠️ 2026-09-18 修：以前只更新前者，指纹路径下这个值**恒为 0** ⇒
        ① 落盘窗口算不出来（live_*.wav 一个都没生成）；② 命中时刻被映射到*未来*
        （now + t）⇒ 状态机的 "0 ≤ now−plant ≤ 3.5" 直接拒绝 —— 即使分数恢复也会被丢掉。
        """
        if self._fp_refs or self._fp_fed:
            # 走指纹路径（有参考）或"只录素材"模式（没参考但在收 PCM）时，流时间都按样本数算
            return self._fp_fed / ANALYSIS_RATE
        return self._pcm_frames_done * HOP / ANALYSIS_RATE

    def feed(self, pcm: np.ndarray) -> list[PlantHit]:
        """喂一段 PCM（float32，[-1,1]，ANALYSIS_RATE 单声道），返回本次新命中。"""
        pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if pcm.size == 0:
            return []
        if self._fp_refs:
            return self._feed_fingerprint(pcm)
        if not self._tmpls:
            # 没有模板/参考时也**保留原始 PCM**（2026-09-18 修）。
            # 为什么：标定素材是"安放事件触发、从缓冲区里取 10 秒"存下来的，而缓冲区就是这里。
            # 从前这里直接 return，缓冲区永远是空的 => 没有参考的机器**永远攒不到素材** =>
            # 自动标定陷入死循环（实测：用户朋友那边打完一局，一直显示"还没有安放片段"）。
            keep = int(FP_WINDOW_SEC * ANALYSIS_RATE)
            self._fp_pcm = np.concatenate([self._fp_pcm, pcm])[-keep:]
            self._fp_fed += len(pcm)
            return []
        self._pcm = np.concatenate([self._pcm, pcm])
        # 只为"新完成的帧"算特征
        avail = 1 + max(0, (len(self._pcm) - NFFT)) // HOP
        if avail > self._pcm_frames_done:
            new = band_features(self._pcm[self._pcm_frames_done * HOP:])
            self._feats = np.concatenate([self._feats, new]) if len(self._feats) else new
            self._pcm_frames_done += len(new)
        # 在线：只扫描"上次之后"的新位置（增量、够快）。
        # ⚠️ 回退量必须 ≥ 最长模板：靠近末尾的那几帧因为后面帧还不够、暂时打不了分，
        # 如果直接跳过就永远看不到它们的峰值（自检抓到的 bug：在线 0 命中、离线 1 命中）。
        hits = _scan_features(self._feats, self._tmpls, self.threshold,
                              from_pos=self._next_pos, self_offset=self._origin_frames)
        longest = max((len(tz) for _, tz in self._tmpls), default=1)
        self._next_pos = max(0, len(self._feats) - longest)
        # 去重状态必须跨调用保持（每次 feed 都是新的 _scan_features，局部 last_t 不管用）
        fresh = [h for h in hits if h.t_stream - self._last_hit_t >= REFRACTORY_SEC]
        if fresh:
            self._last_hit_t = max(h.t_stream for h in fresh)
        self._trim()
        return fresh

    def _feed_fingerprint(self, pcm: np.ndarray) -> list[PlantHit]:
        """指纹路径：命中哈希按时间偏移分箱，最高箱就是分数；阈值用背景基线动态抬高。"""
        from . import fingerprint as fp
        keep = int(FP_WINDOW_SEC * ANALYSIS_RATE)
        self._fp_pcm = np.concatenate([self._fp_pcm, pcm])[-keep:]
        self._fp_fed += len(pcm)
        if self._fp_fed - self._fp_last_scan < int(FP_INTERVAL_SEC * ANALYSIS_RATE):
            return []
        self._fp_last_scan = self._fp_fed
        peaks = fp.peaks_of(self._fp_pcm)
        if len(peaks) < 4:
            return []
        qh = fp.hashes_of(peaks)
        best, best_off, best_ref, best_tmpl = 0, 0.0, "", None
        for tmpl, ref in self._fp_refs:
            hits, off = fp.best_offset_hits(qh, ref)
            if hits > best:
                best, best_off, best_ref, best_tmpl = hits, off, _ref_id(tmpl), tmpl
        # need 必须有**天花板**：基线只在"没命中"时上涨，而实战里枪声/语音会把
        # "最高命中数"经常推到 20~40 ⇒ 3×基线会涨到 60+ ⇒ 真身那 24~52 永远够不到
        # （2026-09-18 实测：采集电平正常 0.007~0.10、却零候选，这就是元凶）。
        floor = self.min_hits if self.min_hits is not None else fp.MIN_HITS
        need = min(FP_NEED_MAX, max(floor, self._fp_baseline + FP_BASELINE_MARGIN))
        # 暴露给调用方写日志：必须能区分"没匹配上（best 低）"和"被 need 挡住"。
        self.last_scores = (float(best), float(need), float(self._fp_baseline))
        if best < need:
            # 没命中就更新背景基线（用户第 4 点：阈值要高于实测基线）
            self._fp_baseline = 0.8 * self._fp_baseline + 0.2 * best
            return []
        base_sample = self._fp_fed - len(self._fp_pcm)
        # 参考片段里"播报起点"相对片段起点的偏移（onset_sec）也要算进去
        onset = float(getattr(best_tmpl, "onset_sec", 0.0) or 0.0)
        t_stream = base_sample / ANALYSIS_RATE + best_off + onset
        if t_stream - self._last_hit_t < REFRACTORY_SEC:
            return []
        self._last_hit_t = t_stream
        return [PlantHit(t_stream=t_stream, score=float(best), name=best_ref)]

    def pcm_window(self, end_stream_time: float, seconds: float) -> np.ndarray | None:
        """取"以 end_stream_time 结尾"的 seconds 秒音频（落盘用）。

        为什么需要它：落盘原来取的是"处理命中那一刻的最后 6 秒"，而主循环每 0.4 秒才轮询
        一次 ⇒ 播报在窗口里的位置每局差最多 0.4 秒 ⇒ 生成的参考指纹锚点语义不一致 ⇒
        表现为"归零每局偏得都不一样"。改成相对**命中时刻**的固定窗口，源头就一致了。
        """
        if len(self._fp_pcm) == 0:
            return None
        n = int(seconds * ANALYSIS_RATE)
        buf_end = self._fp_fed
        want_end = int(end_stream_time * ANALYSIS_RATE)
        lo = max(0, want_end - n)
        hi = min(buf_end, want_end)
        if hi - lo < n // 2:                      # 缓冲区里没有（还没喂到 / 已被裁掉）
            return None
        off = len(self._fp_pcm) - (buf_end - lo)
        start = max(0, off)
        end = start + (hi - lo)
        return self._fp_pcm[start:end].copy()

    def recent_pcm(self, seconds: float) -> np.ndarray | None:
        """最近 seconds 秒的原始 PCM（落盘/诊断用）。没数据返回 None。"""
        n = int(seconds * ANALYSIS_RATE)
        if len(self._pcm) == 0:
            return None
        return self._pcm[-n:].copy()

    def _trim(self) -> None:
        """把缓冲裁到最近 keep_frames 帧（位置换算靠 _origin_frames 记账）。"""
        if len(self._feats) <= self.keep_frames:
            return
        drop = len(self._feats) - self.keep_frames
        self._feats = self._feats[drop:].copy()
        drop_samples = drop * HOP
        self._pcm = self._pcm[drop_samples:].copy()
        self._pcm_frames_done -= drop
        self._next_pos = max(0, self._next_pos - drop)
        self._origin_frames += drop
