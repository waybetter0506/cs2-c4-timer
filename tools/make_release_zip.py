#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打可交付压缩包（工具，不属于产品运行链路）。

两种口味：

* `--lite`：**给使用者**的（默认）—— 源码 + 九个 bat + 教程 + 回归夹具 + 工具，
  不含 `docs/` 里的开发流水账。
* 不加 `--lite`：整包 —— 上面这些 + `docs/` 全部（给"要改这个项目的人"）。

两种都**不含任何游戏音频**（模板 / 参考指纹 / 录音）：那是 Valve 的素材，且换机器就失效。
脚本最后会自己检查一遍并报出来。

用法：
    python tools/make_release_zip.py                 # 精简版 -> dist/C4time_<版本>_lite.zip
    python tools/make_release_zip.py --full          # 整包   -> dist/C4time_<版本>.zip
    python tools/make_release_zip.py --version v0.1.2
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from c4timer import __version__ as CODE_VERSION  # noqa: E402

# 精简版要的顶层条目（目录会递归收，文件直接收）
LITE_TOP = ("c4timer", "tests", "tools", "gsi", "fixtures")
LITE_ROOT_FILES = ("README.md", "LICENSE", "requirements.txt", ".gitignore")
# 根目录的教程（2026-09-18 从 docs/ 挪到根目录：解压后一眼就能看到）
TUTORIAL = "使用教程+原理.md"
# 整包相对精简版多出来的
FULL_EXTRA = ("docs",)

# 永远不收：运行产物、缓存、历史音频、打包输出、打包/签名用的私密文件
SKIP_DIRS = {"runtime", "sessions", "dist", "build", "private", "__pycache__", ".git", ".idea", ".vscode"}
SKIP_SUFFIX = {".pyc", ".pyo", ".wav", ".npz", ".npy", ".zip", ".log"}
AUDIO_SUFFIX = {".wav", ".npz", ".npy", ".mp3"}


def want(rel: Path, lite: bool) -> bool:
    """这个仓库相对路径要不要收进包。"""
    parts = rel.parts
    if any(p in SKIP_DIRS for p in parts):
        return False
    if rel.suffix.lower() in SKIP_SUFFIX:
        return False
    if rel.name.startswith(".") and rel.name not in (".gitignore",):
        return False
    top = parts[0]
    if top in LITE_TOP:                        # 目录内容：两种口味都要
        return True
    if len(parts) == 1 and (top in LITE_ROOT_FILES or top.endswith((".bat", ".md"))):
        return True                            # 根目录文件、所有启动器、根目录的教程
    return (not lite) and top in FULL_EXTRA    # docs/ 只进整包


def main() -> int:
    ap = argparse.ArgumentParser(description="打可交付压缩包")
    ap.add_argument("--full", action="store_true", help="打整包（含 docs/）")
    ap.add_argument("--version", default=CODE_VERSION, help="包名里的版本号（默认取代码里的 __version__）")
    ap.add_argument("--out", default=None, help="输出路径（默认 dist/ 下）")
    args = ap.parse_args()

    lite = not args.full
    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and want(p.relative_to(ROOT), lite))
    if not files:
        print("[X] 一个文件都没匹配上，检查 include 规则")
        return 1

    out = Path(args.out) if args.out else (
        ROOT / "dist" / f"C4time_{args.version}{'_lite' if lite else ''}.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            name = p.relative_to(ROOT).as_posix()
            if p.suffix.lower() in (".bat", ".cmd", ".cfg"):
                # 启动器与 GSI 配置**必须以 CRLF 进包**：cmd.exe 解析 LF-only 的 .bat 会吃掉
                # 行首字符（本项目实测踩过），而压缩包是从工作区打的、工作区可能是 LF。
                # 在写包这一步强制规范化，比依赖 git 的 autocrlf 更可靠。
                z.writestr(name, p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            else:
                z.write(p, name)
        # 教程额外写一份 .txt：解压后双击就能用记事本打开（不是所有机器都关联了 .md），
        # 内容在打包时从 .md 生成，不会与正本脱节。
        tut = ROOT / TUTORIAL
        if tut.exists():
            body = tut.read_text(encoding="utf-8")
            z.writestr(TUTORIAL.replace(".md", ".txt"),
                       "（本文件是 " + TUTORIAL + " 的纯文本副本，内容相同；"
                       "Markdown 版本在仓库里渲染更好看）\r\n\r\n"
                       + body.replace("\r\n", "\n").replace("\n", "\r\n"))

    audio = [p for p in files if p.suffix.lower() in AUDIO_SUFFIX]
    bat = [p.name for p in files if p.suffix.lower() == ".bat"]
    print(f"[OK] {'精简版' if lite else '整包'}：{len(files)} 个文件，"
          f"{out.stat().st_size / 1024:.0f} KB -> {out}")
    print(f"     启动器 {len(bat)} 个：{'、'.join(sorted(bat))}")
    print(f"     音频类文件：{len(audio)} 个" + ("（应当是 0）" if audio else " OK"))
    if audio:
        for p in audio[:5]:
            print(f"       [!] {p.relative_to(ROOT)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
