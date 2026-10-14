"""P0 自检：用 210 个真实 GSI 报文回归 GSI 解析。

这里断言的全部是"语义不变量"，不是"抄一遍我的实现输出"：
每一条都能从 2026-09-15 的实测证据独立推出。如果哪条挂了，
说明解析代码违反了实测事实，而不是"测试过时了"。
"""

from __future__ import annotations

from pathlib import Path

from c4timer.fixtures import DEFAULT_FIXTURE, Capture, load_captures

from .checks import Checks

# 实测：第 3 次抓包抓到一段完整的无钳拆弹
EXPECTED_DEFUSE_FIRST = 9.8
EXPECTED_DEFUSE_LAST = 0.1


def run(path: Path | str | None = None, verbose: bool = True) -> tuple[int, int, list[str]]:
    c = Checks()
    fixture = Path(path) if path else DEFAULT_FIXTURE

    if verbose:
        print("=" * 72)
        print("P0 自检：GSI 解析 · 真实报文回归")
        print(f"夹具: {fixture}")
        print("=" * 72)

    if not fixture.exists():
        c.ok(False, f"夹具存在（{fixture}）")
        return c.passed, c.failed, c.failures

    captures = load_captures(fixture)
    total_packets = sum(len(cap.records) for cap in captures)
    samples = [s for cap in captures for s in cap.samples()]

    if verbose:
        print(f"\n[抓包切分] 共 {total_packets} 包，{len(captures)} 次抓包：")
        for cap in captures:
            print(f"   #{cap.index}  {len(cap.records):>4} 包  "
                  f"{cap.wall_start} 起  时长 {cap.duration:.1f}s")

    # ---- 1. 全量可解析 -------------------------------------------------
    if verbose:
        print("\n[1] 全量解析")
    c.eq(len(samples), total_packets, "每个包都解析出了 GsiSample（0 异常）")
    c.ok(all(s.top_keys for s in samples), "每个样本都有顶层键记录")

    # ---- 2. 铁律 1：观察者组件 = 顶层有 bomb 键 --------------------------
    if verbose:
        print("\n[2] 铁律 1：观察者组件可用性判据")
    c.ok(
        all(s.observer == ("bomb" in s.top_keys) for s in samples),
        "observer 标志 == payload 顶层含 'bomb'",
    )
    observer_samples = [s for s in samples if s.observer]
    c.ok(len(observer_samples) > 0, "夹具里确实存在观察者数据（否则回归没意义）")
    c.ok(
        all(s.allplayers_count > 0 for s in observer_samples),
        "所有观察者包都带 allplayers（同属观察者组件）",
    )
    c.ok(
        all(s.observing_other for s in observer_samples),
        "所有观察者包里 player 块描述的都是别人（不是自己）—— 所以不能用 health==0 判断自己死了",
    )
    non_observer = [s for s in samples if not s.observer]
    c.ok(
        all(s.bomb_state_raw is None for s in non_observer),
        "没有观察者数据时，顶层 bomb.state 一律缺席",
    )
    # 阵营（2026-09-16 加）：实机反馈"过早归零往往出现在 CT 方"，要能按阵营分组统计 ——
    # 所以活着时必须解析出 player.team；观战时 player 块是别人，不许当成自己的阵营。
    teams = {s.self_team for s in non_observer}
    c.ok(
        {"CT", "T"} <= teams,
        f"活着时能解析出自己的阵营（CT/T 都出现过：{sorted(t for t in teams if t)}）",
    )
    c.ok(
        all(s.self_team is None for s in observer_samples),
        "观战时 player 块描述的是别人 -> 不声称那是自己的阵营（调用方沿用上一次）",
    )
    # carried_delta（2026-09-16 加）：这一包是不是"内容变化触发"的。
    # 用途是活着那一路的锚点研究（安放前最后一包的时刻比报安放那包更接近真实安放）。
    n_delta = sum(1 for s in samples if s.carried_delta)
    c.ok(
        0 < n_delta < len(samples),
        f"夹具里两类包都有：内容变化触发的 {n_delta} 个 / 心跳 {len(samples) - n_delta} 个",
    )
    # changed（2026-09-16 晚加，诊断用）：把 `previously`/`added` 摊平成 "prev:/add: 路径=值"。
    # 用途是验证"安放触发的那一包"能否从**内容**上认出来（现在只能靠间隔猜），见 docs 第二十节。
    c.ok(
        all(s.changed for s in samples if s.carried_delta),
        "每个 carried_delta=True 的包都记下了变更键（不为空）",
    )
    c.ok(
        all(not s.changed for s in samples if not s.carried_delta),
        "心跳包没有变更键（changed 为空）",
    )
    c.ok(
        all(k.startswith(("prev:", "add:")) for s in samples for k in s.changed if k != "…"),
        "变更键都带 prev:/add: 前缀（能分清是旧值还是新值；末尾的 '…' 是截断标记）",
    )
    n_round_delta = [s for s in samples
                     if any(k.startswith(("prev:round", "add:round")) for k in s.changed)]
    c.ok(len(n_round_delta) > 0, f"夹具里确实有改到 round.* 的包（{len(n_round_delta)} 个）")

    # ---- 3. 铁律 5：终局残留压制 ---------------------------------------
    if verbose:
        print("\n[3] 铁律 5：非 live 期间不信顶层 bomb.state（终局残留）")
    n_state_exploded = sum(1 for s in samples if s.bomb_state == "exploded")
    n_round_exploded = sum(1 for s in samples if s.round_bomb == "exploded")
    c.eq(n_state_exploded, n_round_exploded, "'exploded' 只可能来自 round.bomb")
    n_state_defused = sum(1 for s in samples if s.bomb_state == "defused")
    n_round_defused = sum(1 for s in samples if s.round_bomb == "defused")
    c.eq(n_state_defused, n_round_defused, "'defused' 只可能来自 round.bomb")
    residue = [
        s for s in samples
        if s.round_phase != "live"
        and s.bomb_state_raw in ("defused", "exploded")
        and s.round_bomb is None
    ]
    c.ok(
        len(residue) > 0,
        f"夹具里确实存在带残留状态的包（实测 {len(residue)} 个），且被压制："
        "它们规范化后 bomb_state 必须为 None",
    )
    c.ok(
        all(s.bomb_state is None for s in residue),
        "残留包规范化后 bomb_state 全部为 None（否则会误报爆炸/拆除）",
    )

    # ---- 4. 铁律 3 / 4：倒计时归属互斥 ---------------------------------
    if verbose:
        print("\n[4] 铁律 3/4：爆炸剩余与拆弹剩余互斥、且归属正确")
    both = [s for s in samples
            if s.explosion_remaining is not None and s.defuse_remaining is not None]
    c.eq(len(both), 0, "没有任何一包同时给出爆炸剩余和拆弹剩余")
    bad_expl = [
        s for s in samples
        if s.explosion_remaining is not None
        and not (s.bomb_state in ("planted", "planting") or s.pcd_phase == "bomb")
    ]
    c.eq(len(bad_expl), 0, "explosion_remaining 只在 planted/planting 或 pcd.phase==bomb 时出现")
    bad_def = [
        s for s in samples
        if s.defuse_remaining is not None
        and not (s.bomb_state == "defusing" or s.pcd_phase == "defuse")
    ]
    c.eq(len(bad_def), 0, "defuse_remaining 只在 defusing 或 pcd.phase==defuse 时出现")

    # ---- 5. 拆弹段：数值必须与实测吻合 ---------------------------------
    if verbose:
        print("\n[5] 拆弹段（第 3 次抓包，实测降 9.70s / 实际经过 9.72s）")
    episodes: list[list] = []
    cur: list = []
    for s in samples:
        if s.bomb_state == "defusing":
            cur.append(s)
        elif cur:
            episodes.append(cur)
            cur = []
    if cur:
        episodes.append(cur)
    c.ok(len(episodes) >= 1, f"至少抓到一段 defusing（实际 {len(episodes)} 段）")

    if episodes:
        ep = episodes[0]
        first, last = ep[0], ep[-1]
        c.ok(
            abs((first.defuse_remaining or -1) - EXPECTED_DEFUSE_FIRST) < 0.35,
            f"拆弹起始 countdown ≈ {EXPECTED_DEFUSE_FIRST}（实际 {first.defuse_remaining}）→ 无钳 10s",
        )
        c.ok(
            0.0 <= (last.defuse_remaining if last.defuse_remaining is not None else -1) <= 0.3,
            f"拆弹结束时剩余已归零附近（实测 bomb.countdown=0.1 / pcd.phase_ends_in=0.0，"
            f"解析取 {last.defuse_remaining}）",
        )
        c.ok(
            all(s.explosion_remaining is None for s in ep),
            "整个拆弹期间 explosion_remaining 恒为 None（countdown 已被拆弹占用）",
        )
        seq = [s.defuse_remaining for s in ep if s.defuse_remaining is not None]
        c.ok(
            all(b <= a + 1e-9 for a, b in zip(seq, seq[1:])),
            "拆弹剩余单调不增",
        )
        c.ok(
            all(s.pcd_phase == "defuse" for s in ep),
            "拆弹期间 phase_countdowns.phase == 'defuse'",
        )
        c.ok(
            all(s.bomb_player for s in ep),
            "拆弹期间 bomb.player 一直标识拆弹者",
        )
        # bomb.countdown 与 phase_countdowns.phase_ends_in 是同一件事的两种视图，
        # 实测两者最多差 0.1s。它们互为佐证：只有两个都读、并且都指向"拆弹"，
        # 才能确信 countdown 这一刻不是爆炸剩余。
        pairs = [
            (s.bomb_countdown, s.pcd_ends_in) for s in ep
            if s.bomb_countdown is not None and s.pcd_ends_in is not None
        ]
        c.ok(len(pairs) >= 10, f"拆弹期间两个倒计时字段都可用（{len(pairs)} 对）")
        c.ok(
            all(abs(a - b) <= 0.3 for a, b in pairs),
            "bomb.countdown 与 phase_countdowns.phase_ends_in 在拆弹期间一致（互为佐证）",
        )
        c.ok(
            all(
                s.bomb_countdown is None
                or s.explosion_remaining != s.bomb_countdown
                or s.bomb_countdown == s.pcd_ends_in
                for s in ep
            ),
            "拆弹期间绝没有把 bomb.countdown 当成爆炸剩余",
        )
        if len(seq) >= 2 and first.recv_mono is not None:
            wall = last.recv_mono - first.recv_mono
            drop = seq[0] - seq[-1]
            c.ok(
                abs(drop - wall) < 0.15,
                f"countdown 的降幅({drop:.3f}s) 与真实经过时间({wall:.3f}s) 一致 "
                "—— 这条就是'defusing 时 countdown = 拆弹剩余'的直接证明",
            )

    # ---- 6. 事件覆盖：安放 / 拆除 / 爆炸 / 回合结束 ---------------------
    if verbose:
        print("\n[6] 事件覆盖")
    planted_events = 0
    prev: str | None = None
    for s in samples:
        if s.bomb_state == "planted" and prev != "planted":
            planted_events += 1
        if s.bomb_state is not None:
            prev = s.bomb_state
    if verbose:
        print(f"    planted 状态段数      : {planted_events}")
        print(f"    含 defused 的包        : {n_state_defused}")
        print(f"    含 exploded 的包       : {n_state_exploded}")
        print(f"    回合结束(over)的包     : {sum(1 for s in samples if s.round_phase == 'over')}")
    c.ok(planted_events >= 3, f"至少 3 段 planted（实际 {planted_events}）")
    c.ok(n_state_defused > 0, "抓到了拆除成功")
    c.ok(n_state_exploded > 0, "抓到了爆炸")
    c.ok(
        sum(1 for s in samples if s.round_phase == "over") > 0,
        "抓到了回合结束",
    )
    c.ok(
        all(s.round_phase != "over" or s.bomb_state in (None, "defused", "exploded")
            for s in samples),
        "回合结束时炸弹状态只会是 None / defused / exploded",
    )

    # ---- 7. 数值字符串解析容错 -----------------------------------------
    if verbose:
        print("\n[7] 数值容错")
    negative = [s for s in samples if s.pcd_ends_in is not None and s.pcd_ends_in < 0]
    c.ok(len(negative) > 0, f"夹具里存在负的 phase_ends_in（实测 {len(negative)} 个）—— 不要假设 >= 0")
    c.ok(
        all(s.bomb_state is not True for s in samples),
        "GSI 增量里的布尔占位符 `true` 没有被当成真实状态",
    )
    c.ok(
        all(
            (s.bomb_countdown is None)
            or isinstance(s.bomb_countdown, float)
            for s in samples
        ),
        "所有 countdown 都是 float 或 None（字符串已被解析）",
    )

    # ---- 8. 幂等 / 无副作用 --------------------------------------------
    if verbose:
        print("\n[8] 解析幂等性")
    again = [s for cap in load_captures(fixture) for s in cap.samples()]
    c.eq(len(again), len(samples), "重复解析结果数量一致")
    c.ok(
        all(a.bomb_state == b.bomb_state and a.defuse_remaining == b.defuse_remaining
            for a, b in zip(samples, again)),
        "重复解析结果逐字段一致（解析器无隐藏状态）",
    )

    if verbose:
        print("\n" + "=" * 72)
        print(f"P0 自检完成: {'全部通过' if c.failed == 0 else '有失败'}  "
              f"[OK {c.passed} / X {c.failed}]")
        print("=" * 72)
    return c.passed, c.failed, c.failures


def main() -> int:
    _, failed, _ = run()
    return 1 if failed else 0
