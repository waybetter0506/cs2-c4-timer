"""P3 自检：端到端（真实报文 -> 解析 -> 状态机 -> 显示层）。

P1 的状态机测试用的是**人工构造**的 GsiSample。这里不同：把
`fixtures/gsi_capture_20260915.jsonl` 里 210 个**真实 GSI 包**按原始时间轴灌进去，
断言状态机在真实数据上产生的状态序列与显示内容。

为什么必须做：1 代的教训就是"用合成数据验证解析器"——合成数据永远长得比现实好看。

需求变更后这里的 [3] 块（"抓包 #3 观战真值驱动的 DEFUSING + 单条绿字"）整块删掉了：
`DEFUSING` 状态与绿字都已从产品里移除，那段真实数据现在只会照常走红字。
显示断言改成"任何状态都最多一行（只有红字）"。
"""

from __future__ import annotations

from c4timer.config import Config
from c4timer.fixtures import DEFAULT_FIXTURE, load_captures
from c4timer.gsi import parse_payload
from c4timer.render import build
from c4timer.state import ARMED, DEFUSED, EXPLODED, IDLE, C4StateMachine

from .checks import Checks

# 抓包之间留一点间隔，避免时间轴回退
CAPTURE_GAP = 5.0


class Trace:
    """把整份抓包按真实时间轴灌进状态机，记录每一次状态迁移与当时的显示。"""

    def __init__(self, path=None) -> None:
        self.path = path or DEFAULT_FIXTURE
        self.cfg = Config()
        self.cfg.debug.print_events = False
        self.sm = C4StateMachine(self.cfg)
        self.transitions: list[dict] = []
        self.snapshots: list[dict] = []
        self.by_capture: dict[int, list[dict]] = {}
        self.error: str | None = None

        offset = 0.0
        for cap in load_captures(self.path):
            rows = []
            for rec in cap.records:
                t = offset + float(rec.get("elapsed", 0.0))
                body = rec.get("body")
                if not isinstance(body, dict):
                    continue
                try:
                    sample = parse_payload(body, t, rec.get("wall", ""))
                    before = self.sm.state
                    before_snap = self.sm.snapshot(t)
                    before_exp = before_snap.explosion_remaining
                    self.sm.feed_gsi(sample)
                    self.sm.tick(t)
                    snap = self.sm.snapshot(t)
                    view = build(snap, show_idle=False, debug=False)
                except Exception as exc:      # 真实数据上任何异常都是缺陷
                    self.error = f"第 {cap.index} 次抓包 #{rec.get('n')} 处炸了: {exc!r}"
                    raise
                entry = {
                    "t": t, "n": rec.get("n"), "state": self.sm.state,
                    "before": before, "view": view,
                    "bomb_state": sample.bomb_state,
                    "observer": sample.observer,
                    "exp": snap.explosion_remaining,
                    "exp_before": before_exp,
                    # 迁移前红字是否已经归零（本地兜底判爆炸的唯一合法前提）
                    "deadline_before": before_snap.deadline_passed,
                    "rows": len(view.rows),
                    "anchor_unknown": snap.anchor_unknown,
                    "source": snap.source,
                }
                rows.append(entry)
                self.snapshots.append(entry)
                if self.sm.state != before:
                    self.transitions.append({**entry, "capture": cap.index})
            self.by_capture[cap.index] = rows
            offset += cap.duration + CAPTURE_GAP


