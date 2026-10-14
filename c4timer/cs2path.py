"""定位 CS2 的 GSI 配置目录并体检（**只读**，不写游戏目录）。

为什么需要它：本机沙箱写不进 D 盘的游戏目录，cfg 必须由用户自己装。
那么"到底装上没有、装的是哪一版、端口对不对"就成了最常见的坑 ——
1 代就曾在"游戏到底有没有加载我们的 cfg"上耗掉大量时间。
这里把它变成一条命令就能问清的事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

GAME_DIR_NAME = "Counter-Strike Global Offensive"
# CS2 的目录名至今仍是 "Counter-Strike Global Offensive"，不是 "Counter-Strike 2"
CFG_SUBPATHS = (Path("game/csgo/cfg"), Path("game/core/cfg"))
CFG_GLOB = "gamestate_integration_*.cfg"
OUR_CFG_NAME = "gamestate_integration_csc4.cfg"

# 各盘符下值得试的 Steam 目录名（相对盘根）。
#
# ⚠️ 2026-09-19 修：这里以前是五条**写死的完整路径**
# （`C:\Program Files (x86)\Steam` / `C:\Program Files\Steam` / `D:\Steam` / `E:\Steam` /
#  `F:\Steam`），于是"Steam 装在 D 盘且带 Program Files (x86)"这种最常见的装法一条都不沾
# —— 一位使用者的机器就这么中了：他的游戏在 `D:\Program Files (x86)\Steam\steamapps\common\...`，
# 找游戏目录返回空，**体检和「装GSI配置.bat」双双失效**，只能手动装 cfg。
# 教训：**按规律找，不要按路径找** —— 路径永远列不完，规律（盘符 × 常见目录名）能覆盖。
_STEAM_DIR_NAMES = (
    r"Steam",
    r"Program Files (x86)\Steam",
    r"Program Files\Steam",
    r"Games\Steam",
    r"SteamLibrary",                       # 直接当库根用（它的 steamapps 就在下面）
)
# 注：`Program Files (x86)\Steam\Steam` 看着像"双重 Steam"，试过**别加** ——
# 实测那个目录在标准安装里确实存在（Steam 自己的 cached/games 缓存），
# 于是每个正常机器都会多出一条假库根。

_PATH_RE = re.compile(r'"path"\s*"([^"]+)"')
_URI_RE = re.compile(r'"uri"\s*"([^"]+)"')
_PORT_RE = re.compile(r":(\d+)")


def manual_game_dir() -> str:
    """用户在 config.json 里手填的游戏目录（没填就是空串）。"""
    try:
        from .config import load
        return (load().cs2.game_dir or "").strip()
    except Exception:                      # noqa: BLE001 配置读不了就当没填
        return ""


def _looks_like_game_dir(cand: Path) -> bool:
    """像不像 CS2 的游戏根目录（判据是下面有 game\\csgo 或 game\\core）。"""
    try:
        return (cand / "game" / "csgo").is_dir() or (cand / "game" / "core").is_dir()
    except OSError:
        return False


def resolve_manual(raw: str) -> list[Path]:
    """把用户填的那串路径解释成**游戏根目录**列表。

    宽容到什么程度：下面这些**粘哪一个都认**（用户最容易复制到的是第 2 种）——
      · `...\\Counter-Strike Global Offensive\\game\\csgo\\cfg`  （cfg 目录本身）
      · `...\\Counter-Strike Global Offensive\\game\\csgo`        （csgo 目录）
      · `...\\Counter-Strike Global Offensive`                    （游戏根目录）
      · `...\\Steam\\steamapps\\common`                           （common 目录）
      · `...\\Steam\\steamapps`                                   （steamapps）
      · `...\\Steam`                                              （Steam 根目录）
    认不出来就返回空列表，由调用方打印"没认出来"。

    ⚠️ 判据必须是"**下面真的有 game\\csgo**"，不能只判"这个目录存在" ——
    后者会让 `C:\\` 也算数，于是"已记住：C:\\"这种荒唐结果就出来了。
    """
    text = (raw or "").strip().strip('"').strip()
    if not text:
        return []
    try:
        p = Path(text.replace("/", "\\"))
    except (OSError, ValueError):
        return []
    if not p.is_dir():
        return []

    out: list[Path] = []

    # 1) 用户可能粘到很深的一层：从它本身往上退，最多 4 层就能到游戏根目录
    #    （cfg → csgo → game → 游戏根）
    node: Path | None = p
    for _ in range(4):
        if node is None:
            break
        if _looks_like_game_dir(node):
            out.append(node)
            break
        parent = node.parent
        node = parent if parent != node else None

    # 2) 粘的是 Steam 根目录 / steamapps / common 时，往下找一层
    for cand in (p / GAME_DIR_NAME,
                 p / "steamapps" / "common" / GAME_DIR_NAME,
                 p / "common" / GAME_DIR_NAME):
        if _looks_like_game_dir(cand) and cand not in out:
            out.append(cand)
    return out


def _drive_letters() -> list[str]:
    """当前存在的盘符。

    用 `GetLogicalDrives` 拿位图 —— **不去碰盘**。不要写成对每个盘
    `Path("X:\\\\").exists()`：那会让空光驱转起来、断开的网络盘卡住几秒。
    拿不到（非 Windows / 调不动）就退回只试 C。
    """
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()      # type: ignore[attr-defined]
    except Exception:                                          # noqa: BLE001
        return ["C"]
    letters = [chr(ord("A") + i) for i in range(26) if mask & (1 << i)]
    return letters or ["C"]


def _scan_common_steam_dirs() -> list[Path]:
    """扫各盘符下的常见 Steam 目录名（绿色版 / 非默认盘靠这一条）。"""
    out: list[Path] = []
    for letter in _drive_letters():
        for sub in _STEAM_DIR_NAMES:
            p = Path(f"{letter}:\\") / sub
            try:
                if p.is_dir() and p not in out:
                    out.append(p)
            except OSError:
                continue
    return out


def _steam_from_registry() -> list[Path]:
    """从注册表问 Steam 装在哪 —— 覆盖面最大的一条。

    Steam 官方安装器一定会把自己写进注册表，所以不论装在哪个盘、路径里有没有空格，
    这里都能拿到。绿色版/免安装版没有这一项，由 `_scan_common_steam_dirs` 兜底。
    注意 `SteamPath` 的值是正斜杠形式（`c:/program files (x86)/steam`），要转回来。
    """
    try:
        import winreg
    except ImportError:                    # 非 Windows：本项目不支持，但别在这里炸
        return []
    out: list[Path] = []
    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    for hive, sub, value in keys:
        try:
            with winreg.OpenKey(hive, sub) as key:
                raw, _kind = winreg.QueryValueEx(key, value)
        except OSError:
            continue
        try:
            p = Path(str(raw).replace("/", "\\"))
        except (OSError, ValueError):
            continue
        if p.is_dir() and p not in out:
            out.append(p)
    return out


def library_roots() -> list[Path]:
    """所有 Steam 库目录（含 Steam 本体所在的那个）。

    三条来源，按可靠性排：

    1. **注册表**里 Steam 自己记的安装路径；
    2. **扫各盘符**下的常见目录名（绿色版、非默认盘、盘符超过 H 的）；
    3. 从上面每个根读 `steamapps/libraryfolders.vdf`，把**另外挂的库**也捞出来
       （这条一直在，负责"游戏装在第二个库"的情况）。
    """
    roots: list[Path] = []
    for steam in _steam_from_registry() + _scan_common_steam_dirs():
        vdf = steam / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            try:
                text = vdf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for raw in _PATH_RE.findall(text):
                p = Path(raw.replace("\\\\", "\\"))
                if p.is_dir() and p not in roots:
                    roots.append(p)
        if steam.is_dir() and steam not in roots:
            roots.append(steam)
    return roots


def find_game_dirs() -> list[Path]:
    """游戏根目录。手填的优先，然后自动找。"""
    out: list[Path] = list(resolve_manual(manual_game_dir()))
    for root in library_roots():
        game = root / "steamapps" / "common" / GAME_DIR_NAME
        if game.is_dir() and game not in out:
            out.append(game)
    return out


def find_cfg_dirs() -> list[Path]:
    out: list[Path] = []
    for game in find_game_dirs():
        for sub in CFG_SUBPATHS:
            d = game / sub
            if d.is_dir() and d not in out:
                out.append(d)
    return out


@dataclass
class CfgInfo:
    path: Path
    uri: str = ""
    port: int | None = None
    has_bomb: bool = False
    has_phase_countdowns: bool = False
    has_allplayers_state: bool = False
    is_ours: bool = False
    size: int = 0

    @property
    def blockers(self) -> list[str]:
        """会导致"收不到炸弹信息"的问题。"""
        problems = []
        if not self.has_bomb:
            problems.append("没有请求 bomb 组件")
        if not self.has_phase_countdowns:
            problems.append("没有请求 phase_countdowns 组件")
        if self.port is None:
            problems.append("uri 里解析不出端口")
        return problems


def inspect(path: Path) -> CfgInfo:
    info = CfgInfo(path=path, is_ours=path.name == OUR_CFG_NAME)
    try:
        raw = path.read_bytes()
    except OSError:
        return info
    info.size = len(raw)
    if raw[:3] == b"\xef\xbb\xbf":
        # Valve 官方文档明写：cfg 带 UTF8-BOM 就不会被加载
        info.uri = "!!! 带 UTF8-BOM，游戏不会加载 !!!"
    text = raw.decode("utf-8", errors="replace")
    m = _URI_RE.search(text)
    if m:
        info.uri = m.group(1)
        pm = _PORT_RE.search(info.uri)
        if pm:
            info.port = int(pm.group(1))
    info.has_bomb = bool(re.search(r'"bomb"\s*"1"', text))
    info.has_phase_countdowns = bool(re.search(r'"phase_countdowns"\s*"1"', text))
    info.has_allplayers_state = bool(re.search(r'"allplayers_state"\s*"1"', text))
    return info


@dataclass
class Diagnosis:
    cfg_dirs: list[Path] = field(default_factory=list)
    cfgs: list[CfgInfo] = field(default_factory=list)

    @property
    def ours(self) -> list[CfgInfo]:
        return [c for c in self.cfgs if c.is_ours]

    @property
    def ok(self) -> bool:
        return any(not c.blockers for c in self.ours)


def diagnose() -> Diagnosis:
    d = Diagnosis(cfg_dirs=find_cfg_dirs())
    for cfg_dir in d.cfg_dirs:
        for path in sorted(cfg_dir.glob(CFG_GLOB)):
            d.cfgs.append(inspect(path))
    return d


# ---------------------------------------------------------------------------
# 安装 GSI 配置（本项目**唯一**会写游戏目录的操作，2026-09-18 加）
# ---------------------------------------------------------------------------


def install_our_cfg(template: Path) -> tuple[Path | None, str]:
    """把仓库里的 GSI 配置模板复制到 CS2 的 cfg 目录，返回 (写好的路径, 一句结果说明)。

    为什么可以由程序来写：这是 **Valve 官方文档化的 GSI 配置文件**，属于 README
    「安全边界」里明确列出的"会做的事"；除它之外程序不碰游戏目录里的任何东西
    （也只写 `gamestate_integration_csc4.cfg` 这一个文件名，不删不改别人的 cfg）。

    找不到目录、或模板读不到时返回 (None, 原因)，由调用方打印给用户看。
    """
    dirs = find_cfg_dirs()
    if not dirs:
        return None, "没找到 CS2 的 cfg 目录（游戏装了吗？Steam 库路径是否常规？）"
    # 优先 game/csgo/cfg —— 游戏实际读取的那一个；没有才退回找到的第一个
    target = next((d for d in dirs if d.parent.name == "csgo"), dirs[0])
    dst = target / OUR_CFG_NAME
    try:
        text = template.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"读不到模板 {template}（{exc}）"
    existed = dst.exists()
    # 统一按 CRLF 写入：游戏里那份能正常工作的是 CRLF；LF 在多数 KeyValues 解析器上也能读，
    # 但没必要赌 —— 这里显式转换，不依赖 Python 版本的 newline 参数（3.10+ 才有）。
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
    if existed:
        try:
            if dst.read_bytes() == data:
                return dst, "已经装好，内容与模板一致（无需改动）"
        except OSError:
            pass
    try:
        dst.write_bytes(data)
    except OSError as exc:
        return None, f"写不进去 {dst}（{exc}）—— 可能需要管理员权限，或手动复制"
    return dst, ("已更新（覆盖了旧版本）" if existed else "已装好")


# ---------------------------------------------------------------------------
# 「游戏到底有没有加载我们的 cfg」—— 文件存在 ≠ 游戏加载了
# ---------------------------------------------------------------------------


def recent_game_logs(limit: int = 3, game: Path | None = None) -> list[Path]:
    """最近的游戏日志。CS2 每次启动都会写一行 Loading Game State Integration。

    `game` 给定时**只看那一个安装** —— 体检要按安装分组显示（哪个目录有日志、
    哪个从来没启动过），所以必须能分开查（2026-09-19 加）。
    """
    found: list[Path] = []
    for g in ([game] if game is not None else find_game_dirs()):
        for sub in ("game/csgo", "game/core"):
            d = g / sub
            if not d.is_dir():
                continue
            try:
                logs = [p for p in d.glob("*.log") if p.is_file()]
            except OSError:
                continue
            logs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            found.extend(logs[:limit])
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found if game is not None else found[:limit]


def check_cfg_loaded(cfg_name: str = OUR_CFG_NAME, limit: int = 3,
                     game: Path | None = None) -> list[dict]:
    """在最近的游戏日志里找 `Loading Game State Integration: <我们的 cfg>`。

    为什么需要：cfg **文件存在**只说明我们把它放在了对的地方；
    只有游戏日志里出现这一行，才说明游戏**真的加载了**它。
    （cfg 只在游戏启动/换图时读取 —— 装完不重启游戏是完全无效的。）

    ⚠️ 返回**空列表**和"找到日志但没命中"是两件完全不同的事（2026-09-19 修输出时才分清）：
      · 空列表 ⇒ 这个安装的 `game\\csgo` 下**一个 .log 都没有** ⇒ 从没启动过游戏；
      · 有元素但 loaded=False ⇒ 日志在，只是里面没有我们的 cfg ⇒ 装完没重启 / 装错目录。
    调用方必须分开说，否则用户会被"没有加载记录"这句话带到完全错的方向。
    """
    results: list[dict] = []
    for path in recent_game_logs(limit, game=game):
        hits: list[str] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "Loading Game State Integration" in line and cfg_name in line:
                        hits.append(line.strip())
        except OSError:
            continue
        results.append({
            "log": path,
            "name": path.name,
            "mtime": path.stat().st_mtime,
            "loaded": bool(hits),
            "count": len(hits),
            "last": hits[-1] if hits else None,
        })
    return results
