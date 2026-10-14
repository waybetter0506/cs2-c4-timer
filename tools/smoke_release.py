#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包后冒烟测试：验证**冻结路径**在真 exe 下确实成立。

为什么要单独跑这一遍：源码环境下 `paths.is_frozen()` 恒为 False，数据目录就是仓库根；
也就是说"只有打包后才生效"的那一半逻辑，四百多项自检里**一项都覆盖不到**。
这个脚本把真 exe 跑起来验它，是打包流程的最后一道关。

用法：
    python tools/smoke_release.py                          # 取 dist/ 下最新的 C4timer*
    python tools/smoke_release.py --release dist/C4timer_v1.0.0
    python tools/smoke_release.py --keep                   # 跑完不删临时目录

（2026-09-25 精简：原来 15 项里有 11 项在测一个已经删掉的启动闸门，那部分整体去掉，
留下"冻结路径 + 三个子命令真的能跑"这些在任何版本都成立的东西。）
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def newest_release() -> Path | None:
    """dist/ 下最新的发行包目录。

    只认目录：`dist/` 里还躺着历史源码 zip（`C4time_v0.1.26_lite.zip` 之类），
    它们名字也以 C4time 开头，混进来会选错。
    """
    cands = sorted((p for p in (ROOT / "dist").glob("C4timer*") if p.is_dir()),
                   key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def other_instance_running() -> bool:
    """当前会话里有没有别的 c4timer 在跑。

    它握着单实例命名互斥体，会让被测 exe 的 `run` 直接退出 —— 冒烟就挂在一项
    与被测代码完全无关的用例上（2026-09-19 踩过：用户自己那份还开着）。
    所以先探一下：探到就明说"先关掉它"，别让人去猜。

    做法是**自己试着拿一次互斥体**：拿得到说明没人占（立刻还回去），拿不到就是有人占。
    """
    try:
        from c4timer.single import SingleInstance
    except Exception:                              # noqa: BLE001 探不到就当没有
        return False
    si = SingleInstance()
    if si.acquire():
        si.release()
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="打包后冒烟测试")
    ap.add_argument("--release", default=None, help="发行包目录（默认取 dist/ 下最新的）")
    ap.add_argument("--keep", action="store_true", help="跑完保留临时目录")
    args = ap.parse_args()

    if other_instance_running():
        print("[X] 已经有一个 c4timer 在运行。它占着单实例互斥体，被测 exe 的 run 会直接退出，")
        print("    冒烟会挂在一条和被测代码无关的用例上。先把它关掉（关窗口或 Ctrl+C）再跑。")
        return 2

    rel = Path(args.release).resolve() if args.release else newest_release()
    if rel is None or not (rel / "c4timer.exe").exists():
        print("[X] 找不到发行包（先跑 python tools/package_release.py）")
        return 2

    work = ROOT / "runtime" / "_smoke"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    for item in rel.iterdir():
        dst = work / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)

    results: list[tuple[bool, str]] = []

    def check(cond: bool, label: str, detail: str = "") -> None:
        results.append((bool(cond), label))
        print(f"  [{'OK' if cond else 'X '}] {label}" + (f"   {detail}" if detail else ""))

    def exe(argv: list[str]) -> tuple[int, str]:
        p = subprocess.run([str(work / "c4timer.exe")] + argv, cwd=str(work),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    print("=" * 72)
    print(f"打包后冒烟测试：{rel}")
    print("=" * 72)

    # 1) exe 起得来、版本号打得出来
    rc, out = exe(["--version"])
    check(rc == 0 and "c4timer" in out, "exe 能启动并打印版本", f"rc={rc} {out.strip()}")

    # 2) 体检：证明 cs2path 在冻结环境里能跑通（注册表 + 扫盘 + 读 vdf）
    rc, out = exe(["doctor"])
    check(rc == 0, "doctor 在打包后能跑通", f"实际 {rc}")
    check("CS2 安装" in out, "体检里带 CS2 安装那一段")

    # 3) 标定子命令（新机器第一次长出音频精度走的就是它）
    rc, out = exe(["calibrate"])
    check(rc == 0, "calibrate 子命令在打包后能跑通", f"实际 {rc}")
    check("跳过" in out or "还没有安放片段" in out, "calibrate 给出可读的说明")

    # 4) 真的跑起来两秒（不装 HUD，不去碰游戏）
    rc, out = exe(["run", "--seconds", "2", "--no-hud"])
    check(rc == 0, "run --seconds 2 --no-hud 正常退出", f"实际 {rc}")

    # 5) 冻结路径：数据必须落在 exe 旁边，而不是打包内部或别的地方
    rt = work / "runtime"
    check(rt.is_dir(), "运行期数据目录在 exe 旁边（冻结路径解析正确）", str(rt))
    check((rt / "c4timer.log").exists(), "日志写进了 exe 旁边的 runtime/")

    print()
    bad = [label for ok, label in results if not ok]
    print(f"合计 {len(results) - len(bad)}/{len(results)} 通过")
    for label in bad:
        print(f"  [X] {label}")
    print(f"临时目录：{work}" + ("（已保留）" if args.keep else "（跑完即删）"))
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
