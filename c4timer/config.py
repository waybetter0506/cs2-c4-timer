"""配置：dataclass 定义 + JSON 读写。

原则：所有会因环境而变的量都进配置，代码里不写死。
默认值取自 2026-09-15 的实测结果。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .paths import app_root

# 程序所在目录（冻结运行时 = exe 目录，源码运行时 = 仓库根）。见 c4timer/paths.py。
ROOT = app_root()
DEFAULT_PATH = ROOT / "config.json"
# 上一次 load() 的失败原因（BOM、语法错、读不了……）。doctor 会把它打出来，
# 因为"配置被静默忽略"是最难查的一类问题。
LAST_LOAD_ERROR: str | None = None


@dataclass
class Cs2Config:
    """CS2 装在哪。

    **默认全自动**（`c4timer/cs2path.py`：注册表 → 扫各盘符 → 读 libraryfolders.vdf），
    正常装法的 Steam 都能找到，不需要用户做任何事。

    这一项是给**自动找不着**的情况留的保险丝（2026-09-19 加）。为什么要留：
    一位使用者的 Steam 装在 `D:\\Program Files (x86)\\Steam`，而当时的自动查找只认
    几条写死的路径 ⇒ 连"体检"和"装GSI配置.bat"都用不了。自动查找那一侧已经修了，
    但**装法永远列不完**（蒸汽平台、完美世界、移动硬盘、网络盘、改过名的目录……），
    让用户自己填一次是最彻底的兜底。

    填什么都行 —— 用 `指定游戏目录.bat` 双击设置，或
    `c4timer set-cs2 --dir "D:\\...\\Counter-Strike Global Offensive"`。
    """

    # 例如 D:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive
    # 也可以填它的 cfg 子目录、steamapps\common、甚至 Steam 根目录 —— 都认。
    game_dir: str | None = None


@dataclass
class GsiConfig:
    host: str = "127.0.0.1"
    # 与已安装的 gamestate_integration_csc4.cfg 保持一致，不要随便改
    port: int = 30077
    # 超过这么久没收到任何合法包 -> 认为 GSI 断连。
    #
    # 必须大于 heartbeat！装好的 cfg 写的是 "heartbeat 10.00"，
    # 实测正常游玩时包间隔中位 ~0.7s、**最长 10.3s**（没有状态变化时就只靠心跳）。
    # 原来是 5.0 —— 结果正常游玩时会误报"GSI 断连"（实机验证时发现）。
    stale_after_sec: float = 15.0


@dataclass
class TimersConfig:
    bomb_seconds: float = 40.0
    # 实测：GSI 报告"已安放"比真实安放时刻晚，所以要把它回推。
    #
    # 关系：本地归零时刻 = 通知时刻 P + bomb_seconds − 本值。
    #       **本值越大，归零越早**（这一点极易搞反）。
    #
    # ★ 语义（用户实机确认后定稿）：**红字归零 = 拆弹的最后时限**，
    #   即"游戏自己那个 40 秒计时器"的归零时刻，**不是**真正的爆炸瞬间
    #   （真爆比它晚约 1.05 秒，见 explode_after_deadline_sec）。
    #   观战真值那条路本来就是截止时刻口径（本地归零 = 报文 + 服务端倒计时），
    #   所以存活路径也要对齐同一个时刻，两条路口径才一致。
    #
    # 取值历史：
    #   · 1.85 = 0.8 + 1.05（"对齐真爆"时代的两条可信测量）
    #   · 1.5（2026-09-16）：实机反馈"旁观完美、活着偏早"，而活着的偏差 = L_p − 补偿。
    #     12 个样本的 L_p 中位 ≈1.5~1.7（夹具直接测量 1.02 / 1.51，23 点那轮 5 局
    #     1.17 / 1.28 / 1.68 / 1.92 / 2.40，旧 cfg 5 局 1.84~2.17）⇒ 取中位 1.5。
    #   · **1.0（2026-09-16 晚，当前值）**：用户又报"当我是 CT、T 下包后归零过早"。
    #     当天日志里那两局都是**心跳包做上一包**（间隔 10.0 秒、chg=0 ⇒ 锚点用不了
    #     ⇒ 吃这个常数），而这两局的真实 L_p 只有 **0.76 秒**（CALIB 下界 −0.74）
    #     和 **0.3~0.8 秒**（爆炸报文落在本地归零后 2.46 秒、被 ±2.0 秒标定窗挡掉，
    #     反推误差 ≈ −1.0~−1.3 秒）—— 吃 1.5 就系统偏早 0.7~1.3 秒。
    #     取 1.0 把这两局压到 ~0~0.3 秒。
    #     这一路**没有**可用信息：心跳说明那段时间客户端什么都没变，
    #     下包没留下任何触发包，L_p 只能猜（唯一硬界是 L_p ≤ 间隔）。
    #
    # ⚠️ 方向：本值越大归零越早。**锚点那条路不受本值影响**（能认出"安放触发包"时
    #   用 间隔+0.10）；本值只在**锚点用不了**时生效（心跳做上一包 / 没有上一包）。
    #   1.5 → 1.0 是**故意往"偏晚"挪**：用户实测反馈的是"归零过早"。
    # ⚠️ 逐局真实 L_p 在 0.3~2.4 秒之间晃 → 这一路的残差 ±0.6 秒是结构性的，调参解决不了；
    #   要亚秒精度只有观战（服务端倒计时）那条路。E2 已证活着时**拿不到**服务端倒计时。
    plant_latency_compensation: float = 1.0
    # 实测：GSI 的 `exploded` 通知只比真实爆炸晚约 0.07 秒
    # （用录音里"急促滴声转连续轰鸣"那个起振点标定出来的）。
    explosion_notify_latency: float = 0.07
    # ★ 实测：**炸弹真正爆炸比"倒计时归零 / 拆弹最后时限"晚这么多**。
    # 出处：唯一一局同时看得到服务端倒计时的 —— 服务端倒计时归零在 19:07:34.814，
    # 而 19:07:34.930 那包还是 planted、19:07:35.931 才报 exploded（通知延迟 0.07）。
    # 红字的语义是"拆弹最后时限"（用户实机确认），CALIB 的参照系要跟着用它：
    #     误差 = 本地归零 − (爆炸报文时刻 − 通知延迟 − 本值)
    explode_after_deadline_sec: float = 1.05
    # 本地兜底判"爆炸"的宽限：时限到了之后还要再等这么久才敢自己判爆炸。
    # 为什么不能一到时限就判：最后一秒里拆弹仍可能成功（那会先收到 defused 报文），
    # 抢在前面报"已爆炸"会先闪一下再变成"拆弹成功"。
    local_bang_grace_sec: float = 0.6
    # 观战真值校正：小于这个偏差平滑吸收，大于就直接对齐
    snap_tolerance_sec: float = 0.5
    smooth_step_sec: float = 0.08
    # 结束文案停留时长（Q25：停 1.5 秒后消失）
    hide_after_end_sec: float = 1.5


@dataclass
class AudioConfig:
    """音频子系统（2026-09-17 重新接入）：听"安放播报语音"来拿真实安放时刻。

    **为什么需要它**：活着或死后看队友时，Valve 故意把 GSI 的"已安放"事件延迟
    0.03~2.6 秒（随机），而**安放播报语音不延迟**（它由客户端自己的游戏状态触发）。
    标定实测（2026-09-17，366 秒真实录音 / 3 次安放）：播报模板匹配真身得分
    0.83~0.86，其它播报最高 0.70；真身比 GSI 揭示包早 0.34 / 2.40 / 0.92 秒。
    详见 docs/精度方案总览.md 第五节。

    **门控（2026-09-19 修订）**：只看"回合是不是 live、GSI 有没有揭示过安放"，
    **阵营不再参与**。原来是"CT 方全流程启用、T 方全流程不用"，理由是"T 方自己下包
    本来就准"；用户实机复核发现那只覆盖了"自己下包"——**T 方队友下包时变的是队友的
    背包，你自己那一包数据没有任何变化**，锚点一样退到常数补偿、一样有 ±1 秒误差。
    所以现在两队都启用。
    这也正好对上错过的场景：CT 拆弹判断需要准的时刻，而自己下包（T）本来就准。

    **安全**：WASAPI 环回录音是被动采集（与 OBS 录音同类），不注入、不读内存、
    不碰游戏进程。检测不到就退回 GSI 估算，绝不因为音频失效而让面板变差。
    """

    enabled: bool = True
    # None = 默认播放设备；游戏在跑时优先用**进程环回**（只录 CS2 自己的声音）
    device_id: str | None = None
    buffer_ms: int | None = None
    gain: float = 1.0
    read_timeout_ms: int = 40

    # 模板目录（从用户自己的录音里标定出来，**不进版本库**）；None = runtime/audio_templates
    template_dir: str | None = None
    # 参与匹配的模板长度（秒）与**两个**阈值（2026-09-17 实战日志逼出来的拆分）：
    template_sec: float = 2.2
    # 候选阈值：检测器报到这个分数才算候选。**采信与否不在这里决定** ——
    # 用户 2026-09-17 拍板："确定不是误报再显示倒计时，从多少秒开始计时都行" ⇒
    # 候选要等 GSI 揭示包确认后才采用（没有确认就什么都不显示，误报无害）。
    threshold: float = 0.70
    # 指纹路径的**命中数阈值**（"最高时间偏移箱里的命中哈希数"）。
    # 16 的来历（2026-09-18）：真身 30~42、背景噪声 ≤6，阈值取 16 时实机四局全部命中；
    # 之所以不取更低，是因为当时把"中文播报文件得 13 分"误判成跨语言虚假碰撞 ——
    # 后来用户说明自己打的一直是中文局，那两个文件其实是**同一句的弱命中**，
    # 所以真实背景上限比 13 更低、这个阈值偏保守（代价是偶尔漏检）。
    # ⚠️ 正在用日志里"距安放 ≥10 秒的窗口"重新标定（见 docs/实机反馈与待办.md 第二十四节）；
    # 调整只需改这一个数，**不用重建参考指纹**。
    min_hits: int = 16
    # 音频链路偏置（秒）：**加**在"播报时刻"上，>0 = 我们的归零更晚。
    # 为什么需要它：参考指纹里的 onset 只记"裁剪起点 → 播报起点"这个**几何事实**（0.40 秒），
    # 而"播报起点 → 真实安放"还含采集管线的延迟，那是**产品侧**的常数、与参考无关。
    # 标定史：0.30（2026-09-17 用户对照拆弹时限"偏早约 0.3 秒" ⇒ 后移）+
    #         0.20（2026-09-18 实机"普遍偏早 0.2 秒"，且同局 CALIB 下界稳定在 −0.17~−0.24）
    #         = 0.50。
    # 放在配置里而不是重建参考：以后调时间只改这一个数，不用重跑 build_fingerprints.py。
    onset_bias_sec: float = 0.50


@dataclass
class UiConfig:
    enabled: bool = True
    # 面板上的两行字都用它：数值行含中文标签（"炸弹时限 37"），
    # 用等宽字体（Consolas）会让中文掉进回退字体、两种字形混在一行里。
    font_family: str = "Microsoft YaHei UI"
    # 只剩调试角标在用等宽字体
    mono_font: str = "Consolas"
    # 数值行字号（pt；实际 pt = round(值 × scale)，最小 8）
    font_size: int = 16
    # 状态行字号（pt）
    sub_font_size: int = 12
    scale: float = 1.0
    # --- 面板样式（对齐游戏自己的通知框：深灰底 + 左右红竖条 + 白字）---
    # ⚠️ 底色必须离 COLOR_KEY(#010203) 足够远，否则那块会被当透明抠掉。
    panel_color: str = "#2E2E36"
    panel_bar_color: str = "#FF0000"
    panel_bar_width: int = 4
    # None = 还没拖过，用默认位置（水平居中、纵向 82% 处；见 hud.DEFAULT_Y_RATIO）
    pos_x: int | None = None
    pos_y: int | None = None
    # Q25：默认不常驻；打开只是为了观察程序是否在跑
    show_idle: bool = False
    # 数字精度：剩余时间大于这个值时显示整数（**向上取整** = 游戏「还剩第几秒」），
    # 否则显示一位小数。
    # 15 秒之前整数（一眼能对比游戏自己的计时器），最后 15 秒给小数（能看出亚秒）。
    decimal_below_sec: float = 15.0


@dataclass
class DebugConfig:
    print_events: bool = True
    print_gsi: bool = False
    log_path: str | None = None
    # 关掉落盘日志（自检时用，免得污染 runtime/c4timer.log）
    no_log: bool = False


@dataclass
class Config:
    cs2: Cs2Config = field(default_factory=Cs2Config)
    gsi: GsiConfig = field(default_factory=GsiConfig)
    timers: TimersConfig = field(default_factory=TimersConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)


def _apply(obj: Any, data: dict) -> None:
    """把 dict 里的已知字段写进 dataclass，未知键忽略、类型尽量转换。"""
    if not is_dataclass(obj) or not isinstance(data, dict):
        return
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            continue
        cur = getattr(obj, key)
        if is_dataclass(cur):
            _apply(cur, value)
            continue
        if value is None and cur is not None and not _allows_none(known[key]):
            continue
        try:
            if cur is not None and not isinstance(value, type(cur)) and not (
                isinstance(cur, bool) or isinstance(cur, int) or isinstance(cur, float)
            ):
                continue
            if isinstance(cur, bool):
                setattr(obj, key, bool(value))
            elif isinstance(cur, int) and not isinstance(cur, bool):
                setattr(obj, key, int(value))
            elif isinstance(cur, float):
                setattr(obj, key, float(value))
            else:
                setattr(obj, key, value)
        except (TypeError, ValueError):
            continue


def _allows_none(f) -> bool:
    return "None" in str(f.type)


def load(path: Path | str | None = None) -> Config:
    global LAST_LOAD_ERROR
    cfg = Config()
    p = Path(path) if path else DEFAULT_PATH
    LAST_LOAD_ERROR = None
    if p.exists():
        try:
            # 用 utf-8-sig 读：记事本和不少编辑器会写 BOM，而带 BOM 的 JSON
            # 会让 json.loads 直接抛错。以前这个错被静默吞掉，结果是
            # **整份 config.json 被忽略、程序照默认值跑**，用户却以为配置生效了。
            _apply(cfg, json.loads(p.read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError) as exc:
            LAST_LOAD_ERROR = f"{p}: {exc!r}"
    return cfg


def save(cfg: Config, path: Path | str | None = None) -> Path:
    p = Path(path) if path else DEFAULT_PATH
    p.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return p


def set_value(cfg: Config, dotted: str, raw: str) -> None:
    """`timers.bomb_seconds=40` 这种命令行赋值。"""
    if "." not in dotted:
        raise ValueError(f"需要 section.field 形式: {dotted!r}")
    section, name = dotted.split(".", 1)
    target = getattr(cfg, section, None)
    if target is None or not is_dataclass(target):
        raise ValueError(f"没有配置段: {section!r}")
    if not hasattr(target, name):
        raise ValueError(f"{section} 没有字段 {name!r}")
    cur = getattr(target, name)
    if isinstance(cur, bool):
        value: Any = raw.strip().lower() in ("1", "true", "yes", "on", "是")
    elif isinstance(cur, int):
        value = int(float(raw))
    elif isinstance(cur, float):
        value = float(raw)
    elif raw.strip().lower() in ("none", "null", ""):
        value = None
    else:
        value = raw
    setattr(target, name, value)
