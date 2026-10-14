"""从**自动落盘的安放片段**生成指纹参考。

这一段原来是 `tools/build_fingerprints.py`（2026-09-19 搬进包里）。搬家的原因很实际：
发行版是编译过的 exe，**不能带任何 .py 文件**（带了就等于把指纹算法送出去）。
而"打完第一局自动建参考"是产品在全新机器上唯一能长出音频精度的路径，
它必须在 exe 里面，所以逻辑必须落在包内。`tools/build_fingerprints.py` 现在只剩一个壳。

标签不用人工：程序在收到 GSI 安放包后 1 秒开始写 `runtime/audio_dumps/plant_*.wav`
（10 秒），这些片段**必然含整句播报**。

⚠️ 2026-09-18 的重要修正：旧版本用 `runtime/audio_dumps/2026....wav`（6 秒候选落盘）
从固定 3.0 秒处切参考。那批落盘是**命中后**才写的（窗口 = 触发前 4.5 秒 + 触发后 1.5 秒），
所以播报其实落在 4.50 秒处、只剩 1.5 秒 ✗ => 参考只有**半句话**，哈希天花板 13~17，
再怎么调阈值也分不开真身（实测实战片段只有 1~5 分）和误报。
新版本改用 10 秒安放片段，音频实测播报起点在片段内 4.65~5.45 秒，从 4.4 秒切 3.2 秒就够。

跨实例留一实测（方法：用"用户确认过的半句"当锚点滑动定位播报位置，再用 A 的 2.6 秒
当参考去打分**别的**落盘，无泄漏）：
    参考 A(121546) → 其它实例 26 / 10 / 6，负样本最高 13、中位 2
    参考 B(121853) → 其它实例 42 / 8 / 5 / 4，负样本最高 12、中位 1
对比半句话参考时期"真身只有 1~5 分"，这是量级上的改善。
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from .audio import fingerprint as fp
from .paths import runtime_dir

# 裁剪窗口（相对安放片段的起点）
REF_START_SEC = 4.4
REF_SEC = 3.2
# 裁剪起点 → 播报起点的**几何**偏移（实测 4.8−4.4=0.4；三次落盘 4.65/5.45/5.10，抖动 ±0.35）。
# ⚠️ 这里**只烧几何值**：2026-09-18 起，"播报起点 → 真实安放"那段（含采集管线延迟）是
# 产品侧常数 `config.audio.onset_bias_sec`（当前 0.50 秒），不再写进参考 ——
# 这样调时间只改配置一个数，不用重建参考。
REF_ONSET_SEC = 0.4
# 交叉验证门槛：候选参考与"基准参考"必须对得上这么多哈希才算同一句话
MIN_CROSS_HITS = 12
# 对齐门槛：与基准片段对比，整段至少要命中这么多哈希才认为"对的是同一句话"
MIN_ALIGN_HITS = 12
# 参考数量上限：检测器对每个参考各自打分取最大 => 参考越多误报上限越高
# （第二十三节 17 记过：5 个参考把弱命中的误报从 13 顶到 29）。按互证总分挑最好的若干个。
MAX_REFS = 5
# 参考的**误报上限**（只在显式加 `--screen-negatives` 时生效）。
# ⚠️ 2026-09-18 复核：这条筛查的**前提被推翻了** —— 用户说明自己打的一直是中文局，
# 而这里当作"负样本"的中文文件（`zh_template.wav` / `*_zh.wav`）其实装的是**同一句话**
# （只是被枪声盖住/被截断的弱命中）。所以它会把**好参考**误判成"误报高"丢掉：
# 实测 `124452`(24) 与 `124628`(29) 两个实例就是这么被筛掉的。
# 因此默认**不做**筛查；要开就必须先准备一套确认不含该语言播报的音频。
# 现行 4 个参考（121546 / 121853 / 124900 / 132615）正是**开着筛查**那次生成的（已实机验证），
# 想复现就加 `--screen-negatives`。
MAX_FALSE_HITS = 14
NEGATIVE_HINTS = ("audio_templates/zh_template.wav", "audio_dumps/*_zh.wav")
# 片段里必须真有"播报"：哈希太少说明裁到的是静音/纯噪声。
# 为什么必须挡：一旦把没内容的片段写成参考，之后自动标定会认为"已有参考，不覆盖"，
# 就再也修不回来了（新用户第一次标定常常只有 1 个片段，没有别的机会补救）。
MIN_REF_HASHES = 20
# 片段多到超过这个数就不再打印两两命中矩阵，只打一行摘要（2026-09-19 加）。
# 为什么：矩阵行数 = 片段数，92 段时打过一整屏 92×92 的数字，人根本看不了；
# 而这几行是给使用者看的诊断信息，不是给机器看的。**计算照旧**（排序要用它）。
MAX_PRINTED_MATRIX = 12


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def build(dumps: Path | None = None, out: Path | None = None,
          screen_negatives: bool = False) -> int:
    """按落盘片段生成参考指纹。返回 0 = 成功写出，非 0 = 没写（原因见打印）。

    参数默认取产品运行目录（`runtime/audio_dumps` 与 `runtime/audio_templates`）；
    显式传路径是为了让自检能在备份出来的片段上重跑，而不是动用户手上的参考。
    """
    dumps = Path(dumps) if dumps is not None else runtime_dir() / "audio_dumps"
    out = Path(out) if out is not None else runtime_dir() / "audio_templates"
    runtime = out.parent

    plants = sorted(dumps.glob("plant_*.wav"))
    if not plants:
        print("[X] 没有安放片段：先打一局（程序会在每次安放后落盘 plant_*.wav）")
        return 1
    n = int(REF_SEC * fp.RATE)
    start = int(REF_START_SEC * fp.RATE)

    full: list[tuple[str, np.ndarray]] = []
    for p in plants:
        pcm = read_wav(p)
        if len(pcm) < start + n:
            print(f"[skip] {p.name}: 只有 {len(pcm) / fp.RATE:.1f}s，"
                  f"装不下 {REF_START_SEC}s+{REF_SEC}s")
            continue
        full.append((p.name, pcm))
    if not full:
        print("[X] 没有一段够长")
        return 1

    # 落盘时点每次不同 => 播报在片段里的位置会差 ±0.4~0.65 秒（实测三次：
    # 121546/121853/122141 的播报分别在 4.80/5.45/5.10 秒）。固定窗口切会切歪，
    # 所以先按**内容对齐**到第一段（哈希时间偏移 = 同一句话在各自片段里的位置差），
    # 再从"同一个物理位置"开始切 —— 旧工具当年已证明这一步是必要的。
    base_nm, base_pcm = full[0]
    # 对齐锚点用**整段**。试过"用裁出来的 3.2 秒当锚点"（更干净、命中也更高），
    # 但实测更糟：它给出的偏移是 +3.5~+5.7 秒的**错峰**，被几何约束挡掉后会把
    # `121853` 这个最有用的参考弄丢（参考从 3 个掉到 2 个 ⇒ 实战分数从 19~25 掉到 7~13）。
    # 整段锚点命中数低（9~33）但偏移是**可信**的，宁可少收几个片段。
    base_h = fp.hashes_of(fp.peaks_of(base_pcm))
    cuts: list[tuple[str, np.ndarray]] = []
    for nm, pcm in full:
        if nm == base_nm:
            off, hits = 0.0, -1
        else:
            hits, off = fp.best_offset_hits(fp.hashes_of(fp.peaks_of(pcm)), base_h)
        s = int((REF_START_SEC + off) * fp.RATE)
        note = "基准" if hits < 0 else f"对齐命中 {hits}、偏移 {off:+.2f}s"
        if hits >= 0 and hits < MIN_ALIGN_HITS:
            print(f"[skip] {nm}: {note}（对不上，可能是别的声音）")
            continue
        # 几何约束**别再收紧**（2026-09-18 踩过）：曾经要求裁剪起点落在 3.5~5.6 秒
        # （按"播报在第 4.65~5.45 秒"推的），结果把播报偏后的那几局（5.25 / 6.35 秒）
        # 全部误杀。错峰（偏移 +3.5~+5.7 秒）本来就会被下面的"裁剪越界"挡掉，不需要它。
        if s < 0 or s + n > len(pcm):
            print(f"[skip] {nm}: {note} => 裁剪越界（{s / fp.RATE:.2f}s 起）")
            continue
        onset_at = (s + int(REF_ONSET_SEC * fp.RATE)) / fp.RATE
        print(f"[切] {nm}: {note} => 起点 {onset_at:.2f}s")
        cuts.append((nm, pcm[s:s + n]))
    if not cuts:
        print("[X] 一段都没切成")
        return 1

    # 先做一次质量下限检查：片段里必须真的有"播报"（哈希太少说明裁到的是静音/纯噪声）。
    for nm, c in cuts:
        n_hashes = sum(len(v) for v in fp.reference_table(c).values())
        if n_hashes < MIN_REF_HASHES:
            print(f"[X] {nm}: 切出来的片段只有 {n_hashes} 个哈希"
                  f"（要求 >= {MIN_REF_HASHES}）——这一局的播报可能没录到，或者声音太小")
            print("    没有写任何参考文件。再打一局，然后重新双击 启动.bat 重试即可。")
            return 1

    # 两两命中矩阵只用于"多片段时挑最稳的那一个"。新用户第一次标定常常只有 1 个片段，
    # 那就**跳过互证直接建参考**（2026-09-18 修：从前会打印
    # "没有任何片段能与它互证" 然后退出 => 新用户永远建不出第一个参考）。
    names = [nm for nm, _ in cuts]
    if len(cuts) == 1:
        print(f"只有 1 个片段（{names[0]}）：跳过互证，直接用它建参考"
              f"（多打一局再标定会更稳）")
    else:
        # 选"跟别人最对得上"的那一段当参考，去掉抖动大的异常片段
        tables = [(nm, fp.reference_table(c)) for nm, c in cuts]
        hit = {}
        for i, (nm, tb) in enumerate(tables):
            for j, (nm2, tb2) in enumerate(tables):
                if i != j:
                    hit[(nm, nm2)] = fp.best_offset_hits(tb2, tb)[0]
        if len(names) <= MAX_PRINTED_MATRIX:
            print("两两命中矩阵（行=参考，列=去打分的那段）：")
            print("      " + "".join(f"{n[6:12]:>8s}" for n in names))
            for a in names:
                print(f"  {a[6:12]:>6s}" + "".join(
                    "       -" if a == b else f"{hit[(a, b)]:8d}" for b in names))
        else:
            vals = sorted(hit.values())
            print(f"两两互证：{len(names)} 段，{len(vals)} 对，"
                  f"中位 {vals[len(vals) // 2]} 命中、最高 {vals[-1]} 命中"
                  f"（≥{MIN_CROSS_HITS} 才算互相印证；段数太多就不逐对打印了）")
        best_nm = max(names, key=lambda a: sum(hit[(a, b)] for b in names if b != a))
        peers = [b for b in names if b != best_nm and hit[(best_nm, b)] >= MIN_CROSS_HITS]
        print(f"\n选中 {best_nm}；互证片段 {peers if peers else '（无）'}")
        if not peers:
            worst = max((hit[(best_nm, b)] for b in names if b != best_nm), default=0)
            print(f"[X] 片段之间对不上（最高只有 {worst} 个命中，"
                  f"要求 >= {MIN_CROSS_HITS}）。")
            print("    意思是这几段录音里的播报太弱或者太吵，现在建出来的参考在实战中"
                  "也过不了阈值，所以这次不写任何文件。")
            print("    各片段电平（RMS，越大越有戏）：")
            levels = []
            for nm, c in cuts:
                rms = float(np.sqrt((c.astype(np.float64) ** 2).mean()))
                levels.append(rms)
                flag = "  <== 明显偏低" if rms < 0.01 else ""
                print(f"      {nm}: RMS={rms:.4f}{flag}")
            # ⚠️ 别在这条提示里写死"正常应该 >= 0.02"（2026-09-19 修）。
            # 实测本机 131 段**能建出好参考**的素材里，切出来的 3.2 秒有 24% 低于 0.02，
            # 中位是 0.038 —— 写死 0.02 会把一批正常素材说成"偏低"，把人往错方向带。
            # 所以只标"明显偏低"（本机历史上只有 2% 低于 0.01），再在整体偏低时另说一句。
            med = sorted(levels)[len(levels) // 2]
            print(f"      中位 {med:.4f}")
            if med < 0.02:
                print("    整体偏低：把系统音量调到 50% 以上（游戏内音量也别太低），"
                      "并确认声音是从**当前默认播放设备**出来的（用蓝牙耳机时最容易搞错）。")
            print("    怎么办：再打一局，然后重新双击 启动.bat —— 它会自动重试。"
                  "（没有写参考，所以下次还会自己重试；片段攒得越多越容易成功）")
            return 1
        if len(peers) < len(names) - 1:
            print(f"[注意] 有 {len(names) - 1 - len(peers)} 段对不上：那段可能被枪声盖住/"
                  f"窗口没对上，已排除")
        # 参考数量上限（2026-09-18 加）：检测器对每个参考各自打分再取最大 =>
        # 参考越多，误报上限越高（第二十三节 17 记过：5 个参考把中文弱命中的误报从 13 顶到 29）。
        # 按"与其它片段的互证总分"排序，最多保留互证最好的 MAX_REFS 个。
        try:
            from . import refstats as _rs
            _fired = {nm: _rs.fired(nm) for nm in names}
        except Exception:                       # noqa: BLE001 统计读不到就不加成
            _fired = {}
        ranked = sorted(names, key=lambda a: (-(1 if _fired.get(a, 0) else 0),
                                              -sum(hit[(a, b)] for b in names if b != a)))
        if len(ranked) > MAX_REFS:
            print(f"\n有 {len(ranked)} 个片段，只拿互证最好的 {MAX_REFS} 个当参考："
                  f"{', '.join(ranked[:MAX_REFS])}")
            print("（其余片段仍留在 runtime/audio_dumps 里，随时可以重标）")
            ranked = ranked[:MAX_REFS]
        keep_names = set(ranked)
        cuts = [(nm, c) for nm, c in cuts if nm in keep_names]

    # 误报筛查（2026-09-18 加，随后默认关闭）：检测器对每个参考各自打分再取最大
    # => **参考越多误报上限越高**，所以需要筛掉"会让误报变高"的参考。
    # ⚠️ 但**负样本前提后来被推翻**：这里当作负样本的中文文件其实装的是**同一句话**
    # （用户打的本来就是中文局），于是筛查会把好参考误丢（实测 124452/124628 被丢）。
    # 所以默认不做筛查（要做得显式加 `--screen-negatives`，且先准备真正的负样本）。
    negs: list[tuple[str, np.ndarray]] = []
    if screen_negatives:
        for hint in NEGATIVE_HINTS:
            for p in sorted(runtime.glob(hint)):
                if p.suffix == ".wav":
                    negs.append((p.name, read_wav(p)))
        if not negs:
            print("[注意] 开了筛查但没有负样本 => 参考数目不受约束")
    else:
        print("[跳过] 误报筛查未开启（默认；加 --screen-negatives 才做，见模块头注释）")
    keep: list[tuple[str, np.ndarray]] = []
    for nm, c in cuts:
        tb = fp.reference_table(c)
        worst, who = 0, "-"
        for nn, npcm in negs:
            h, _ = fp.best_offset_hits(fp.hashes_of(fp.peaks_of(npcm)), tb)
            if h > worst:
                worst, who = h, nn
        if negs and worst > MAX_FALSE_HITS:
            print(f"[丢弃] {nm}: 对已知非本句音频（{who}）命中 {worst} > "
                  f"{MAX_FALSE_HITS}，会把误报顶过产品阈值")
            continue
        keep.append((nm, c))
        if negs:
            print(f"[保留] {nm}: 误报最高 {worst}（{who}）<= {MAX_FALSE_HITS}")
    if not keep:
        print("[X] 误报筛查把参考全筛掉了：先查负样本是不是真的不含播报")
        return 1

    # 新机器上 runtime/audio_templates 还不存在（从没有人写过参考）=> 先建目录。
    # 2026-09-18 修：朋友那边 4 个片段都通过了互证，却在写文件时
    # FileNotFoundError: runtime\audio_templates\fp_01.npz（目录不存在）。
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("fp_*.npz"):          # 清掉旧参考，避免半句话参考混在里面
        old.unlink()
        print(f"[清理] {old.name}")
    # 每段（已对齐到同一物理位置）各出一个参考：检测器对每个参考各自打分再取最大
    # （用户的设计）。多个参考 = 多种枪声背景下的同一句话，抗噪更强。
    onset = REF_ONSET_SEC          # 只烧几何值；产品侧偏置在 config.audio.onset_bias_sec
    written = []
    for k, (nm, c) in enumerate(keep, 1):
        dst = out / f"fp_{k:02d}.npz"
        np.savez_compressed(dst, pcm=c.astype(np.float32),
                            feats=np.zeros((4, 24), dtype=np.float32),
                            onset_sec=onset, name="fp",
                            source=f"{nm}-aligned-len{REF_SEC}s")
        written.append(dst.name)
        print(f"[OK] {dst.name} ← {nm}"
              f"（哈希 {sum(len(v) for v in fp.reference_table(c).values())}）")
    # 顺手导一份 wav：让用户**听一遍**确认这段就是完整播报（音频不能靠数字确认）。
    # 用留下来的第一个参考（不是 best_nm —— 它可能正好被误报筛查丢掉）。
    dst_wav = out / "参考_请试听.wav"
    with wave.open(str(dst_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fp.RATE)
        w.writeframes(np.clip(keep[0][1] * 32768.0, -32768, 32767).astype(np.int16).tobytes())

    print(f"\n共 {len(written)} 个参考：{', '.join(written)}")
    print(f"[OK] 试听文件：{dst_wav.name}（{REF_SEC}s，onset={onset:.2f}s）"
          f"—— 确认里面是**完整一句**播报")
    return 0
