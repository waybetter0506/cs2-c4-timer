"""音频事件类型与检测结果。

从 2 代 `cs2timer/state.py` 里抽出来的：那边 `Event`/`Detection` 和 C4 状态机混在
同一个文件里，而本项目的状态机是 `c4timer/state.py`（结构完全不同），
所以把这两个纯数据类型单独放这里，`detect.py` 只依赖它们。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Event(str, Enum):
    """能被听出来的 C4 相关音效。"""

    PLANT = "plant"                    # 安放完成
    DEFUSE_START = "defuse_start"      # 开始拆弹
    DEFUSE_SUCCESS = "defuse_success"  # 拆弹成功
    EXPLODE = "explode"                # 爆炸
    ROUND_END = "round_end"            # 回合结束


@dataclass
class Detection:
    """一次被检出的音效；`time` 已回填到真实起振时刻。"""

    event: Event
    time: float
    confidence: float = 1.0
    # 相似度连续维持超过阈值的时长（秒）。拆弹声是 1.25 秒持续音，
    # C4 滴声是周期性短音 —— 实测这个时长比"峰值多高"更有判别力。
    sustain: float = 0.0


# 与 c4timer.state 的事件常量对齐（那边是字符串常量，这边是枚举）
STATE_KIND = {
    Event.PLANT: "plant",
    Event.DEFUSE_START: "defuse_start",
    Event.DEFUSE_SUCCESS: "defuse_success",
    Event.EXPLODE: "explode",
    Event.ROUND_END: "round_end",
}
