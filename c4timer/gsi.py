"""CS2 Game State Integration 接收与解析。

================================ 证据来源 ================================
本文件里每一条解析规则都来自 2026-09-15 在本机抓到的 210 个真实报文
（fixtures/gsi_capture_20260915.jsonl），不是文档推测，也不是合成数据。
1 代项目（E:\\csc4）就是死在"用合成数据验证解析器"上的。

============================= 五条实测铁律 =============================
1. 顶层 `bomb` / `phase_countdowns` / `allplayers` 是"观察者组件"，
   只在本地玩家死亡并观战时下发。判据极简：payload 顶层有没有 "bomb" 键。
   实测：27/27 与 76/76 个含 bomb 的包，player.steamid 都指向被观战者；
   而 60 个 player.steamid == 自己的包，一个都没有。
   → 绝不能用 player.state.health == 0 判断自己死了（死后 player 描述的是被观战者）。

2. `round.bomb` 只有 planted / defused / exploded 三个值（没有 defusing），
   玩家活着时也一直有，是终局判定的可靠来源。

3. `bomb.countdown` 的语义会随 state 切换：
       planting -> 距离安放完成
       planted  -> 距离爆炸
       defusing -> 距离拆弹成功      <<< 实测 9.8 -> 0.1，降 9.70s，实际经过 9.72s，差 19ms
   所以【绝对不能在 defusing 期间把它当爆炸倒计时】。

4. 有人拆弹时，`phase_countdowns.phase` 变成 "defuse"、`phase_ends_in` 与
   bomb.countdown 同步变为拆弹剩余；同时 `bomb.player` 以 added 形式出现（拆弹者 steamid）。
   这一刻 GSI 里【再也拿不到爆炸剩余】—— 必须靠本地 T0 时钟推算。
   附：countdown 的【初值】直接说明拆弹者有没有钳子（≈9.8 无钳 / ≈4.8 有钳），
   所以观战时只需显示一条，不需要猜。

5. 终局状态会残留：实测 round.phase=freezetime、round.bomb 缺失时，
   顶层 bomb.state 仍在报 "exploded"。→ round.phase != "live" 时一律不信
   顶层 bomb.state，以 round.bomb 为准。
   另：`phase_ends_in` 会变成负数（实测跑到 -1.9），不要假设 >= 0。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

# 顶层出现这个键 == 观察者数据可用（你死了在观战）
OBSERVER_KEY = "bomb"

# 顶层 bomb.state 的全部已知取值
BOMB_STATES = (
    "carried",
    "dropped",
    "planting",
    "planted",
    "defusing",
    "defused",
    "exploded",
)
# round.bomb 只有这三个（没有 defusing）
ROUND_BOMB_STATES = ("planted", "defused", "exploded")
TERMINAL_STATES = ("defused", "exploded")


# --------------------------------------------------------------------------
# 取值工具：GSI 的数值字段是字符串，而且会用 `true` 表示"这个键刚出现"
# --------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """GSI 的时长/坐标是带引号的字符串（"37.1"），要容错解析。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


