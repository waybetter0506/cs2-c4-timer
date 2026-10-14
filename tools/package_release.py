#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把产品打成可以直接卖出去的发行包（打包工具，不属于产品运行链路）。

产物长这样：

    dist/C4timer_<版本>/
        c4timer.exe              编译后的主程序（源码不在里面）
        _internal/               PyInstaller 的运行时与 numpy
        启动.bat                 自动标定 + 启动
        装GSI配置.bat            装 CS2 的 GSI 配置（唯一会写游戏目录的操作）
        体检.bat                 环境配置自检
        打包带走.bat             整包压成一个 zip，方便带去网吧
        指定游戏目录.bat         自动找不到游戏时的兜底
        使用说明.txt
        用户协议.txt
        第三方许可/              随产物分发的第三方组件许可与版权声明
        gsi/gamestate_integration_csc4.cfg

为什么用 PyInstaller 而不是 Nuitka（2026-09-19 决定）
--------------------------------------------------
两者都不限制打包后按什么许可证分发：
* PyInstaller 是 GPL-2.0-or-later **带 bootloader exception**，明确允许把程序
  打包后按任意许可证分发（例外只要求"用它的 bootloader 打出来的产物"不反过来
  限制别人，不影响你的代码用什么许可证）。
* Nuitka 是 AGPL-3.0 带 Runtime Library Exception v1.0，原文 "You may then convey
  such a combination under terms of your choice"。
选 PyInstaller 的实际原因是**本机没有 C 编译器**（MSVC 与 gcc 都没有），Nuitka 要现下
一份 MinGW 才能用；PyInstaller 自带预编译 bootloader，装完就能打。
以后有编译器想换 Nuitka，把 build_with_pyinstaller() 换掉即可，组装与校验部分不用动。

用法：
    python tools/package_release.py                    # 打当前 __version__
    python tools/package_release.py --version v1.0.0
    python tools/package_release.py --skip-build       # 只重新组装+校验（复用上次编译）
    python tools/package_release.py --skip-tests       # 跳过自检（不推荐）
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from c4timer import __version__ as CODE_VERSION  # noqa: E402
from c4timer.paths import is_frozen  # noqa: E402

PKG = ROOT / "packaging"
BUILD_DIR = ROOT / "dist" / "_build"
WORK_DIR = ROOT / "runtime" / "_pybuild"
LICENSE_DIR_NAME = "第三方许可"

# 发行包里必须有的东西（少一个就说明组装漏了）
REQUIRED = (
    "c4timer.exe",
    "启动.bat",
    "装GSI配置.bat",
    "体检.bat",
    "打包带走.bat",
    "指定游戏目录.bat",
    "使用说明.txt",
    "用户协议.txt",
    "gsi/gamestate_integration_csc4.cfg",
    LICENSE_DIR_NAME + "/README.txt",
)

# 绝不能出现的东西：我们的源码、游戏音频、私有数据、开发资料
FORBIDDEN_DIRS = {"c4timer", "tests", "tools", "docs", "_archive", "fixtures",
                  "sessions", "private", "packaging", "runtime", ".git", ".github"}
FORBIDDEN_SUFFIX = {".npz", ".npy", ".wav", ".mp3", ".pyc", ".pyo", ".spec"}
FORBIDDEN_FILES = {"config.json", "c4timer.log",
                   "calibration_stats.json", "reference_stats.json"}


# ----------------------------------------------------------------------
# 编译
# ----------------------------------------------------------------------

