"""P1 自检：状态机。确定性测试 —— 时间由测试注入，不需要游戏、不需要睡觉。

覆盖的每一条都对应需求书里的一条规则，或者一处"1 代踩过的坑"。

需求变更（删掉"拆弹倒计时绿字"，只剩"红字"）之后，原 [3][3b][3c][4][5][6][7][8][9][10]
[13][15] 这些测音频拆弹/绿字/假拆/观战拆弹真值/音频接管安放的块整块删掉了 ——
它们测的是已经不存在的功能。[17]（爆炸/回合结束的音频事件）测的是那代音频链路，
2026-09-16 随它一起移除（见 docs/实机反馈与待办.md 第十五节）。
⚠️ 音频在 2026-09-17 以**指纹方案**回归，但它的自检在 `tests/test_app.py` 第 [12] 节，
本模块仍只管状态机本身的规则（含 `feed_audio_plant` 的 T0 修正窗口）。
保留的块按新语义适配，并补了 5 组新规则（见文末 A 段）。
"""

from __future__ import annotations

from dataclasses import replace

from c4timer.config import Config
from c4timer.gsi import GsiSample
from c4timer.state import (
    ARMED,
    CALIB_TIGHT,
    CALIB_WINDOW,
    DEFUSED,
    EXPLODED,
    IDLE,
    LATE_PLANT_GUARD,
    C4StateMachine,
    calibration_error,
)

from .checks import Checks

_BASE = GsiSample(
    recv_mono=0.0,
    recv_wall="",
    observer=False,
    observed_steamid="me",
    self_steamid="me",
    observing_other=False,
    round_phase="live",
    map_round=1,
    map_name="de_dust2",
    map_mode="competitive",
    bomb_state=None,
    bomb_state_raw=None,
    round_bomb=None,
    explosion_remaining=None,
    defuse_remaining=None,
    pcd_phase=None,
    pcd_ends_in=None,
    bomb_countdown=None,
    bomb_player=None,
    defuser_kit=None,
    top_keys=(),
    allplayers_count=0,
)


def sample(now: float, **kw) -> GsiSample:
    return replace(_BASE, recv_mono=now, **kw)


def machine() -> C4StateMachine:
    cfg = Config()
    cfg.debug.print_events = False      # 测试时别刷屏
    # 本文件的算术按 comp=0.9 写死（40−0.9=39.1、39.1→0.1 之类）。
    # 产品默认值会随实测漂移（已经 0.9 → 1.85），在这里钉住，
    # 以后调产品默认值就不会把这一堆算术弄坏。
    cfg.timers.plant_latency_compensation = 0.9
    return C4StateMachine(cfg)


def _plant_at(sm: C4StateMachine, now: float) -> None:
    """走"活着时靠 round.bomb 得知已安放"这条主路径。

    ⚠️ 最后两包的间隔（3 秒）**必须大于补偿值**（测试里用 0.9，标定那组用到 1.84），
    否则会触发"间隔上界夹取"（见 `state._comp_for_this_plant`），
    本函数就变成在测夹取、而不是在测常数补偿了。
    """
    sm.feed_gsi(sample(now - 5.0))
    sm.feed_gsi(sample(now - 3.0, round_phase="live"))
    sm.feed_gsi(sample(now, round_phase="live", bomb_state="planted", round_bomb="planted"))


def _local_bang(sm: C4StateMachine) -> float:
    """本地兜底判"爆炸"的时刻（tick 必须等到这一刻才敢下结论）。"""
    assert sm.planted_mono is not None
    return (sm.planted_mono + sm.cfg.timers.bomb_seconds
            + sm.cfg.timers.explode_after_deadline_sec
            + sm.cfg.timers.local_bang_grace_sec)