def _str(value: Any) -> str | None:
    """注意：GSI 的增量块用 `true` 充当占位符，绝不能当成真实值。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    return text or None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


# 诊断用：把 `previously` / `added` 摊平成 "块:路径=值" 的短字符串。
#
# 为什么需要（2026-09-16 晚）：活着那一路的锚点现在只能靠"间隔 ≤ 3 秒 + chg=1"猜
# "哪一包是被安放事件触发的那一包"，猜错就是几秒的误差。Valve 的增量提示里带着
# **改了哪些键**，如果"安放触发包"在内容上可识别（比如 delta 里带 bomb/round.bomb），
# 就能把它认出来、把活着那一路做到 0.1 秒级；如果不可识别，也能彻底证明"锚点=巧合"。
# 只记录键与标量值，容器只记键名；总数超过 _DELTA_MAX 就截断（日志别被刷爆）。
_DELTA_MAX = 6


def _delta_paths(payload: dict) -> tuple[str, ...]:
    out: list[str] = []
    for block, mark in (("previously", "prev"), ("added", "add")):
        node = payload.get(block)
        if not isinstance(node, dict):
            continue
        for path, value in _flatten(node):
            out.append(f"{mark}:{path}" + (f"={value}" if value is not None else ""))
            if len(out) >= _DELTA_MAX:
                return tuple(out) + ("…",)
    return tuple(out)


def _flatten(node: dict, prefix: str = "") -> list[tuple[str, str | None]]:
    """{"round": {"bomb": true}} -> [("round.bomb", "true")]（容器只给键名）。"""
    out: list[tuple[str, str | None]] = []
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.append((path, None))
            out.extend(_flatten(value, f"{path}."))
        elif isinstance(value, bool):
            out.append((path, "true" if value else "false"))
        elif isinstance(value, (int, float, str)):
            text = str(value).strip()
            out.append((path, text[:24] if text else None))
        else:
            out.append((path, None))
    return out


# --------------------------------------------------------------------------
# 解析结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GsiSample:
    """一个 GSI 包解析后的规范化视图。所有字段都是"这一包说了什么"。"""

    recv_mono: float
    recv_wall: str

    # --- 观察者数据是否可用（铁律 1）---
    observer: bool
    # 当前 player 块描述的是谁 / 是不是自己
    observed_steamid: str | None
    self_steamid: str | None
    observing_other: bool

    # --- 回合 ---
    round_phase: str | None      # live / freezetime / over
    map_round: int | None
    map_name: str | None
    map_mode: str | None

    # --- 炸弹（已按铁律 5 做过残留压制）---
    bomb_state: str | None       # 规范化后的权威状态
    bomb_state_raw: str | None   # 顶层 bomb.state 原值（仅供调试/回归）
    round_bomb: str | None       # round.bomb 原值

    # --- 倒计时（已按铁律 3 / 4 分好归属）---
    explosion_remaining: float | None   # 距离爆炸；拆弹期间必为 None
    defuse_remaining: float | None      # 距离拆弹成功
    pcd_phase: str | None
    pcd_ends_in: float | None
    bomb_countdown: float | None
    bomb_player: str | None             # 拆弹者 steamid
    defuser_kit: bool | None            # 来自 allplayers（增量块里可能缺失）

    # --- 原始 ---
    top_keys: tuple[str, ...]
    allplayers_count: int
    # 自己所在阵营（CT / T）。**只有 player 块描述的是自己时才可靠** ——
    # 死了观战时 player 是别人，这时给 None，由调用方沿用上一次已知值。
    # 用途：实机反馈"倒计时过早归零往往出现在 CT 方"，要能按阵营分组统计 L_p。
    self_team: str | None = None
    # 这一包是否由"内容变化"触发（`previously`/`added` 非空）；心跳包为 False。
    # 用途：活着那一路的锚点研究（见 state._comp_for_this_plant）。
    carried_delta: bool = False
    # 变更键的扁平列表（诊断用）：("prev:round.phase=freezetime", "add:round.bomb=true", …)。
    # 心跳包为空元组。用途见 _delta_paths 的注释（docs/实机反馈与待办.md 第二十节）。
    changed: tuple[str, ...] = ()

    @property
    def bomb_planted(self) -> bool:
        return self.bomb_state in ("planting", "planted", "defusing")

    @property
    def is_terminal(self) -> bool:
        return self.bomb_state in TERMINAL_STATES

    @property
    def observed_defuse_duration(self) -> float | None:
        """拆弹刚发生时，countdown 初值 ≈ 拆弹总时长（9.8 无钳 / 4.8 有钳）。

        用途：观战时据此判断拆弹者有没有钳子，以及给本地倒计时定总长。
        """
        if self.bomb_state == "defusing" and self.defuse_remaining is not None:
            return self.defuse_remaining
        return None


def parse_payload(
    body: Any,
    recv_mono: float,
    recv_wall: str | None = None,
) -> GsiSample:
    """把一个 GSI payload 解析成 GsiSample。

    语义要点：**body 就是当前状态**（各已启用组件是完整的），`previously` /
    `added` 只是"哪些键变了"的提示。1 代把 `previously` 合并进当前状态，
    于是上一包的 planted 被反复当成当前值，状态永久卡死 —— 别重蹈覆辙。
    """
    payload = _dict(body)
    provider = _dict(payload.get("provider"))
    mp = _dict(payload.get("map"))
    rnd = _dict(payload.get("round"))
    player = _dict(payload.get("player"))
    allplayers = _dict(payload.get("allplayers"))

    observer = OBSERVER_KEY in payload
    top_bomb = _dict(payload.get(OBSERVER_KEY))
    pcd = _dict(payload.get("phase_countdowns"))

    # 这一包是不是"内容变化触发"的？`previously`/`added` 就是 Valve 给的变更提示：
    # 心跳包两者都空。**注意别拿它当状态用**（1 代把增量并进当前值，状态就永久卡死了），
    # 这里只当"这一包有没有内容变化"的标记，供活着那一路的锚点研究用
    # （见 state._comp_for_this_plant 与 docs/实机反馈与待办.md 第十六节）。
    carried_delta = bool(payload.get("previously")) or bool(payload.get("added"))
    # 变更**键**的扁平列表（诊断用，2026-09-16 晚加）：实验目的是验证"安放触发的那一包"
    # 能不能从内容上认出来（而不是只能靠"间隔 ≤ 3 秒"猜）。见 docs 第二十节。
    changed = _delta_paths(payload)

    round_phase = _str(rnd.get("phase"))
    round_bomb = _str(rnd.get("bomb"))
    if round_bomb not in ROUND_BOMB_STATES:
        round_bomb = None

    bomb_state_raw = _str(top_bomb.get("state"))
    if bomb_state_raw not in BOMB_STATES:
        bomb_state_raw = None

    # 铁律 5：非 live 期间，顶层 bomb.state 可能是上一回合的残留，不可信
    trusted_top_state = bomb_state_raw if round_phase == "live" else None

    if trusted_top_state is not None:
        bomb_state = trusted_top_state          # 更细（含 defusing/planting）
    else:
        bomb_state = round_bomb                 # 终局以 round.bomb 为准

    bomb_countdown = _num(top_bomb.get("countdown"))
    pcd_phase = _str(pcd.get("phase"))
    pcd_ends_in = _num(pcd.get("phase_ends_in"))
    bomb_player = _str(top_bomb.get("player"))

    # 铁律 3 / 4：倒计时归属于哪个 phase，必须严格区分
    defuse_remaining: float | None = None
    explosion_remaining: float | None = None

    if bomb_state == "defusing" or pcd_phase == "defuse":
        # 拆弹期间 countdown 是"拆弹剩余"。此处 **绝不** 给 explosion_remaining 赋值。
        if pcd_phase == "defuse" and pcd_ends_in is not None:
            defuse_remaining = pcd_ends_in
        elif bomb_countdown is not None:
            defuse_remaining = bomb_countdown
        else:
            defuse_remaining = pcd_ends_in
    elif bomb_state in ("planted", "planting") or pcd_phase == "bomb":
        if pcd_phase == "bomb" and pcd_ends_in is not None:
            explosion_remaining = pcd_ends_in
        elif bomb_countdown is not None:
            explosion_remaining = bomb_countdown
        else:
            explosion_remaining = pcd_ends_in

    defuser_kit: bool | None = None
    if bomb_player and bomb_player in allplayers:
        raw_kit = _dict(_dict(allplayers[bomb_player]).get("state")).get("defusekit")
        if isinstance(raw_kit, bool):
            defuser_kit = raw_kit

    self_sid = _str(provider.get("steamid"))
    observed_sid = _str(player.get("steamid"))
    self_team = (_str(player.get("team"))
                 if (self_sid and observed_sid == self_sid) else None)

    return GsiSample(
        recv_mono=recv_mono,
        recv_wall=recv_wall or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        observer=observer,
        observed_steamid=observed_sid,
        self_steamid=self_sid,
        observing_other=bool(observed_sid and self_sid and observed_sid != self_sid),
        round_phase=round_phase,
        map_round=_int(mp.get("round")),
        map_name=_str(mp.get("name")),
        map_mode=_str(mp.get("mode")),
        bomb_state=bomb_state,
        bomb_state_raw=bomb_state_raw,
        round_bomb=round_bomb,
        explosion_remaining=explosion_remaining,
        defuse_remaining=defuse_remaining,
        pcd_phase=pcd_phase,
        pcd_ends_in=pcd_ends_in,
        bomb_countdown=bomb_countdown,
        bomb_player=bomb_player,
        defuser_kit=defuser_kit,
        top_keys=tuple(sorted(payload.keys())),
        allplayers_count=len(allplayers),
        self_team=self_team,
        carried_delta=carried_delta,
        changed=changed,
    )


# --------------------------------------------------------------------------
# 接收端
# --------------------------------------------------------------------------


class GsiReceiver:
    """监听 127.0.0.1:port，每收到一个包就回调一次。

    注意：**必须立刻回 200**。Valve 官方明确——上一个请求没拿到 2XX，
    游戏就不发下一个，而且下次改为全量重发。这是延迟的隐形来源。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 30077,
        on_sample: Callable[[GsiSample], None] | None = None,
        on_raw: Callable[[dict, float], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.on_sample = on_sample
        self.on_raw = on_raw
        self.last_recv_mono: float | None = None
        self.total = 0
        self.dropped = 0
        self.parse_errors = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- 状态 -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._server is not None

    def age(self, now: float | None = None) -> float | None:
        if self.last_recv_mono is None:
            return None
        return (now if now is not None else time.monotonic()) - self.last_recv_mono

    def stale(self, after_sec: float, now: float | None = None) -> bool:
        age = self.age(now)
        return age is None or age > after_sec

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        if self._server is not None:
            return
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "c4timer-gsi/1.0"

            def log_message(self, *args):  # 静音默认访问日志
                pass

            def _ok(self) -> None:
                payload = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except OSError:
                    pass

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b""
                receiver._handle(raw)
                self._ok()

            def do_GET(self):
                receiver._handle(b"")
                self._ok()

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="gsi-recv", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    # -- 内部 -------------------------------------------------------------

    def _handle(self, raw: bytes) -> None:
        now = time.monotonic()
        self.total += 1
        try:
            body = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.dropped += 1
            return
        if not isinstance(body, dict) or not body:
            self.dropped += 1
            return
        self.last_recv_mono = now
        if self.on_raw is not None:
            try:
                self.on_raw(body, now)
            except Exception:  # 回调绝不能影响接收
                pass
        if self.on_sample is not None:
            try:
                sample = parse_payload(body, now)
            except Exception:
                self.parse_errors += 1
                return
            try:
                self.on_sample(sample)
            except Exception:
                self.parse_errors += 1

    def __enter__(self) -> "GsiReceiver":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