def build_with_pyinstaller() -> int:
    """调 PyInstaller 打 onedir（不是 onefile：程序要往自己旁边写 runtime/）。"""
    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir",                      # onedir：exe 旁边就是数据目录，onefile 会解到临时目录
        "--console",                     # 保留控制台窗口（用户要看日志）
        "--name", "c4timer",
        # 字节码优化级别 2（等价 -OO）：**去掉 docstring 与 assert**。
        # 为什么必须加：PyInstaller 不是"编译成机器码"，它装的是**字节码** ——
        # 用 pyinstxtractor 能把 PYZ 抠出来，模块名、函数名、字符串常量、
        # 以及**文档字符串**全都看得到。这个项目的设计说明大量写在 docstring 里
        # （阈值为什么取这个值、哪条弯路走过），那才是真正值钱的部分。
        # 级别 2 把它们从字节码里删掉；注释本来就在编译时被丢掉了。
        # 代价：__doc__ 变成 None。本程序不依赖运行时读 docstring。
        "--optimize", "2",
        "--distpath", str(BUILD_DIR),
        "--workpath", str(WORK_DIR),
        "--specpath", str(WORK_DIR),
        "--paths", str(ROOT),
        str(PKG / "launcher.py"),
    ]
    print("[编译] " + " ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))     # 不捕获输出：让编译日志直接刷在屏幕上
    if rc != 0:
        print(f"[X] PyInstaller 失败，退出码 {rc}")
        print("    没装？在能联网的机器上执行：python -m pip install --target runtime\\_bt pyinstaller")
        print("    然后设置 PYTHONPATH=runtime\\_bt 再跑本脚本。")
        return rc
    if not (BUILD_DIR / "c4timer" / "c4timer.exe").exists():
        print("[X] 编译完成了但找不到 c4timer.exe")
        return 1
    return 0


# ----------------------------------------------------------------------
# 第三方许可
# ----------------------------------------------------------------------

def _find_dist_license(dist_name: str) -> Path | None:
    """在已安装的发行版里找许可文本（不同包文件名不一样，按名字猜）。"""
    try:
        dist = md.distribution(dist_name)
    except md.PackageNotFoundError:
        return None
    for f in dist.files or []:
        name = Path(str(f)).name.lower()
        if name.startswith(("license", "copying", "notice")):
            p = Path(dist.locate_file(f))
            if p.exists() and p.is_file():
                return p
    return None


# 产物里的原生库对应的许可。清单里没列到的会标成"请核对"，不会被当成没问题。
NATIVE_LICENSES = {
    "python312.dll": "CPython 解释器（见 Python-PSF-License.txt）",
    "tcl86t.dll": "Tcl（见 Tcl-Tk-License.txt）",
    "tk86t.dll": "Tk（见 Tcl-Tk-License.txt）",
    "zlib1.dll": "zlib（见 zlib-License.txt）",
    "libffi-8.dll": "libffi（MIT 风格；上游 LICENSE 未内嵌）",
    "libcrypto-3.dll": "OpenSSL（Apache-2.0；上游 LICENSE 未内嵌）",
    "libssl-3.dll": "OpenSSL（Apache-2.0；上游 LICENSE 未内嵌）",
    "pyexpat.pyd": "Expat（MIT；上游 LICENSE 未内嵌）",
    "VCRUNTIME140.dll": "Microsoft VC++ 运行时可再分发组件",
    "VCRUNTIME140_1.dll": "Microsoft VC++ 运行时可再分发组件",
    "select.pyd": "CPython 标准库扩展（见 Python-PSF-License.txt）",
    "unicodedata.pyd": "CPython 标准库扩展（见 Python-PSF-License.txt）",
}


def _native_note(rel: Path) -> str:
    """产物里某个原生库的许可来自哪。判不出来就明说"请核对"，不装作没问题。"""
    name = rel.name
    top = rel.parts[0] if len(rel.parts) > 1 else ""
    if name.startswith("libscipy_openblas"):
        return "OpenBLAS（BSD-3；上游 LICENSE 未内嵌）"
    if name.startswith("msvcp140"):
        return "Microsoft VC++ 运行时可再分发组件"
    if top == "numpy":
        return "numpy 自身组件（见 numpy/ 下的完整许可集合）"
    if top in ("tcl8", "_tcl_data", "_tk_data"):
        return "Tcl/Tk（见 Tcl-Tk-License.txt）"
    if name in NATIVE_LICENSES:
        return NATIVE_LICENSES[name]
    if name.startswith("_") and name.endswith(".pyd"):
        return "CPython 标准库扩展（见 Python-PSF-License.txt）"
    return "（未分类，请核对）"


def collect_licenses(dest: Path, out: Path) -> list[str]:
    """把随产物分发的第三方许可文本抓出来，并生成一份清单。

    **这不是可选项**：产物里带着 CPython 运行时、Tcl/Tk、numpy，
    它们的许可都要求在分发时附上版权与许可声明。

    原则：**能从产物里拿的就从产物里拿**（版本一定对得上），
    拿不到的（解释器与打包器不在产物里）再从本机环境取。
    仍然拿不到文本的组件**不会被静默忽略** —— 它们会进清单的"未内嵌"一节，
    并且由本函数返回、在打包结尾打印出来。

    返回"未内嵌许可文本"的清单（空列表 = 全部齐了）。
    """
    dest.mkdir(parents=True, exist_ok=True)
    embedded: list[str] = []
    not_embedded: list[str] = []

    # 1) Tcl/Tk：产物里就带着 license.terms（_tk_data / _tcl_data）
    terms = sorted(p for p in out.rglob("license.terms") if "demos" not in p.parts)
    if terms:
        chunks = []
        for p in terms:
            body = p.read_text(encoding="utf-8", errors="replace").strip()
            chunks.append(f"===== 产物内 {p.relative_to(out).as_posix()} =====\n\n{body}")
        (dest / "Tcl-Tk-License.txt").write_text("\n\n".join(chunks) + "\n", encoding="utf-8")
        embedded.append(f"Tcl-Tk-License.txt（直接取自产物，{len(terms)} 份）")

    # 2) numpy 等：dist-info/licenses 就是上游给分发者准备的完整许可集合，整棵树搬过来
    for dist_dir in sorted(out.rglob("*.dist-info")):
        lic_dir = dist_dir / "licenses"
        if not lic_dir.is_dir():
            continue
        name = dist_dir.name.split("-")[0]
        shutil.copytree(lic_dir, dest / name, dirs_exist_ok=True)
        embedded.append(f"{name}/（{dist_dir.name} 自带的完整许可集合）")

    # 3) CPython：解释器在产物里，但许可文本不在
    py_lic = None
    for base in (getattr(sys, "base_prefix", ""), sys.prefix):
        cand = Path(base) / "LICENSE.txt"
        if cand.exists():
            py_lic = cand
            break
    if py_lic:
        shutil.copyfile(py_lic, dest / "Python-PSF-License.txt")
        embedded.append("Python-PSF-License.txt（CPython）")

    # 4) 打包器：bootloader 是它的，附上它的许可与例外说明
    pyi = _find_dist_license("pyinstaller")
    if pyi:
        shutil.copyfile(pyi, dest / "PyInstaller-License.txt")
        embedded.append("PyInstaller-License.txt（打包器 bootloader 例外）")

    # 5) 仓库里手工存放的那几份（本机环境里没有的，例如 zlib）
    static_dir = PKG / "licenses"
    if static_dir.is_dir():
        for p in sorted(static_dir.glob("*.txt")):
            shutil.copyfile(p, dest / p.name)
            embedded.append(f"{p.name}（仓库内静态存放）")

    # 6) 原生库清单：把产物里每个 dll/pyd 都列出来，标出它的许可来自哪儿
    internal = out / "_internal"
    natives = sorted(p for p in out.rglob("*")
                     if p.is_file() and p.suffix.lower() in (".dll", ".pyd"))
    rows: list[str] = []
    for p in natives:
        rel = p.relative_to(internal) if internal in p.parents else p.relative_to(out)
        note = _native_note(rel)
        rows.append(f"    {rel.as_posix():<52}{note}")
        if "未内嵌" in note or "请核对" in note:
            not_embedded.append(f"{rel.as_posix()} —— {note}")

    lines = [
        "本目录列出随本程序一起分发的第三方组件、它们的许可，以及版权声明。",
        "",
        "下面是本程序用到的别人的东西。",
        "",
        "── 已内嵌许可文本 ──",
        "",
    ]
    lines += [f"  · {item}" for item in embedded]
    lines += ["", "── 未内嵌许可文本（如要做严格合规，请把上游 LICENSE 补进本目录）──", ""]
    lines += ([f"  ! {item}" for item in not_embedded] if not_embedded
              else ["  （无）"])
    lines += [
        "",
        "── 产物内原生库完整清单 ──",
        "",
        "  文件（相对 _internal/）                              许可",
    ]
    lines += rows
    lines += [
        "",
        "说明：",
        "  · CPython 与 Tcl/Tk 由打包工具一并带入产物（解释器与悬浮窗运行时）。",
        "  · numpy 是本程序唯一的第三方 Python 依赖，用于音频指纹与频带分析。",
        "  · 打包工具本身不进入产物代码，仅按其 bootloader 例外使用。",
        "  · Microsoft VC++ 运行时可再分发组件按 Visual Studio 许可条款随附，无需额外声明。",
        "",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    (dest / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    for item in embedded:
        print(f"  [许可] {item}")
    for item in not_embedded:
        print(f"  [!] 未内嵌许可文本：{item}")
    return not_embedded


# ----------------------------------------------------------------------
# 组装
# ----------------------------------------------------------------------

def copy_crlf(src: Path, dst: Path) -> None:
    """bat/cfg 一律以 CRLF 落盘：cmd.exe 解析 LF-only 的 .bat 会吃掉行首字符。"""
    data = src.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)


def assemble(version: str) -> Path:
    out = ROOT / "dist" / f"C4timer_{version}"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    # 1) 编译产物
    built = BUILD_DIR / "c4timer"
    for item in sorted(built.iterdir()):
        dst = out / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)
    print(f"[组装] 程序本体：{built.name} -> {out.name}")

    # 2) 启动器：纯 ASCII + CRLF
    for name in ("启动.bat", "装GSI配置.bat", "体检.bat", "打包带走.bat",
                 "指定游戏目录.bat"):
        copy_crlf(PKG / name, out / name)

    # 3) 文本：带 BOM 写，记事本直接双击就能正确显示中文
    stamp = time.strftime("%Y-%m-%d")
    for name in ("使用说明.txt", "用户协议.txt"):
        body = (PKG / name).read_text(encoding="utf-8")
        body = body.replace("@VERSION@", version).replace("@DATE@", stamp)
        body = body.replace("\r\n", "\n")
        body = body.replace("\n", "\r\n")
        (out / name).write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

    # 4) GSI 配置（产品要能在没有源码的机器上自己装）
    copy_crlf(ROOT / "gsi" / "gamestate_integration_csc4.cfg",
              out / "gsi" / "gamestate_integration_csc4.cfg")

    # 5) 第三方许可
    return out


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------

def validate(out: Path) -> int:
    problems: list[str] = []
    warnings: list[str] = []

    for rel in REQUIRED:
        if not (out / rel).exists():
            problems.append(f"缺少必需文件：{rel}")

    for p in sorted(out.rglob("*")):
        rel = p.relative_to(out)
        if p.is_dir():
            if p.name in FORBIDDEN_DIRS:
                problems.append(f"不该出现的目录：{rel}/")
            continue
        if p.name in FORBIDDEN_FILES:
            problems.append(f"不该出现的文件：{rel}")
        if p.suffix.lower() in FORBIDDEN_SUFFIX:
            problems.append(f"不该出现的文件类型：{rel}")
        if p.suffix.lower() == ".py":
            # 我们的源码绝不能进包；别人的 .py 也要报出来看一眼
            if rel.parts[0] == "c4timer":
                problems.append(f"源码泄漏：{rel}")
            else:
                warnings.append(f"第三方 .py（通常无害，确认一下）：{rel}")

    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print()
    print("=" * 72)
    print(f"发行包校验：{out}")
    print("=" * 72)
    print(f"  文件数 {sum(1 for p in out.rglob('*') if p.is_file())}"
          f"    体积 {size / 1024 / 1024:.1f} MB")
    print(f"  必需项 {len(REQUIRED)} 个：{'齐全' if not problems else '有缺失'}")
    for w in warnings[:10]:
        print(f"  [!] {w}")
    if len(warnings) > 10:
        print(f"  [!] ……还有 {len(warnings) - 10} 条同类提示")
    if problems:
        print()
        for item in problems:
            print(f"  [X] {item}")
        print()
        print("  发行包**不可交付**：先修掉上面这些问题。")
        return 1
    print("  [OK] 没有源码、没有游戏音频、没有私有数据 —— 可以交付")
    return 0


# ----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="打发行包（编译 + 组装 + 校验）")
    ap.add_argument("--version", default=f"v{CODE_VERSION}",
                    help="产物目录名里的版本号，默认 v<代码里的 __version__>（即 v1.0.0）")
    ap.add_argument("--skip-build", action="store_true", help="复用上次编译结果，只重新组装")
    ap.add_argument("--skip-tests", action="store_true", help="跳过自检（不推荐）")
    ap.add_argument("--allow-frozen", action="store_true",
                    help="允许在打包后的环境里运行（默认拒绝）")
    args = ap.parse_args()

    if is_frozen() and not args.allow_frozen:
        print("[X] 现在跑在打包后的环境里，打出来的东西会不对。用源码跑这个脚本。")
        return 1

    if not args.skip_tests:
        print("[自检] python -m c4timer test")
        rc = subprocess.call([sys.executable, "-m", "c4timer", "test"], cwd=str(ROOT))
        if rc != 0:
            print("[X] 自检没过，先修好再打包（确认要打就加 --skip-tests）")
            return rc

    if not args.skip_build:
        rc = build_with_pyinstaller()
        if rc != 0:
            return rc
    elif not (BUILD_DIR / "c4timer" / "c4timer.exe").exists():
        print("[X] --skip-build 但上次的编译结果不在，去掉这个参数重跑")
        return 1

    out = assemble(args.version)
    missing = collect_licenses(out / LICENSE_DIR_NAME, out)
    rc = validate(out)
    if missing:
        print()
        print(f"  [!] 有 {len(missing)} 个原生库没有内嵌许可文本（清单见 "
              f"{LICENSE_DIR_NAME}/README.txt）。")
        print("      发行不受阻，但要做严格合规就把上游 LICENSE 补进那个目录。")
    print()
    print(f"  产物目录：{out}")
    print("  下一步：把整个目录压成 zip 发给别人，或者传成 GitHub Release 附件。")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
