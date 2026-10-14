"""c4timer 命令行入口。

    python -m c4timer test                     # 自检（真实报文回归）
    python -m c4timer replay [--file F]        # 回放抓包，打印时间轴
    python -m c4timer listen [--port N]        # 实时监听 GSI（P0 先只打印）
    python -m c4timer config --list
    python -m c4timer config --set timers.bomb_seconds=40
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

# 本机控制台默认是 GBK，中文输出会炸；1 代在这里踩过坑。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from . import __version__, config as cfgmod  # noqa: E402
from .fixtures import DEFAULT_FIXTURE, load_captures  # noqa: E402
from .gsi import GsiReceiver, GsiSample  # noqa: E402


def _fmt(value, width: int = 8) -> str:
    if value is None:
        return " " * (width - 1) + "-"
    if isinstance(value, float):
        return f"{value:>{width}.2f}"
    return f"{str(value):>{width}}"


def _line(idx: int, s: GsiSample) -> str:
    return (
        f"{idx:>4} {s.recv_mono:>8.2f}s "
        f"{'OBS' if s.observer else '   '} "
        f"{str(s.round_phase or '-'):>10} "
        f"r{s.map_round if s.map_round is not None else '-':>3} "
        f"{str(s.bomb_state or '-'):>10} "
        f"exp={_fmt(s.explosion_remaining)} "
        f"def={_fmt(s.defuse_remaining)} "
        f"pcd={str(s.pcd_phase or '-'):>8} "
        f"sid={str(s.observed_steamid or '-')[:12]:>12}"
    )


def cmd_replay(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else DEFAULT_FIXTURE
    captures = load_captures(path)
    print("=" * 120)
    print(f"回放 {path}")
    print("=" * 120)
    total = 0
    for cap in captures:
        samples = cap.samples()
        total += len(samples)
        print(f"\n--- 抓包 #{cap.index}  {len(samples)} 包  {cap.wall_start} 起  "
              f"时长 {cap.duration:.1f}s ---")
        print(f"{'#':>4} {'time':>9} {'':>3} {'phase':>10} {'rnd':>4} "
              f"{'bomb':>10} {'exp':>8} {'def':>8} {'pcd':>8} {'observed':>12}")
        prev_state = prev_obs = None
        for i, s in enumerate(samples, 1):
            if not args.full:
                # 默认只打印"有变化"的行，避免 200 行噪声
                changed = (
                    s.bomb_state != prev_state
                    or s.observer != prev_obs
                    or s.defuse_remaining is not None
                    or s.explosion_remaining is None
                )
                prev_state, prev_obs = s.bomb_state, s.observer
                if not changed:
                    continue
            print(_line(i, s))

    # 汇总
    all_samples = [s for cap in captures for s in cap.samples()]
    n_obs = sum(1 for s in all_samples if s.observer)
    print("\n" + "=" * 120)
    print(f"合计 {len(captures)} 次抓包 / {total} 包")
    print(f"  观察者数据可用（你死了在观战）: {n_obs} 包")
    print(f"  玩家活着（无观察者数据）      : {len(all_samples) - n_obs} 包")
    for st in ("planted", "defusing", "defused", "exploded"):
        n = sum(1 for s in all_samples if s.bomb_state == st)
        if n:
            print(f"  bomb_state == {st:<9}: {n} 包")
    print(f"  回合结束(over)               : "
          f"{sum(1 for s in all_samples if s.round_phase == 'over')} 包")
    print("=" * 120)
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    # 自检模块在顶层 `tests/` 包（2026-09-18 从 c4timer/ 搬出去：它们不是产品代码）。
    # 入口命令没变，仍是 `python -m c4timer test`。
    from tests import test_app, test_e2e, test_gsi, test_render, test_state

    p0, f0, _ = test_gsi.run(args.file)
    print()
    p1, f1, _ = test_state.run()
    print()
    p2, f2, _ = test_render.run()
    print()
    p4, f4, _ = test_e2e.run()
    print()
    p5, f5, _ = test_app.run()
    counts = [("P0(GSI解析)", p0, f0), ("P1(状态机)", p1, f1), ("P1(显示层)", p2, f2),
              ("P3(端到端真实报文)", p4, f4),
              ("P3(应用层门控)", p5, f5)]
    total_pass = sum(p for _, p, _ in counts)
    total_fail = sum(f for _, _, f in counts)
    print("\n合计: " + "    ".join(f"{n} {p}/{p + f}" for n, p, f in counts) +
          f"    总计 {total_pass} 通过 / {total_fail} 失败")
    return 1 if total_fail else 0


def cmd_listen(args: argparse.Namespace) -> int:
    cfg = cfgmod.load(args.config)
    port = args.port or cfg.gsi.port
    print("=" * 96)
    print(f"c4timer {__version__} —— 实时监听 GSI  http://127.0.0.1:{port}/")
    print("（本命令只解析并打印，不显示悬浮窗；P0 验收用）")
    print("游戏侧无需操作：gamestate_state cfg 已指向该端口。Ctrl+C 退出。")
    print("=" * 96)
    print(f"{'#':>4} {'time':>9} {'':>3} {'phase':>10} {'rnd':>4} "
          f"{'bomb':>10} {'exp':>8} {'def':>8} {'pcd':>8} {'observed':>12}")

    import time
    count = 0

    def on_sample(s: GsiSample) -> None:
        nonlocal count
        count += 1
        print(_line(count, s), flush=True)

    recv = GsiReceiver(cfg.gsi.host, port, on_sample=on_sample)
    try:
        recv.start()
    except OSError as exc:
        print(f"[X] 无法监听 {cfg.gsi.host}:{port} —— {exc}")
        print("    端口被占用？关掉占用它的程序，或用 --port 换一个（需同步改 cfg）。")
        return 1
    try:
        # 只负责"保持进程活着"，直到 Ctrl+C。这里原来还有一个恒不生效的
        # `if stale(...): pass` 空分支（2026-09-18 清理），已删。
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        recv.stop()
    print(f"\n共收到 {count} 个合法包，丢弃 {recv.dropped} 个，解析错误 {recv.parse_errors} 个。")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .app import C4TimerApp

    cfg = cfgmod.load(args.config)
    cfg.debug.print_gsi = args.debug
    if args.debug:
        cfg.debug.print_events = True
    if args.edit:
        cfg.ui.show_idle = True      # 编辑模式必须有东西可抓，否则窗口是隐藏的
    app = C4TimerApp(cfg, enable_hud=not args.no_hud, debug=args.debug)
    if not args.no_hud:
        # tkinter 必须在主线程创建
        if args.edit:
            app.make_hud(click_through=False)
            print("编辑模式：拖动悬浮窗到想要的位置，松手即保存到 config.json。")
            print("（编辑模式下窗口不穿透，所以此时点不到游戏；定好位就正常启动。）")
            print("按 Ctrl+C 退出编辑模式。")
        else:
            app.make_hud()
    return app.run(args.seconds)


def _port_listening(host: str, port: int, timeout: float = 0.3) -> bool:
    """只做一次 connect，不 bind —— 不会干扰正在监听的进程。"""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _save_cs2_dir(path: Path, value: str | None) -> None:
    """只改 `cs2.game_dir` 这一个键，其余原样保留。

    为什么不复用 `config --set`：那条路会把**全部字段**写盘（等于把当时的默认值钉死），
    以后我们调了默认值这个用户就吃不到了。这里只动这一个键。
    """
    import json

    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            pass                                  # 读不出来就当成空配置重建这一项
    section = data.get("cs2")
    if not isinstance(section, dict):
        section = {}
    if value is None:
        section.pop("game_dir", None)
        if section:
            data["cs2"] = section
        else:
            data.pop("cs2", None)
    else:
        section["game_dir"] = value
        data["cs2"] = section
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_set_cs2(args: argparse.Namespace) -> int:
    """指定 CS2 的游戏目录 —— 自动查找找不着时的兜底（2026-09-19 加）。

    为什么要给用户这个手填的口子：自动查找已经覆盖了注册表 + 扫盘 + libraryfolders.vdf，
    但**装法永远列不完**（蒸汽平台、完美世界、移动硬盘、网络盘、改过名的目录……）。
    让他自己指一次，比我们一条条补路径彻底得多。
    """
    from .cs2path import find_cfg_dirs, manual_game_dir, resolve_manual

    path = Path(args.config) if args.config else cfgmod.DEFAULT_PATH
    print("=" * 78)
    print("指定 CS2 游戏目录")
    print("=" * 78)

    if args.clear:
        if not manual_game_dir():
            print("  你本来就没有手动指定过，不用清。")
            return 0
        _save_cs2_dir(path, None)
        print(f"[OK] 已清掉手填的路径，回到全自动查找")
        print(f"     写进 {path}")
        return 0

    raw = args.dir
    if not raw:
        print("  把 CS2 的文件夹拖进这个窗口（或直接粘贴路径），然后回车。")
        print("  下面这几种**粘哪个都行**：")
        print(r"      ...\Counter-Strike Global Offensive        （游戏根目录，最好找）")
        print(r"      ...\Counter-Strike Global Offensive\game\csgo\cfg")
        print(r"      ...\Steam\steamapps\common")
        print("  不想填就直接回车退出。")
        print()
        try:
            raw = input("  路径> ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = ""
        if not raw:
            print("  没填，什么都没改。")
            return 1

    games = resolve_manual(raw)
    if not games:
        print(f"[X] 从这个路径里没认出 CS2：{raw}")
        print(r"    请确认它下面有 game\csgo 这一层（也就是游戏的根目录），")
        print(r"    或者你直接粘的是 game\csgo\cfg 那一层。")
        return 1

    # 存绝对路径：用户拖进来的是绝对路径，但手打可能是相对的；
    # 而读取发生在**另一个进程、另一个工作目录**里，相对路径到那时就指别处了。
    _save_cs2_dir(path, str(Path(games[0]).resolve()))
    print(f"[OK] 已记住：{games[0]}")
    print(f"     写进 {path}（只改了 cs2.game_dir 这一项）")
    print()
    print("  立刻验证一遍 ——")
    dirs = find_cfg_dirs()
    if not dirs:
        print("  [X] 还是没找到 cfg 目录。这个路径可能不对。")
        print(r"      用资源管理器看一眼：游戏根目录下面应该有 game\csgo\cfg。")
        return 1
    for d in dirs:
        mark = "  ← 游戏实际读这个" if d.parent.name == "csgo" else ""
        print(f"      {d}{mark}")
    print()
    print("  现在可以双击「装GSI配置.bat」了；双击「体检.bat」也能看到结果。")
    return 0


def cmd_install_gsi(args: argparse.Namespace) -> int:
    """把仓库里的 GSI 配置装到 CS2 的 cfg 目录（本项目唯一会写游戏目录的操作）。"""
    from .config import ROOT
    from .cs2path import OUR_CFG_NAME, install_our_cfg

    template = ROOT / "gsi" / OUR_CFG_NAME
    print("=" * 78)
    print("安装 GSI 配置（让 CS2 把炸弹状态推送给本程序）")
    print("=" * 78)
    print(f"  模板：{template}")
    if not template.exists():
        print(f"  [X] 模板不存在 —— 仓库不完整？应该在 gsi/{OUR_CFG_NAME}")
        return 1
    dst, msg = install_our_cfg(template)
    if dst is None:
        print(f"  [X] {msg}")
        print("      手动做法：把 gsi\\" + OUR_CFG_NAME + " 复制到 CS2 的")
        print("      ...\\Counter-Strike Global Offensive\\game\\csgo\\cfg\\ 目录里。")
        print("      或者双击「指定游戏目录.bat」告诉程序游戏装在哪，再来一次。")
        return 1
    print(f"  [OK] {msg}")
    print(f"      位置：{dst}")
    print()
    print("  下一步：**重启 CS2**（cfg 只在游戏启动/换图时读取），然后双击 体检.bat 确认")
    print("          「本程序的 cfg 已装好，组件齐全」。")
    return 0


def _cfg_verdict(ours_in: list[Path], live_games: list[Path], loaded_games: list[Path],
                 index_of: dict, has_games: bool = True) -> list[str]:
    """体检结论：把"cfg 装没装 / 游戏加载没加载 / 是不是装错了安装"分开说。

    抽成**纯函数**是为了能被自检直接覆盖 —— 这段判据正是 2026-09-19 把所有人
    （包括我）带偏的地方：旧版本只有一句"没有加载记录"，于是
    **"cfg 装进了另一个安装"被说成了"用户忘了重启游戏"**，一个真实工单查了一整轮。

    四种处境：
      1. 没找到任何安装        -> 让用户手动指定
      2. 哪里都没有我们的 cfg  -> 让用户装一次
      3. cfg 所在的安装加载过  -> 一切正常
      4. cfg 所在的安装有日志但没加载 -> 装完没重启
      5. cfg 在没日志的安装里、而别的安装有日志 -> **装错了安装**（最容易被误判的一种）
      6. 所有安装都没有日志    -> 找错了安装（用户实际在玩的不在这几个里）
    """
    def ids(items: list[Path]) -> str:
        return "、".join(f"[{index_of.get(g, '?')}]" for g in items)

    if not has_games:
        return ["[X] 没找到 CS2 安装 —— 先双击「指定游戏目录.bat」"]
    if not ours_in:
        return ["[!] 没找到本程序的 cfg（gamestate_integration_csc4.cfg）。",
                "                      双击 装GSI配置.bat 可自动装好，装完重启 CS2。"]
    if any(g in loaded_games for g in ours_in):
        return ["[OK] 本程序的 cfg 已装好，而且游戏确实加载过 —— 组件齐全，可以启动。"]
    if any(g in live_games for g in ours_in):
        return ["[!] cfg 装好了，日志也有，但**里面没有加载它的记录** ——",
                "                      多半是装完没有重启 CS2（cfg 只在启动/换图时读取）。"]
    if live_games:
        return [f"[X] 本程序的 cfg 装到了 {ids(ours_in)} 里，"
                f"但你实际在玩的是 {ids(live_games)} ——",
                f"                      游戏读的是 {ids(live_games)} 那份 cfg，所以收不到推送。",
                "                      双击「指定游戏目录.bat」，"
                "把真正在玩的那个游戏目录拖进去，",
                "                      再重跑一次 装GSI配置.bat。"]
    return ["[X] 本程序的 cfg 是装好了，但**这些安装里一个游戏日志都没有** ——",
            "                      说明你实际在玩的**不是这几个安装**，游戏读不到这份 cfg。",
            "                      双击「指定游戏目录.bat」，"
            "把真正在玩的那个游戏目录拖进去。"]


def cmd_doctor(args: argparse.Namespace) -> int:
    import platform
    import sys as _sys
    import time

    cfg = cfgmod.load(args.config)
    print("=" * 78)
    print("c4timer 环境体检")
    print("=" * 78)
    print(f"  Python        : {_sys.version.split()[0]}  ({platform.machine()})")
    try:
        import numpy
        print(f"  numpy         : {numpy.__version__}")
    except ImportError as exc:
        print(f"  numpy         : [X] 未安装 ({exc})")

    # ---- CS2 安装与 cfg：**按安装分组**显示（2026-09-19 重写）----
    #
    # 为什么必须分组：一个真实工单里，机器上有**两个** Counter-Strike Global Offensive
    # 目录（一个是从没启动过的僵尸安装、一个是真在玩的）。旧输出把 5 份 cfg 平铺成一列、
    # 「找到 3 个」和下面几张 cfg 对不上号，`[OK] cfg 已装好` 也不说是哪个目录 ——
    # 结果所有人（包括我）都被"没有加载记录"这句话带到"用户忘了重启游戏"，
    # 而真相是"cfg 装进了错的那个安装"。
    # 现在：每个安装一段，写明它有没有游戏日志、cfg 装没装、游戏加载过没有。
    from .cs2path import (CFG_GLOB, CFG_SUBPATHS, OUR_CFG_NAME, check_cfg_loaded,
                          find_game_dirs, inspect, manual_game_dir)

    manual = manual_game_dir()
    print("\n  CS2 安装位置  : " + (f"手动指定 —— {manual}" if manual
                                    else "自动查找（注册表 → 扫各盘符 → libraryfolders.vdf）"))
    games = find_game_dirs()
    print(f"  CS2 安装      : 找到 {len(games)} 个")
    if not games:
        print("      [X] 一个都没找到 —— 自动查找没能认出你的装法。")
        print("          双击「指定游戏目录.bat」把游戏目录告诉程序即可（一次性）。")

    # 按安装记账。判据必须落在**每个安装**上，不能只数总数：
    # "cfg 装到了没在用的那个安装里" 和 "装完没重启" 是两种完全不同的病，
    # 只看总数会把前者误判成后者（2026-09-19 想清楚的）。
    live_games: list[Path] = []       # 有游戏日志的安装 = 真正被启动过的
    loaded_games: list[Path] = []     # 日志里确实加载过我们 cfg 的安装
    ours_in: list[Path] = []          # 我们的 cfg 装在哪些安装里
    index_of = {g: i for i, g in enumerate(games, 1)}

    for idx, game in enumerate(games, 1):
        logs = check_cfg_loaded(OUR_CFG_NAME, limit=1, game=game)
        print(f"\n      [{idx}] {game}")
        if not logs:
            # 注意：这是"连日志都没有"，与"有日志但没命中"是两回事
            print("          游戏日志：一个都没有 —— 这个安装从没启动过游戏")
        else:
            live_games.append(game)
            info = logs[0]
            stamp = time.strftime("%m-%d %H:%M", time.localtime(info["mtime"]))
            if info["loaded"]:
                loaded_games.append(game)
                print(f"          游戏日志：有（最新 {stamp}）"
                      f"—— 加载过本程序 cfg {info['count']} 次  ← 你在玩的是这个")
            else:
                print(f"          游戏日志：有（最新 {stamp}）"
                      f"—— 但里面没有加载本程序 cfg 的记录")
        for sub in CFG_SUBPATHS:
            cfg_dir = game / sub
            if not cfg_dir.is_dir():
                continue
            print(f"          cfg 目录：{cfg_dir}")
            entries = sorted(cfg_dir.glob(CFG_GLOB))
            if not entries:
                print("              （空：这个目录里没有任何 gamestate_integration_*.cfg）")
            for path in entries:
                cinfo = inspect(path)
                if cinfo.is_ours and game not in ours_in:
                    ours_in.append(game)
                mark = "  ← 本程序" if cinfo.is_ours else ""
                print(f"              {path.name:<40} uri={cinfo.uri}{mark}")
                if cinfo.is_ours:
                    for name, ok in (("bomb", cinfo.has_bomb),
                                     ("phase_countdowns", cinfo.has_phase_countdowns),
                                     ("allplayers_state", cinfo.has_allplayers_state)):
                        print(f"                  {'OK ' if ok else 'X  '} {name}")
                    for problem in cinfo.blockers:
                        print(f"                  [X] {problem}")
                    if cinfo.port is not None and cinfo.port != cfg.gsi.port:
                        print(f"                  [X] cfg 端口 {cinfo.port}"
                              f" 与配置 {cfg.gsi.port} 不一致")

    # ---- 结论：四种处境分开说，别让用户猜 ----
    verdict = _cfg_verdict(ours_in, live_games, loaded_games, index_of,
                           has_games=bool(games))
    print("\n  结论          : " + verdict[0])
    for line in verdict[1:]:
        print(line)

    from . import tuning
    from .tuning import STATS_PATH
    print(f"\n  自成长        : {tuning.summary()}")
    print(f"      统计文件    : {STATS_PATH}（删掉它就归零，回到你手调的偏置）")

    # 文件存在 != 游戏加载了 —— 这件事已经在上面"按安装分组"那一段里说清了
    # （每个安装都会写明：有没有日志、日志里有没有加载记录）。
    # 2026-09-19：原来这里还有一段独立的「游戏是否加载了」，它只说"没有加载记录"，
    # 分不清"装了没重启"和"根本找错了安装"，正是把所有人带偏的那句话，已合并到上面。

    busy = _port_listening(cfg.gsi.host, cfg.gsi.port)
    print(f"\n  GSI 端口      : {cfg.gsi.host}:{cfg.gsi.port}  "
          f"{'已被监听（采集工具或另一个实例在跑）' if busy else '空闲，可以启动'}")

    has_cfg = cfgmod.DEFAULT_PATH.exists()
    src = f"来自 {cfgmod.DEFAULT_PATH.name}（会覆盖代码默认值！）" if has_cfg \
        else "来自代码默认值（没有 config.json）"
    print(f"\n  生效参数来源  : {src}")
    if cfgmod.LAST_LOAD_ERROR:
        print("  [X] 配置文件解析失败 -> 整份被忽略，现在跑的是代码默认值：")
        print(f"      {cfgmod.LAST_LOAD_ERROR}")
        print("      （最常见的原因是被编辑器写了 BOM；现在已能读 BOM，")
        print("        若仍报错就是语法问题，删掉该文件即可回到默认值。）")
    print(f"      gsi.stale_after_sec  {cfg.gsi.stale_after_sec:.0f}s"
          f"   （必须 > 心跳 10s，否则正常游玩会误报断连）")
    print(f"      timers 补偿 / 真爆滞后  {cfg.timers.plant_latency_compensation:.2f}s / "
          f"{cfg.timers.explode_after_deadline_sec:.2f}s")
    print("          （补偿 = 安放通知延迟；越大归零越早。只在**锚点用不了**时生效"
          "（心跳做上一包 / 没有上一包））；")
    print("            逐局真实延迟 0.3~2.4 秒都在晃，2026-09-16 由 1.85→1.5→**1.0**"
          "，见 docs/实机反馈与待办.md 第十四、十八节）")
    print(f"      ui 小数门槛           {cfg.ui.decimal_below_sec:.0f}s"
          f"   （剩余多于此显示整数、向上取整=游戏「还剩第几秒」；最后这么多秒给一位小数）")
    if not (1.0 <= cfg.timers.plant_latency_compensation <= 2.5):
        print(f"      [!] 补偿 {cfg.timers.plant_latency_compensation:.2f} 在实测区 1.0~2.5 之外。")
        print("          注意方向：补偿**越大**，红字归零**越早**。")
    print("      游戏侧 cfg 节流：")
    from .cs2path import OUR_CFG_NAME, find_cfg_dirs
    found_any = False
    for cfg_dir in find_cfg_dirs():
        path = cfg_dir / OUR_CFG_NAME
        if not path.exists():
            continue
        found_any = True
        text = path.read_text(encoding="utf-8", errors="replace")
        import re as _re
        vals = []
        for key in ("buffer", "throttle", "heartbeat"):
            m = _re.search(rf'"{key}"\s+"([^"]+)"', text)
            vals.append(f"{key}={m.group(1) if m else '?'}")
        print(f"        {path.parent}  " + " ".join(vals))
    if not found_any:
        print("        （没找到本程序的 cfg —— 先跑一次 体检 看「装 cfg」那一步）")
    print("        （实测把 buffer/throttle 从 0.10 降到 0.02 后，安放延迟出现过 "
          "0.44 秒的样本；n=1，还在收集。若上面出现两份不同值的 cfg，")
    print("          说明有一份是旧副本 —— 游戏只会加载其中一个，建议只留游戏实际用的那份。）")

    print("\n" + "=" * 78)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """把真实抓包按加速时间轴灌进悬浮窗，用来在没有游戏的情况下看真实行为与观感。"""
    import time as _time

    from . import render
    from .app import C4TimerApp
    from .fixtures import DEFAULT_FIXTURE, load_captures
    from .gsi import parse_payload

    cfg = cfgmod.load(args.config)
    cfg.debug.print_events = False
    if args.show_idle:
        cfg.ui.show_idle = True

    app = C4TimerApp(cfg, enable_hud=not args.no_hud, debug=False)
    hud = None if args.no_hud else app.make_hud()

    path = Path(args.file) if args.file else DEFAULT_FIXTURE
    events: list[tuple[float, object]] = []
    offset = 0.0
    for cap in load_captures(path):
        for rec in cap.records:
            body = rec.get("body")
            if not isinstance(body, dict):
                continue
            t = offset + float(rec.get("elapsed", 0.0))
            events.append((t, parse_payload(body, t, rec.get("wall", ""))))
        offset += cap.duration + 5.0
    if not events:
        print(f"[X] {path} 里没有可用报文")
        return 1
    total = events[-1][0]
    speed = max(0.1, args.speed)

    print("=" * 76)
    print(f"演示：回放 {len(events)} 个真实报文，{total:.0f} 秒时间轴，加速 {speed:g}x"
          f"（约 {total / speed:.0f} 秒看完）")
    print("  悬浮窗会按真实状态变化显示；控制台同步打印状态迁移。")
    if hud is None:
        print("  （--no-hud：只打印，不开窗）")
    print("=" * 76)

    idx = 0
    prev_state = None
    t0 = _time.monotonic()
    try:
        while True:
            sim = (_time.monotonic() - t0) * speed
            while idx < len(events) and events[idx][0] <= sim:
                app.state.feed_gsi(events[idx][1])   # type: ignore[arg-type]
                idx += 1
            app.state.tick(sim)
            snapshot = app.state.snapshot(sim)
            view = render.build(snapshot, cfg.ui.show_idle, args.debug,
                                cfg.ui.decimal_below_sec)
            if hud is not None:
                hud.set_view(view)
                hud.root.update()
            if snapshot.state != prev_state:
                prev_state = snapshot.state
                bits = [f"{sim:7.2f}s  {snapshot.state:<9} {snapshot.status_text}"]
                if snapshot.explosion_remaining is not None:
                    bits.append(f"时限 {snapshot.explosion_remaining:5.1f}")
                if snapshot.anchor_unknown:
                    bits.append("(T0 未知)")
                if not view.visible:
                    bits.append("(窗口隐藏)")
                print("  " + "  ".join(bits), flush=True)
            if sim > total + args.tail:
                break
            _time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        if hud is not None:
            try:
                hud.stop()
            except Exception:
                pass
    print("演示结束。")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = cfgmod.load(args.config)
    if args.set:
        if "=" not in args.set:
            print(f"[X] 需要 KEY=VALUE 形式，例如 --set ui.font_size=44（收到 {args.set!r}）")
            return 1
        key, value = args.set.split("=", 1)
        try:
            cfgmod.set_value(cfg, key.strip(), value)
        except ValueError as exc:
            print(f"[X] {exc}")
            return 1
        path = cfgmod.save(cfg, args.config)
        print(f"[OK] {key.strip()} = {value}  ->  {path}")
        return 0
    import json
    print(json.dumps(asdict(cfg), ensure_ascii=False, indent=2))
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """有安放片段、又还没有参考指纹时自动建参考（`启动.bat` 每次启动前跑一次）。"""
    from . import autocal

    return int(autocal.run(dry_run=args.dry_run))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="c4timer", description="CS2 C4 爆炸/拆弹倒计时")
    p.add_argument("--version", action="version", version=f"c4timer {__version__}")
    p.add_argument("--config", default=None, help="配置文件路径（默认 ./config.json）")
    sub = p.add_subparsers(dest="command")

    t = sub.add_parser("test", help="自检：真实报文回归")
    t.add_argument("--file", default=None, help="抓包夹具路径")
    t.set_defaults(func=cmd_test)

    r = sub.add_parser("replay", help="回放抓包并打印时间轴")
    r.add_argument("--file", default=None, help="抓包文件（默认 fixtures/…jsonl）")
    r.add_argument("--full", action="store_true", help="打印每一个包（默认只打印变化点）")
    r.set_defaults(func=cmd_replay)

    l = sub.add_parser("listen", help="实时监听 GSI 并打印")
    l.add_argument("--port", type=int, default=None)
    l.set_defaults(func=cmd_listen)

    r = sub.add_parser("run", help="正式运行：GSI + 悬浮窗")
    r.add_argument("--no-hud", action="store_true", help="不开悬浮窗（只在控制台看）")
    r.add_argument("--edit", action="store_true",
                   help="编辑模式：可拖动悬浮窗定位，位置保存到 config.json")
    r.add_argument("--debug", action="store_true", help="打印事件与周期状态")
    r.add_argument("--seconds", type=float, default=None,
                   help="只跑这么多秒后自动退出（自检用）")
    r.set_defaults(func=cmd_run)

    doc = sub.add_parser("doctor", help="环境体检：CS2 cfg / 端口 / 生效参数")
    doc.set_defaults(func=cmd_doctor)

    ig = sub.add_parser("install-gsi", help="把 GSI 配置装到 CS2 的 cfg 目录（唯一会写游戏目录的操作）")
    ig.set_defaults(func=cmd_install_gsi)

    d = sub.add_parser("demo", help="不用游戏：把真实抓包回放进悬浮窗看行为与观感")
    d.add_argument("--file", default=None, help="抓包文件（默认 fixtures/…jsonl）")
    d.add_argument("--speed", type=float, default=8.0, help="加速倍率（默认 8x）")
    d.add_argument("--tail", type=float, default=3.0, help="结束后再多跑几秒")
    d.add_argument("--no-hud", action="store_true", help="不开窗，只在控制台打印")
    d.add_argument("--show-idle", action="store_true", help="待机时也显示窗口")
    d.add_argument("--debug", action="store_true", help="带上调试角标")
    d.set_defaults(func=cmd_demo)

    c = sub.add_parser("config", help="查看/修改配置")
    # `--list` 是"打印全部配置"的老开关，等价于不带 --set（见 cmd_config 的兜底分支）。
    # 保留它只为兼容旧命令，行为与从前完全一致。
    c.add_argument("--list", action="store_true",
                   help="打印全部配置（等价于不带 --set，保留此开关仅为兼容）")
    c.add_argument("--set", metavar="KEY=VALUE",
                   help="例: --set ui.font_size=44 ；不带 --set 则打印全部配置")
    c.set_defaults(func=cmd_config)

    cal = sub.add_parser("calibrate", help="有安放片段又没参考指纹时，自动把参考建出来")
    cal.add_argument("--dry-run", action="store_true", help="只说明会做什么，不写文件")
    cal.set_defaults(func=cmd_calibrate)

    sc = sub.add_parser("set-cs2", help="指定 CS2 游戏目录（自动查找找不到时的兜底）")
    sc.add_argument("--dir", default=None, help="游戏目录；不给就交互式输入")
    sc.add_argument("--clear", action="store_true", help="清掉手填的路径，回到全自动")
    sc.set_defaults(func=cmd_set_cs2)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "config", None):
        # `--config` 原来是"谁用谁传"，但 `cs2path` 读手填的游戏目录时是**自己去读默认路径**的，
        # 于是出现"刚存进去的值自己都读不到"（2026-09-19 实测踩到）。
        # 把默认路径改掉，让"没显式传路径"的读取方也认同一个文件。
        cfgmod.DEFAULT_PATH = Path(args.config)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