def run(verbose: bool = True) -> tuple[int, int, list[str]]:
    c = Checks()
    if verbose:
        print("=" * 72)
        print("P1 自检：状态机")
        print("=" * 72)

    # ================================================================
    if verbose:
        print("\n[1] 活着安放 -> 40s 自走秒 -> 时限归零不抢爆 -> 本地兜底爆炸 -> 1.5s 后隐藏")
    sm = machine()
    _plant_at(sm, 2.0)
    # t0 = 2.0 - 0.9(补偿) = 1.1
    c.eq(sm.state, ARMED, "安放后进入 ARMED")
    c.near(sm.planted_mono, 1.1, 1e-9, "T0 已按实测延迟补偿回推")
    s = sm.snapshot(2.0)
    c.near(s.explosion_remaining, 39.1, 1e-6, "安放瞬间红字 ≈39.1（不是 40，已补偿）")
    c.eq(s.anchor_unknown, False, "正常回合（见过安放之前的报文）-> T0 可信")
    zero = 1.1 + 40.0
    bang = _local_bang(sm)
    # 红字归零 = "拆弹最后时限"，不是真爆：这一刻**不许**判爆炸
    sm.tick(zero)
    c.eq(sm.state, ARMED, "红字归零那一刻不许抢判爆炸（最后一秒里拆弹还可能成功）")
    s = sm.snapshot(zero)
    c.ok(s.deadline_passed, "红字归零 -> deadline_passed 为真（界面写「时限已到 · 即将爆炸」）")
    c.near(s.explosion_remaining, 0.0, 1e-6, "归零后红字夹在 0.0")
    c.near(bang, zero + 1.05 + 0.6, 1e-9,
           "本地兜底时刻 = 归零 + 真爆滞后 + 宽限")
    sm.tick(bang - 0.01)
    c.eq(sm.state, ARMED, "兜底时刻之前（还差 0.01s）仍然不许判爆炸")
    sm.tick(bang)
    c.eq(sm.state, EXPLODED, "到本地兜底时刻 -> 爆炸")
    s = sm.snapshot(bang)
    c.eq(s.explosion_remaining, 0.0, "爆炸时红字显示 0.0")
    c.ok(s.visible, "爆炸时窗口可见")
    sm.tick(bang + 1.5)
    c.eq(sm.state, IDLE, "结束文案停 1.5s 后回到 IDLE")
    c.ok(not sm.snapshot(9999.0).visible, "IDLE 时窗口不可见（默认不常驻）")

    # ---- 活着那一路的"间隔上界夹取"（2026-09-16 加，见 docs/实机反馈与待办.md 第十六节）----
    # 上一包里炸弹还没安放 ⇒ 真实安放必然在它之后 ⇒ L_p ≤ 间隔。
    # 间隔明显小于常数补偿时改用间隔（+0.2s 余量）当补偿。
    sm = machine()                             # 测试里 comp = 0.9
    sm.feed_gsi(sample(0.4))
    sm.feed_gsi(sample(0.9, round_phase="live"))          # chg=0（心跳）-> 走夹取
    sm.feed_gsi(sample(1.3, round_phase="live", bomb_state="planted", round_bomb="planted"))
    # 间隔 0.4s -> bound = max(0.3, 0.6) = 0.6 < 0.9 -> 本局补偿 0.6
    c.near(sm.planted_mono, 1.3 - 0.6, 1e-9, "chg=0 -> 走硬上界夹取「间隔+0.2」")
    line = next((e.text for e in sm.events if "安放(gsi+comp)" in e.text), "")
    c.ok("夹取" in line and "间隔=0.40s" in line and "chg=0" in line and "本局补偿=0.60" in line,
         f"安放日志标明用了夹取及其参数 —— 实际 {line!r}")

    sm = machine()                             # 保底：间隔极小也不把补偿压到 0
    sm.feed_gsi(sample(1.00))
    sm.feed_gsi(sample(1.05, round_phase="live"))
    sm.feed_gsi(sample(1.10, round_phase="live", bomb_state="planted", round_bomb="planted"))
    c.near(sm.planted_mono, 1.10 - 0.3, 1e-9, "间隔极小 -> 补偿保底 0.3s（防陈旧滞留包）")

    sm = machine()                             # 间隔够大时不动常数补偿
    _plant_at(sm, 2.0)
    c.near(sm.planted_mono, 1.1, 1e-9, "间隔 1.5s > 常数补偿 0.9 -> 不夹取")

    # ---- ① 锚点：上一包若是"被安放事件触发的那一包"（chg=1），用「间隔 + 0.10」 ----
    # 实测残差 +0.102s / σ=0.007（见 state.ANCHOR_LEAD）
    sm = machine()
    sm.feed_gsi(sample(0.4))
    sm.feed_gsi(sample(1.0, round_phase="live", carried_delta=True))
    sm.feed_gsi(sample(2.0, round_phase="live", bomb_state="planted", round_bomb="planted"))
    # 间隔 1.0s -> 锚点给 1.10（夹取会给 1.20，常数补偿 0.90 会更早）
    c.near(sm.planted_mono, 2.0 - 1.10, 1e-9, "chg=1 -> 用锚点「间隔+0.10」当本局补偿")
    line = next((e.text for e in sm.events if "安放(gsi+comp)" in e.text), "")
    c.ok("锚点" in line and "间隔=1.00s" in line and "本局补偿=1.10" in line,
         f"日志标明用了锚点这条路 —— 实际 {line!r}")

    sm = machine()                             # 间隔 2.5s：仍走锚点（上限 3.0 是为留住 03:47 那局）
    sm.feed_gsi(sample(0.4))
    sm.feed_gsi(sample(1.0, round_phase="live", carried_delta=True))
    sm.feed_gsi(sample(3.5, round_phase="live", bomb_state="planted", round_bomb="planted"))
    # 03:47 那局实测：间隔 2.52s、真实 L_p ≥ 2.60 —— 锚点给 2.62 是对的，别把它挡掉
    c.near(sm.planted_mono, 3.5 - 2.60, 1e-9, "间隔 2.5s 仍走锚点（上限 3.0）")

    # 间隔 4.22s：2026-09-16 21:32 实机那局 —— 锚点给 4.32，而真实 L_p 只有 0.75~1.08s
    # ⇒ 红字早 3.4 秒（用户报的"快了将近四秒"）。那一包不是被安放触发的。
    sm = machine()
    sm.feed_gsi(sample(0.4))
    sm.feed_gsi(sample(1.0, round_phase="live", carried_delta=True))
    sm.feed_gsi(sample(5.22, round_phase="live", bomb_state="planted", round_bomb="planted"))
    c.near(sm.planted_mono, 5.22 - 0.9, 1e-9, "间隔 4.22s 超过 ANCHOR_COMP_MAX -> 不许走锚点")
    line = next((e.text for e in sm.events if "安放(gsi+comp)" in e.text), "")
    c.ok("夹取" in line and "间隔=4.22s" in line,
         f"大间隔不许当锚点 —— 实际 {line!r}")

    sm = machine()                             # 间隔 >8s：纯保险，退回常数夹取
    sm.feed_gsi(sample(0.4))
    sm.feed_gsi(sample(1.0, round_phase="live", carried_delta=True))
    sm.feed_gsi(sample(12.0, round_phase="live", bomb_state="planted", round_bomb="planted"))
    c.near(sm.planted_mono, 12.0 - 0.9, 1e-9, "间隔 11s 超过 ANCHOR_COMP_MAX -> 退回常数夹取")

    sm = machine()                             # chg=0（心跳做上一包）也不信锚点
    sm.feed_gsi(sample(0.4))
    sm.feed_gsi(sample(1.0, round_phase="live"))          # carried_delta 默认 False
    sm.feed_gsi(sample(2.0, round_phase="live", bomb_state="planted", round_bomb="planted"))
    c.near(sm.planted_mono, 2.0 - 0.9, 1e-9, "chg=0（心跳）-> 退回常数夹取（间隔 1.0 < 0.9? 不夹）")

    # ---- 音频真值（2026-09-17）：听播报拿真实安放时刻 ----
    # 数据依据见 docs/实机反馈与待办.md 第二十三节（三份录音 10 次安放：
    # 召回 10/10，且原始误报全在 live 之外 —— 门控是必须的）。
    sm = machine()
    sm.feed_gsi(sample(10.0, round_phase="live"))
    c.eq(sm.feed_audio_plant(40.0, 42.0, score=0.9, name="zh"), "planted",
         "音频命中 -> 用它直接建立 T0")
    c.near(sm.planted_mono, 40.0, 1e-9, "T0 = 播报命中的时刻（不吃补偿）")
    c.eq(sm.state, ARMED, "音频命中后状态进入 ARMED")
    c.eq(sm.feed_audio_plant(30.0, 42.0), "ignored", "太旧（滞后 >3.5s）不采信")
    c.eq(sm.feed_audio_plant(41.0, 30.0), "ignored", "未来时刻（now < plant）不采信")

    sm = machine()                             # 已知道安放 -> 用来修 T0
    sm.feed_gsi(sample(0.4))
    sm.feed_gsi(sample(1.0, round_phase="live", carried_delta=True))
    sm.feed_gsi(sample(2.0, round_phase="live", bomb_state="planted", round_bomb="planted"))
    t0 = sm.planted_mono
    c.eq(sm.feed_audio_plant(t0 - 1.5, 2.4, score=0.8, name="zh"), "refined",
         "已安放 -> 用音频把 T0 往前后修正")
    c.near(sm.planted_mono, t0 - 1.5, 1e-9, "T0 被修正到音频给的时刻")
    c.eq(sm.feed_audio_plant(t0 - 9.0, 2.4), "ignored", "差得太远不修（宁可不改）")

    # ---- 两者一致就别动（2026-09-19 加）----
    # 锚点实际上只有两种状态：认出"安放触发那一包"时准到 0.02 秒级；认不出时退到常数
    # 补偿、偏 0.3~2.4 秒。所以"音频与锚点差得很少"≈"锚点走的是准的那条路" —— 这时拿
    # 音频（±0.05~0.1 秒）去覆盖它只会更钝。差得多的（延迟那条路）照旧修。
    sm = machine()
    sm.feed_gsi(sample(0.4))
    sm.feed_gsi(sample(1.0, round_phase="live", carried_delta=True))
    sm.feed_gsi(sample(2.0, round_phase="live", bomb_state="planted", round_bomb="planted"))
    t0 = sm.planted_mono
    c.eq(sm.feed_audio_plant(t0 - 0.05, 2.4, score=0.8, name="zh"), "kept",
         "音频与锚点只差 0.05s -> 保留锚点，不动 T0")
    c.near(sm.planted_mono, t0, 1e-9, "kept 时 T0 一格都没动")
    c.ok("audio" not in (sm.calibration_source or ""),
         "kept 时不许标成 audio 来源（T0 不是音频定的，标了会让自成长学错东西）")
    c.eq(sm.feed_audio_plant(t0 - 0.5, 2.4, score=0.8, name="zh"), "refined",
         "差 0.5s（延迟那条路）-> 照旧用音频修")

    sm = machine()                             # 不在 live 里不采信
    sm.feed_gsi(sample(10.0, round_phase="freezetime"))
    c.eq(sm.feed_audio_plant(11.0, 12.5, score=0.9, name="zh"), "ignored",
         "不在 live（买枪/主菜单）里不采信音频")

    # ---- 心跳路的天花板 = **生产默认值 1.0** ----
    # 2026-09-16 实机：CT 守点、上一包是 10 秒心跳，那两局真实 L_p 只有 0.76 / 0.3~0.8 秒，
    # 吃旧值 1.5 就系统偏早 0.7~1.3 秒（用户报的"归零过早"）。见 docs/实机反馈与待办.md 第十八节。
    prod = C4StateMachine(Config())            # 用生产默认值，不吃 machine() 里的 0.9
    prod.cfg.debug.print_events = False
    prod.feed_gsi(sample(20.0))
    prod.feed_gsi(sample(30.0, round_phase="live"))       # 10.0s 心跳做上一包
    prod.feed_gsi(sample(32.26, round_phase="live", bomb_state="planted", round_bomb="planted"))
    # 间隔 2.26s -> 夹取给 max(0.3, 2.46)=2.46，但天花板是 1.0 -> 本局补偿 1.0
    c.near(prod.planted_mono, 32.26 - 1.0, 1e-9,
           "心跳做上一包 -> 补偿吃生产默认天花板 1.0（不是 1.5）")
    line = next((e.text for e in prod.events if "安放(gsi+comp)" in e.text), "")
    c.ok("夹取" in line and "chg=0" in line and "本局补偿=1.00" in line,
         f"心跳路的安放日志记为夹取 + 本局补偿=1.00 —— 实际 {line!r}")

    # 最后一秒里拆弹成功：本地时限已过、还没到本地兜底爆炸 -> 必须是 DEFUSED
    sm = machine()
    _plant_at(sm, 2.0)
    sm.tick(1.1 + 40.0 + 0.3)                  # 时限过了 0.3s，兜底还差 1.35s
    c.eq(sm.state, ARMED, "本地时限已过、兜底未到 -> 仍不判爆炸")
    sm.feed_gsi(sample(1.1 + 40.0 + 0.3, round_phase="over", map_round=1,
                       bomb_state="defused", round_bomb="defused"))
    c.eq(sm.state, DEFUSED, "最后一秒里收到 defused -> DEFUSED（不许先闪 EXPLODED）")

    # ================================================================
    if verbose:
        print("\n[2] GSI 报 defused（活着也能拿到）-> 真拆")
    sm = machine()
    _plant_at(sm, 2.0)
    sm.feed_gsi(sample(20.0, round_phase="over", bomb_state="defused", round_bomb="defused"))
    c.eq(sm.state, DEFUSED, "round.bomb=defused -> 拆弹成功")
    c.eq(sm.snapshot(20.0).status_text, "拆弹成功 · 炸弹已解除", "终局文案正确")
    sm.tick(21.5)
    c.eq(sm.state, IDLE, "终局停留 1.5s 后隐藏")

    # ================================================================
    if verbose:
        print("\n[11] 回合边界：over / freezetime 一律重置")
    sm = machine()
    _plant_at(sm, 2.0)
    sm.feed_gsi(sample(30.0, round_phase="over", bomb_state=None, round_bomb=None))
    c.eq(sm.state, IDLE, "phase=over 且无炸弹事件 -> 重置")

    sm = machine()
    _plant_at(sm, 2.0)
    sm.feed_gsi(sample(30.0, round_phase="freezetime", map_round=2))
    c.eq(sm.state, IDLE, "phase=freezetime -> 重置")

    if verbose:
        print("\n[12] 终局残留：freezetime + 无 round.bomb 不得误报爆炸")
    sm = machine()
    _plant_at(sm, 2.0)
    sm.feed_gsi(sample(30.0, round_phase="freezetime", map_round=2,
                       bomb_state=None, bomb_state_raw="exploded", round_bomb=None))
    c.eq(sm.state, IDLE, "解析层压制后 -> 既不是 EXPLODED 也已重置")

    if verbose:
        print("\n[14] 回合结束后，残留的 planted 不得复活（铁律 5 的状态机侧）")
    sm = machine()
    _plant_at(sm, 2.0)
    sm.feed_gsi(sample(30.0, round_phase="over"))
    sm.feed_gsi(sample(31.0, round_phase="over", bomb_state="planted", round_bomb="planted"))
    c.eq(sm.state, IDLE, "回合已结束，残留 planted 被拒绝")

    # ================================================================
    if verbose:
        print("\n[16] 观战对时：大偏差直接对齐、小偏差平滑吸收")
    sm = machine()
    _plant_at(sm, 2.0)
    sm.feed_gsi(sample(3.0, observer=True, observed_steamid="b", observing_other=True,
                       bomb_state="planted", explosion_remaining=36.0, pcd_phase="bomb",
                       top_keys=("bomb",)))
    c.near(sm.planted_mono, -1.0, 1e-6, "偏差 2.1s >= 0.5 -> 直接对齐")
    c.near(sm.snapshot(3.0).explosion_remaining, 36.0, 1e-6, "对齐后红字等于真值")

    sm = machine()
    _plant_at(sm, 2.0)
    before = sm.planted_mono
    sm.feed_gsi(sample(3.0, observer=True, observed_steamid="b", observing_other=True,
                       bomb_state="planted", explosion_remaining=38.0, pcd_phase="bomb",
                       top_keys=("bomb",)))
    c.near(sm.planted_mono, before - 0.08, 1e-6, "偏差 0.1s < 0.5 -> 每包最多挪 0.08s")

    # E2 实测（2026-09-15 23:35:35）：归零之后游戏会有一包把倒计时从 0.0 直跳到 −1.9。
    # 那是假值；照单全收会把 T0 推后 1.8 秒，本地兜底阈值"早已过点"，
    # "炸弹已爆炸"就比真爆早约 0.9 秒出现。夹到 0 之后只当"时限已过"。
    sm = machine()
    _plant_at(sm, 2.0)                       # T0 = 1.1，红字归零 = 41.1
    zero = sm.planted_mono + sm.cfg.timers.bomb_seconds
    sm.feed_gsi(sample(zero + 0.065, observer=True, observed_steamid="b",
                       observing_other=True, bomb_state="planted",
                       explosion_remaining=-1.9, pcd_phase="bomb", top_keys=("bomb",)))
    c.ok(abs(sm.planted_mono - 1.1) <= 0.08,
         "负倒计时只当「时限已过」：T0 不被推后 1.8 秒（最多平滑挪一步）")
    sm.tick(zero + 0.07)
    c.eq(sm.state, ARMED, "收到 −1.9 之后也不许提前判爆炸（本地兜底还没到）")
    s = sm.snapshot(zero + 0.07)
    c.ok(s.deadline_passed, "界面仍是「时限已到 · 即将爆炸」，不是「炸弹已爆炸」")

    # 2026-09-16：从"活着靠补偿锚定"切到"服务端真值"的那一刻，写一行本局补偿误差 ——
    # 这是活着那一路**唯一**能直接量出真实安放延迟 L_p 的点（实机反馈"面板跳出就 38 秒"
    # 到底是我们快/慢多少，就靠这行攒样本）。
    sm = machine()                            # 测试里 comp = 0.9
    _plant_at(sm, 2.0)                        # T0 = 1.1
    sm.feed_gsi(sample(3.0, observer=True, observed_steamid="b", observing_other=True,
                       bomb_state="planted", explosion_remaining=38.5, pcd_phase="bomb",
                       top_keys=("bomb",)))
    line = next((e.text for e in sm.events if "本局补偿误差" in e.text), None)
    c.ok(line is not None, "切到真值时写了一行补偿误差")
    c.ok(line is not None and "L_p≈0.50" in line,
         f"偏差 +0.40s、补偿 0.90 -> 实际延迟 L_p≈0.50s（实际 {line!r}）")
    c.eq(sm.calibration_source, "gsi-truth", "切过真值之后标定来源要跟着改")

    # ================================================================
    if verbose:
        print("\n[18] 快照在状态之间保持一致")
    sm = machine()
    c.ok(sm.snapshot(0.0).is_idle, "初始为 IDLE")
    c.ok(not sm.snapshot(0.0).visible, "IDLE 不可见")
    c.eq(sm.snapshot(0.0).explosion_remaining, None, "IDLE 无红字")
    c.ok(not sm.snapshot(0.0).deadline_passed, "IDLE 不算「时限已到」")
    _plant_at(sm, 2.0)
    c.ok(sm.snapshot(2.0).planted, "ARMED 时 planted 为真")
    c.ok(sm.snapshot(2.0).visible, "ARMED 可见")
    c.ok(not sm.snapshot(2.0).is_outcome, "ARMED 不是终局")

    # ================================================================
    if verbose:
        print("\n[19] 迟到的 planted 不得复活已判定的终局（demo 在真实报文上抓到的 bug）")
    sm = machine()
    _plant_at(sm, 2.0)
    bang = _local_bang(sm)
    sm.tick(bang)
    c.eq(sm.state, EXPLODED, "本地兜底时刻 -> EXPLODED")
    # 一包"爆炸前生成、爆炸后才到"的 live/planted（真实数据里晚了约 1 秒）
    sm.feed_gsi(sample(bang + 0.5, round_phase="live", map_round=1,
                       bomb_state="planted", round_bomb="planted"))
    c.eq(sm.state, EXPLODED,
         "迟到的 planted 不得把已判定的爆炸复活成 ARMED（会造成 1 秒假闪烁）")
    sm.feed_gsi(sample(bang + 1.0, round_phase="over", map_round=1))
    c.eq(sm.state, IDLE, "回合 over -> 重置")
    sm.feed_gsi(sample(bang + 6.0, round_phase="freezetime", map_round=2))
    sm.feed_gsi(sample(bang + 10.0, round_phase="live", map_round=2))
    sm.feed_gsi(sample(bang + 11.0, round_phase="live", map_round=2,
                       bomb_state="planted", round_bomb="planted"))
    c.eq(sm.state, ARMED, "下一回合（over -> freezetime -> live）可以正常重新安放")

    # ================================================================
    if verbose:
        print("\n[20] 时间兜底：万一 GSI 跳过了 over，也绝不能让倒计时永久失效")
    sm = machine()
    _plant_at(sm, 2.0)
    bang = _local_bang(sm)
    sm.tick(bang)
    c.eq(sm.state, EXPLODED, "先判出一次爆炸（把 _round_resolved 置起来）")
    # 模拟 GSI 丢包：直接跳到下一回合的 live，中间**没有** over / freezetime
    # 以前这里会让 _round_resolved 永远清不掉 -> 之后每一回合都没有倒计时（静默失败）
    later = bang + LATE_PLANT_GUARD + 2.0
    sm.feed_gsi(sample(later, round_phase="live", map_round=3,
                       bomb_state="planted", round_bomb="planted"))
    c.eq(sm.state, ARMED,
         f"跳过 over 之后，超过时间兜底（{LATE_PLANT_GUARD:.0f}s）新安放必须能生效")
    c.ok(sm.planted_mono is not None, "T0 已建立")

    # 但兜底期内仍然必须拒绝迟到的 planted（原来那个 bug 不能被放回来）
    sm = machine()
    _plant_at(sm, 2.0)
    bang = _local_bang(sm)
    sm.tick(bang)
    within = bang + LATE_PLANT_GUARD - 0.5
    sm.feed_gsi(sample(within, round_phase="live", map_round=1,
                       bomb_state="planted", round_bomb="planted"))
    c.eq(sm.state, EXPLODED,
         f"兜底期内（{LATE_PLANT_GUARD - 0.5:.1f}s）的迟到 planted 仍被拒绝")

    # ================================================================
    # A 段：需求变更后新加/加强的规则
    # ================================================================
    if verbose:
        print("\n[A1] 回合在 40 秒倒计时没走完时结束 -> 也必须重置")
    # 需求原文："40 秒内回合结束也要重置"。这条以前被绿字那套盖住，现在单独钉住。
    for phase, label in (("over", "over"), ("freezetime", "freezetime")):
        sm = machine()
        _plant_at(sm, 2.0)                     # 红字还有 ~39s，远远没走完
        s = sm.snapshot(2.0)
        c.ok(s.planted and s.explosion_remaining > 39.0 - 1e-6,
             f"重置前红字还剩 {s.explosion_remaining:.1f}s（回合确实提前结束）")
        sm.feed_gsi(sample(3.0, round_phase=phase, map_round=1,
                           bomb_state=None, round_bomb="planted"))
        c.eq(sm.state, IDLE,
             f"回合 {label} 时红字只走了 1s -> 必须重置为 IDLE（不许把 39s 走完）")

    if verbose:
        print("\n[A2] 本地兜底爆炸的时刻（把新常量钉死）")
    sm = machine()
    _plant_at(sm, 2.0)
    zero = sm.planted_mono + sm.cfg.timers.bomb_seconds
    lag = sm.cfg.timers.explode_after_deadline_sec
    grace = sm.cfg.timers.local_bang_grace_sec
    c.eq(grace, 0.6, "local_bang_grace_sec 产品默认值 = 0.6")
    sm.tick(zero + lag + grace - 1e-9)
    c.eq(sm.state, ARMED,
         f"归零+{lag}+{grace} 之前的最后一刻仍为 ARMED（不许提前判爆炸）")
    sm.tick(zero + lag + grace)
    c.eq(sm.state, EXPLODED,
         f"到达 planted_mono + 40 + {lag} + {grace} -> 必须判 EXPLODED")

    if verbose:
        print("\n[A3] anchor_unknown：回合中途才启动（第一包就是 planted）")
    # 模拟"程序在回合中途启动/重连"：本回合**从没见过**非 planted 的报文
    sm = machine()
    sm.feed_gsi(sample(100.0, round_phase="live", map_round=1,
                       bomb_state="planted", round_bomb="planted"))
    c.eq(sm.state, ARMED, "中途启动也能凭 round.bomb=planted 进入 ARMED")
    c.eq(sm.anchor_unknown, True, "没见过「安放之前」的报文 -> anchor_unknown=True")
    s = sm.snapshot(100.0)
    c.eq(s.anchor_unknown, True, "快照里 anchor_unknown 也是 True（界面要写「时限未知」）")
    c.near(s.explosion_remaining, 40.0 - 0.9, 1e-6,
           "锚点仍按「本包时刻 - 补偿」估（数字不可信，但红字结构不变）")
    c.eq(sm._saw_round_before_plant, False, "内部标记：本回合还没见过「安放之前」")
    # 中途启动的回合不允许被音频/炸弹事件"凭空"变可信
    c.eq(sm.anchor_unknown, True, "没有服务端真值时不可信状态不会被别的路径清掉")
    # 一直往下走，直到回合结束重置
    sm.feed_gsi(sample(150.0, round_phase="over", map_round=1))
    c.eq(sm.state, IDLE, "中途启动的回合照样按 over 重置")
    c.eq(sm.anchor_unknown, False, "重置时 anchor_unknown 清掉")

    if verbose:
        print("\n[A4] anchor_unknown：正常回合（先看到无炸弹报文，再看到 planted）")
    sm = machine()
    _plant_at(sm, 2.0)                          # _plant_at 先喂了两包 bomb_state=None
    c.eq(sm.anchor_unknown, False, "见过「安放之前」的报文 -> anchor_unknown=False")
    c.eq(sm._saw_round_before_plant, True, "内部标记：见过「安放之前」的报文")
    # 本回合后续包全是 planted，也不能把可信标记弄丢
    sm.feed_gsi(sample(3.0, round_phase="live", map_round=1,
                       bomb_state="planted", round_bomb="planted"))
    c.eq(sm.anchor_unknown, False, "本回合后续 planted 包不影响可信性")
    # 新回合里**第一个**包就是 live+planted（freezetime 的包丢了/程序刚重连）
    # -> 同样算「没见过安放之前」，T0 不许信
    sm2 = machine()
    _plant_at(sm2, 2.0)
    sm2.feed_gsi(sample(3.0, round_phase="over", map_round=1))       # 本回合结束
    sm2.feed_gsi(sample(4.0, round_phase="live", map_round=2,
                        bomb_state="planted", round_bomb="planted"))  # 新回合直接 planted
    c.eq(sm2.anchor_unknown, True,
         "新回合里第一个可见包就是 planted -> 同样算「没见过安放之前」")
    c.eq(sm2.state, ARMED, "新回合的 planted 照样进入 ARMED（只是数字不可信）")
    # 对照：新回合里只要先看到一个"无炸弹"的包（例如 freezetime），就重新可信
    sm3 = machine()
    _plant_at(sm3, 2.0)
    sm3.feed_gsi(sample(3.0, round_phase="over", map_round=1))
    sm3.feed_gsi(sample(4.0, round_phase="freezetime", map_round=2))
    sm3.feed_gsi(sample(5.0, round_phase="live", map_round=2,
                        bomb_state="planted", round_bomb="planted"))
    c.eq(sm3.anchor_unknown, False,
         "新回合里先看到 freezetime（无炸弹）再 planted -> T0 依然可信")

    if verbose:
        print("\n[A5] anchor_unknown：拿到服务端真值后必须转为可信")
    sm = machine()
    sm.feed_gsi(sample(100.0, round_phase="live", map_round=1,
                       bomb_state="planted", round_bomb="planted"))
    c.eq(sm.anchor_unknown, True, "先建立不可信锚点")
    sm.feed_gsi(sample(101.0, round_phase="live", map_round=1, observer=True,
                       observed_steamid="b", observing_other=True,
                       bomb_state="planted", round_bomb="planted",
                       explosion_remaining=30.0, pcd_phase="bomb", top_keys=("bomb",)))
    c.eq(sm.anchor_unknown, False, "服务端倒计时到了 -> T0 可信")
    c.eq(sm.snapshot(101.0).anchor_unknown, False, "快照同步为可信")

    # ================================================================
    # 标定：误差区间的**符号方向**
    # ================================================================
    # 这一条是防止"comp 调大 = 归零推后"这个方向性错误再犯。
    prod = Config()
    c.ok(1.0 <= prod.timers.plant_latency_compensation <= 2.5,
         f"产品默认补偿值在合理区间（红字对齐「截止时刻」口径）—— "
         f"实际 {prod.timers.plant_latency_compensation}")
    lag = prod.timers.explode_after_deadline_sec
    notify = prod.timers.explosion_notify_latency

    # 没有上一个报文时退化成点估计：本地归零 140.0，截止时刻比它早 0.3s（偏慢）
    # 截止时刻 = 报爆炸时刻 − 通知延迟 − 真爆滞后
    cal = calibration_error(prod, 140.0, 140.0 - 0.3 + notify + lag)
    c.ok(cal is not None, "calibration_error 有 T0 时给出结果")
    if cal:
        c.near(cal[0], 0.3, 1e-6, "误差下界 +0.3（归零晚于截止时刻）")
        c.near(cal[1], 0.3, 1e-6, "没有上一个报文时区间退化为一点")

    cal = calibration_error(prod, 140.0, 140.0 + 0.2 + notify + lag)
    # 下面几个 `if cal:` 是护栏，但**没有**配套的显式断言时，cal 为 None 会让整块静默跳过
    # （总数悄悄少 3 条、却不报错 —— 2026-09-16 两个会话同时改这个文件时正是这么被误读成
    # "总数不是定数"的）。所以每块前面都先显式断言一次。
    c.ok(cal is not None, "calibration_error 在偏快情形下也给结果")
    if cal:
        c.near(cal[0], -0.2, 1e-6, "误差 -0.2 表示本地归零早于截止时刻")
        c.near(cal[1], -0.2, 1e-6, "点估计同样退化")

    c.eq(calibration_error(prod, None, 0.0), None, "没有 T0 时不标定")

    # 有上一个报文时必须给**区间**：爆炸就落在这两个报文之间。
    # 例：上一个报文在 139.0，报爆炸在 140.07（= 真爆 140.0），本地归零 140.0，
    #     截止时刻 = 140.0 − 1.05 = 138.95
    #   -> 爆炸 ∈ (139.0, 140.0]（真爆口径）-> 截止时刻 ∈ (137.95, 138.95]
    #   -> 误差 ∈ [140.0−138.95, 140.0−137.95] = [1.05, 2.05]
    cal = calibration_error(prod, 140.0, 140.0 + notify, 139.0)
    c.ok(cal is not None, "calibration_error 有上一个报文时给出区间")
    if cal:
        c.near(cal[0], lag, 1e-6, "区间下界 = 归零 − 截止时刻（爆炸取最晚）")
        c.near(cal[1], 1.0 + lag, 1e-6, "区间上界 = 归零 − 上一个报文 + 真爆滞后")
        c.ok(cal[1] > cal[0], "上一个报文隔得远 -> 区间宽，说明这一局证明不了什么")

    # 观战时包很密（0.2s）-> 区间窄，这才有资格给建议
    cal = calibration_error(prod, 140.0, 140.0 + notify, 139.9)
    c.ok(cal is not None, "calibration_error 在密报文情形下给出区间")
    if cal:
        c.ok(cal[1] - cal[0] <= CALIB_TIGHT, "报文间隔 0.2s -> 区间窄到可以给建议")

    # 实测那一局：planted 报文 → 真实爆炸 39.21s。红字要的是**截止时刻**（比真爆早 1.05s），
    # 所以最优 comp = 40 − (39.21 − 1.05) = 1.84。
    interval = 39.21
    sm = machine()
    cfg = sm.cfg
    cfg.timers.plant_latency_compensation = (
        40.0 - (interval - cfg.timers.explode_after_deadline_sec))
    p = 500.0
    _plant_at(sm, p)
    sm.feed_gsi(sample(p + interval + cfg.timers.explosion_notify_latency,
                       round_phase="live", map_round=3,
                       bomb_state="exploded", round_bomb=None))
    c.eq(sm.state, EXPLODED, "GSI 报 exploded -> EXPLODED")
    c.ok(sm.last_calibration is not None, "报 exploded 时留下了标定结果")
    if sm.last_calibration:
        c.near(sm.last_calibration[0], 0.0, 0.02,
               "补偿按「截止时刻」标定后，误差下界应当≈0")
    c.eq(sm.calibration_source, "gsi+comp", "标定记录 T0 来源")

    # 拆弹成功不算标定（不是爆炸，没有可比的真值）
    sm = machine()
    _plant_at(sm, 700.0)
    sm.feed_gsi(sample(720.0, round_phase="live", map_round=4,
                       bomb_state="defused", round_bomb=None))
    c.eq(sm.state, DEFUSED, "defused -> DEFUSED")
    c.eq(sm.last_calibration, None, "拆弹成功不产生标定行")

    # --- 实机发现的坑：本地计时器**总是**先归零（那是它的本职工作），
    # GSI 的 exploded 报文晚 0.07~0.4 秒才到。标定因此不能要求"还没判过终局"，
    # 否则最常见的这一种情况一个字都不会记 —— 实机第一次跑出来就是空的。
    sm = machine()
    p = 900.0
    _plant_at(sm, p)
    zero = p - 0.9 + 40.0
    planted_mono = sm.planted_mono
    sm.tick(zero + 1.05 + 0.6 + 0.01)                      # 本地兜底先判爆炸
    c.eq(sm.state, EXPLODED, "本地计时器先判爆炸")
    c.eq(sm.last_calibration, None, "此刻 GSI 报文还没到，自然没有标定")
    sm.feed_gsi(sample(zero + 0.3, round_phase="over", map_round=5,
                       bomb_state="exploded", round_bomb="exploded"))
    c.ok(sm.last_calibration is not None, "本地先判爆炸后，GSI 的 exploded 仍能标定")
    if sm.last_calibration:
        # 截止时刻 ≈ zero+0.3−0.07−1.05，而本地归零在 zero
        #   -> 误差下界 = 1.05 − 0.3 + 0.07 = 0.82（归零比截止时刻晚 0.82s）
        lag = sm.cfg.timers.explode_after_deadline_sec
        c.near(sm.last_calibration[0], lag - 0.3 + 0.07, 0.02,
               "误差下界 = 本地归零 − 截止时刻")
        # 这一局中途一个包都没有：上一个报文就是安放那包（p=900.0），
        # 归零在 zero=939.1 -> 上界 = 939.1 − 900.0 + 1.05 ≈ 40.15，区间宽。
        # 这里用 planted_mono 推（而不是写死秒数），换补偿值也不会坏。
        gap = zero - (planted_mono + sm.cfg.timers.plant_latency_compensation)
        c.near(sm.last_calibration[1], gap + lag, 0.05,
               "上界 = 归零 − 上一个报文 + 真爆滞后（中间没包 -> 区间宽）")
        c.ok(sm.last_calibration[1] - sm.last_calibration[0] > CALIB_TIGHT,
             "区间宽 -> 这种局不能拿来调补偿值")

    # 观战时 GSI 会把 exploded 反复重发（真实抓包里同一局 5 个包），
    # 只有首次那个代表真实爆炸；重复包既要去重、也要被窗口挡掉。
    sm.last_calibration = None                              # 模拟 app 报完就清
    sm.feed_gsi(sample(zero + 3.5, round_phase="freezetime", map_round=5,
                       bomb_state="exploded", round_bomb="exploded"))
    c.eq(sm.last_calibration, None, "残留/重复的 exploded 包不再标定（去重 + 窗口）")

    # 窗口本身：归零过了 5 秒才收到 exploded，宁可不错报
    sm = machine()
    _plant_at(sm, 300.0)
    sm.feed_gsi(sample(300.0 - 0.9 + 40.0 + 5.0, round_phase="over", map_round=6,
                       bomb_state="exploded", round_bomb="exploded"))
    c.eq(sm.state, EXPLODED, "迟到 5 秒的 exploded 仍然判爆炸")
    c.eq(sm.last_calibration, None, "但超过 {:.0f}s 窗口就不标定（可能是残留包）"
         .format(CALIB_WINDOW))

    # T0 一旦被服务端真值对齐，标定行的"建议补偿值"就失去意义（补偿没参与），
    # 必须把来源改成 gsi-truth —— app 看到非 gsi+comp 就不打建议值。
    sm = machine()
    _plant_at(sm, 100.0)                                   # 来源 gsi+comp
    c.eq(sm.calibration_source, "gsi+comp", "活着时安放 -> 来源 gsi+comp")
    sm.feed_gsi(sample(101.0, round_phase="live", map_round=1, observer=True,
                       bomb_state="planted", round_bomb="planted",
                       explosion_remaining=30.0))          # 看到服务端倒计时
    c.eq(sm.calibration_source, "gsi-truth", "被服务端真值对齐后 -> 来源 gsi-truth")
    # 真值说 101.0 时还剩 30s -> 本地归零被对到 131.0
    sm.feed_gsi(sample(131.05, round_phase="live", map_round=1, observer=True,
                       bomb_state="exploded", round_bomb=None))
    c.ok(sm.last_calibration is not None, "真值对齐的回合也留标定（误差仍是有用信息）")
    c.eq(sm.calibration_source, "gsi-truth", "此时来源仍是 gsi-truth（所以不打建议值）")

    if verbose:
        print("\n" + "=" * 72)
        print(f"P1 自检完成: {'全部通过' if c.failed == 0 else '有失败'}  "
              f"[OK {c.passed} / X {c.failed}]")
        print("=" * 72)
    return c.passed, c.failed, c.failures


def main() -> int:
    _, failed, _ = run()
    return 1 if failed else 0