def run(verbose: bool = True) -> tuple[int, int, list[str]]:
    c = Checks()
    if verbose:
        print("=" * 72)
        print("P3 自检：端到端（真实报文 -> 解析 -> 状态机 -> 显示）")
        print("=" * 72)

    tr = Trace()
    if tr.error:
        c.ok(False, tr.error)
        return c.passed, c.failed, c.failures

    n = len(tr.snapshots)
    if verbose:
        print(f"\n灌入 {n} 个真实包，产生 {len(tr.transitions)} 次状态迁移：")
        for t in tr.transitions:
            print(f"   抓包#{t['capture']} +{t['t']:7.2f}s  {t['before']:>9} -> "
                  f"{t['state']:<9} bomb_state={str(t['bomb_state']):<9} "
                  f"obs={str(t['observer']):<5} 行数={t['rows']}")

    # ---- 1. 全程不得抛异常 ------------------------------------------
    if verbose:
        print("\n[1] 健壮性")
    c.eq(n, 210, "210 个真实包全部走完链路且无异常")
    c.ok(len(tr.transitions) > 0, "确实产生了状态迁移（否则测试没意义）")

    # ---- 2. 抓包 1：活着安放 -> 拆除成功 -> 又安放 -> 爆炸 -------------
    if verbose:
        print("\n[2] 抓包 #1：活着时的主路径（GSI 给安放与终局）")
    cap1 = tr.by_capture.get(1, [])
    states1 = [e["state"] for e in tr.transitions if e["capture"] == 1]
    c.ok(ARMED in states1, "抓包 #1 出现过 ARMED")
    c.ok(DEFUSED in states1, "抓包 #1 出现过 DEFUSED（round.bomb=defused）")
    c.ok(EXPLODED in states1, "抓包 #1 出现过 EXPLODED（round.bomb=exploded）")
    # 抓包 1 全程没有观察者数据 -> 状态机绝不能凭空造出第二条倒计时
    no_observer = [e for e in cap1 if not e["observer"]]
    c.ok(all(e["rows"] <= 1 for e in no_observer),
         "没有观察者数据时最多只有 1 行（红字），不会冒出绿字")
    # 真值（观察者倒计时）是**套在**活着安放的回合里的：它只能对时，不能改状态
    c.ok(all(e["state"] in (IDLE, ARMED, DEFUSED, EXPLODED) for e in cap1),
         "抓包 #1 只会出现四个状态（DEFUSING 已删除）")

    # ---- 3. 抓包 3：观战真值那一段现在也只走红字 -----------------------
    if verbose:
        print("\n[3] 抓包 #3：观战真值不再造出第二条倒计时（绿字已删）")
    cap3 = tr.by_capture.get(3, [])
    c.ok(len(cap3) > 0, f"抓包 #3 有 {len(cap3)} 个包")
    c.ok(all(e["state"] in (IDLE, ARMED, DEFUSED, EXPLODED) for e in cap3),
         "抓包 #3 同样只会出现四个状态")
    c.ok(all(e["rows"] <= 1 for e in cap3), "抓包 #3 任何时刻最多 1 行")
    obs3 = [e for e in cap3 if e["observer"]]
    c.ok(len(obs3) > 0, f"抓包 #3 里有 {len(obs3)} 个观察者包（真值确实进来了）")
    # 真值进来时状态机要吃下它（对时），但显示仍然只有红字那一行
    c.ok(all(e["rows"] == 1 for e in obs3 if e["state"] == ARMED),
         "观察者 + ARMED 时恰好 1 行（红字），没有多出来的行")

    if verbose:
        print("\n[4] 抓包 #3 的终局与回合重置")
    states3 = [e["state"] for e in tr.transitions if e["capture"] == 3]
    c.ok(DEFUSED in states3, "抓包 #3 出现过 DEFUSED")
    c.ok(IDLE in states3, "抓包 #3 出现过 IDLE（回合切换/终局停留后重置）")

    # ---- 5. 终局的两条正确性约束 --------------------------------------
    if verbose:
        print("\n[5] 终局只能来自「GSI 报终局」或「本地兜底时刻已到」")
    exploded_tr = [t for t in tr.transitions if t["state"] == EXPLODED]
    c.ok(len(exploded_tr) > 0, f"确实进入过 EXPLODED（{len(exploded_tr)} 次）")
    # 更正（需求变更后）：本地计时器**不再**在红字归零时判爆炸 ——
    # 红字的零是"拆弹最后时限"，真爆比它晚 explode_after_deadline_sec（+宽限）。
    # 所以判据是"必须二选一：GSI 报 exploded，或迁移发生前红字已经归零"。
    # 注：真实报文的时间轴粒度是秒级，"归零后多久才 tick 到兜底时刻"没法从包上看出，
    # 所以这里只钉住**前提**（归零）而不是精确的兜底秒数 —— 精确秒数由 P1 用注入时钟覆盖。
    bad_expl = [
        t for t in exploded_tr
        if t["bomb_state"] != "exploded" and not t["deadline_before"]
    ]
    c.eq(len(bad_expl), 0,
         f"每次爆炸都由 GSI 终局、或「红字已归零」后的本地兜底触发"
         f"（越界 {[(t['t'], t['exp_before']) for t in bad_expl]}）")
    local_bang = [t for t in exploded_tr if t["bomb_state"] != "exploded"]
    c.ok(all(t["deadline_before"] for t in local_bang),
         f"本地兜底判爆炸（{len(local_bang)} 次）之前红字必须已经归零")
    # 反向：GSI 报 exploded 的那些迁移，绝大多数情况下本地早就归零了（红字先到零）
    c.ok(all(t["deadline_before"] for t in exploded_tr
             if t["bomb_state"] == "exploded" and t["exp_before"] is not None),
         "GSI 报 exploded 时，本地红字也早就归零了（两边口径一致）")

    if verbose:
        print("\n[6] 终局残留不得让状态来回震荡（真实报文抓到过的 bug）")
    # 回合结束、终局文案消失后，残留的 defused/exploded 包不得把状态顶回去
    bounce = [t for t in tr.transitions
              if t["before"] == IDLE and t["state"] in (DEFUSED, EXPLODED)]
    c.eq(len(bounce), 0,
         f"没有 IDLE -> 终局 的反弹（实际 {[(t['capture'], round(t['t'], 2), t['state']) for t in bounce]}）")

    # ---- 7. 迁移合法性 --------------------------------------------------
    if verbose:
        print("\n[7] 迁移合法性")
    legal = {
        IDLE: {ARMED, IDLE},
        ARMED: {DEFUSED, EXPLODED, IDLE, ARMED},
        DEFUSED: {IDLE, ARMED, DEFUSED},
        EXPLODED: {IDLE, ARMED, EXPLODED},
    }
    bad = [t for t in tr.transitions if t["state"] not in legal.get(t["before"], set())]
    c.eq(len(bad), 0,
         f"没有非法迁移（实际 {[(t['before'], t['state']) for t in bad][:4]}）")

    # ---- 8. 显示内容与状态一致 -----------------------------------------
    if verbose:
        print("\n[8] 显示与状态一致（只可能有一行红字）")
    armed_rows = [e["rows"] for e in tr.snapshots if e["state"] == ARMED]
    c.ok(all(r == 1 for r in armed_rows), "ARMED 时恒为 1 行（只有红字）")
    idle_rows = [e["rows"] for e in tr.snapshots if e["state"] == IDLE]
    c.ok(all(r == 0 for r in idle_rows), "IDLE 时 0 行（不常驻）")
    c.ok(all(e["rows"] <= 1 for e in tr.snapshots),
         "任何状态下都不超过 1 行（绿字已随需求删除）")
    # 终局那一行也要在（拆弹成功显示 --，爆炸显示 0.0），不能是空视图
    outcome_rows = [e["rows"] for e in tr.snapshots if e["state"] in (DEFUSED, EXPLODED)]
    c.ok(all(r == 1 for r in outcome_rows), "终局时恒为 1 行（终局文案）")

    # ---- 9. 真实数据上的锚点可信性 --------------------------------------
    if verbose:
        print("\n[9] 真实抓包上 T0 必须始终可信（不存在「回合中途启动」）")
    # 这次回放从第一次抓包的**第一包**开始，且每回合都能看到安放之前的报文，
    # 所以 anchor_unknown 一次都不该出现（出现就说明"看到安放之前"的判据太严了）。
    unknown = [e for e in tr.snapshots if e["anchor_unknown"]]
    c.eq(len(unknown), 0,
         f"全程 anchor_unknown 均为假（实际 "
         f"{[(e['capture'] if 'capture' in e else '-', e['t']) for e in unknown][:4]}）")
    # 反过来：必须真的建立过 T0（否则上面那条是空话）
    planted = [e for e in tr.snapshots if e["state"] == ARMED]
    c.ok(len(planted) > 0, f"真实数据上确实安放过（{len(planted)} 包处于 ARMED）")
    c.ok(all(e["source"] != "-" for e in planted),
         "ARMED 时来源标记不为空（gsi+comp / gsi-truth）")

    if verbose:
        print("\n" + "=" * 72)
        print(f"P3 端到端自检完成: {'全部通过' if c.failed == 0 else '有失败'}  "
              f"[OK {c.passed} / X {c.failed}]")
        print("=" * 72)
    return c.passed, c.failed, c.failures


def main() -> int:
    _, failed, _ = run()
    return 1 if failed else 0
