"""自动标定：有素材、又还没参考指纹时，静默地把参考建出来。

这一段原来是 `tools/auto_calibrate.py`（2026-09-19 搬进包里）。原因和 `buildrefs.py` 一样：
发行版不能带 .py，而"打完第一局，下次启动就自动拥有音频精度"必须在 exe 里。
`tools/auto_calibrate.py` 现在只剩一个壳，`启动.bat` 那套开发流程一个字都不用改。

为什么要有它：使用者打完第一局后，程序已经自动存下了每次安放的 10 秒片段
（`runtime/audio_dumps/plant_*.wav`）—— 那就没必要再让他记得双击某个标定脚本。

安全边界（三条都写进代码而不只是注释）：

* **只在"一个参考都没有"时动手** —— 已经有 `fp_*.npz` 就直接退出，绝不覆盖标定好的参考；
* 片段不够就什么都不做（还没打过局而已）；
* 只写 `runtime/audio_templates/`，不碰游戏目录、不碰配置。
"""

from __future__ import annotations

import time
from pathlib import Path

from . import buildrefs
from .paths import runtime_dir

# 已有参考时，又攒够这么多新片段才自动重标（"打得越多越准"的检测侧那一半）。
# 不能每次启动都重建：参考集合一变，自成长的观测就要作废（分数不可跨参考集比较）。
NEW_CLIPS_TO_REBUILD = 5
# "整体下滑"触发的重标最多这么久一次：一重建，自成长的观测就按代作废，
# 所以不能让它在分数波动时反复重建。
DEGRADE_MIN_INTERVAL_SEC = 6 * 3600


def _read_marker(marker: Path) -> tuple[str, float]:
    """读标记：上次建参考时**用到的最新片段名**，以及建立时间。

    2026-09-19 改（必须改，否则会出大问题）：第一个字段以前是"片段总数"。
    落盘现在有保留上限（`app.KEEP_PLANT_DUMPS`），总数到顶就不再增长
    => `fresh = 总数 − 上次总数` 恒为 0 => **自动重标从此永久停摆**，
    用户再怎么打也不会更新参考。改成记"上次用到的最新片段名"就与总数无关：
    片段名里就是时间，字典序 = 时间序，比它大的就是新片段。

    旧格式（第一个字段是数字）一律当成"不知道用到了哪一段"，这次照常重建一次。
    """
    try:
        parts = marker.read_text(encoding="utf-8").split()
    except OSError:
        return "", 0.0
    if not parts:
        return "", 0.0
    newest = "" if parts[0].isdigit() else parts[0]     # 数字 = 旧格式
    try:
        built_at = float(parts[1]) if len(parts) > 1 else 0.0
    except ValueError:
        built_at = 0.0
    return newest, built_at


def fresh_clips(clips: list[Path], newest_used: str) -> int:
    """比 `newest_used` 更新的片段有几个。

    `newest_used` 为空 = 没有可用的标记（第一次跑 / 旧格式 / 标记被删）=> 全算新片段。
    """
    if not newest_used:
        return len(clips)
    return sum(1 for p in clips if p.name > newest_used)


def run(dry_run: bool = False) -> int:
    dumps = runtime_dir() / "audio_dumps"
    templates = runtime_dir() / "audio_templates"
    clips = sorted(dumps.glob("plant_*.wav"))
    refs = sorted(templates.glob("fp_*.npz"))
    marker = templates / ".build_marker"
    newest_used, built_at = _read_marker(marker)

    if not clips:
        print("[跳过] 还没有安放片段（打一局后，程序会为每次安放自动存 10 秒音频）")
        return 0
    fresh = fresh_clips(clips, newest_used)
    # 实战分数整体下滑也触发重标（换语音包 / 换设备 / 改音量之后常见）
    try:
        from . import refstats
        degraded, cur, base = refstats.degraded()
    except Exception:                             # noqa: BLE001 统计读不到就当没下滑
        degraded, cur, base = False, None, None
    too_soon = (time.time() - built_at) < DEGRADE_MIN_INTERVAL_SEC if built_at else False
    if refs and fresh < NEW_CLIPS_TO_REBUILD and not (degraded and not too_soon):
        print(f"[跳过] 已有 {len(refs)} 个参考指纹，又攒了 {fresh} 个新片段"
              f"（满 {NEW_CLIPS_TO_REBUILD} 个才自动重标，所以不覆盖）")
        return 0

    if not refs:
        why = "还没有参考指纹"
    elif degraded and not too_soon:
        why = f"实战分数整体下滑（近期中位 {cur:.1f} 低于基线 {base:.1f}）"
    else:
        why = f"攒够了 {fresh} 个新片段"
    print(f"[自动标定] {why}，共有 {len(clips)} 个安放片段 => 现在建参考")
    if dry_run:
        print("           （--dry-run：只看计划，不写文件）")
        return 0

    rc = buildrefs.build()
    if rc == 0:
        try:                                  # 记下这次用到了最新的哪一段，下次据此判断"攒够没有"
            marker.write_text(f"{clips[-1].name} {time.time():.0f}\n", encoding="utf-8")
        except OSError:
            pass
        print("[自动标定] 完成：以后每局都会听播报校准（日志里能看到「播报候选 -> 确认」）")
    else:
        print("[自动标定] 没成功（不影响使用：精度退回 GSI 估算）；"
              "下次双击 启动.bat 会自动再试")
    return 0 if rc == 0 else rc
