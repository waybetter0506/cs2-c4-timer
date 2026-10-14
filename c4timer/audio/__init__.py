"""音频子系统：WASAPI 回环采集 + 安放播报指纹识别。

三个职责，各占各的模块，互不越界：

* **采集**：`capture.py`（线程封装）→ `loopback.py`（WASAPI 回环，系统混音/进程两种模式）
  → `combase.py`（COM/WASAPI 的 ctypes 绑定）
* **分析**：`plant.py`（安放播报检测器：分块喂入 → 命中时刻）
  → `fingerprint.py`（Shazam 式指纹：时频峰值星座 + 成对哈希 + 时间偏移分箱）
  → `detect.py` / `events.py` / `wav_io.py`（频带特征、数据类型、WAV 读写）
* **参考**：`load_templates()` 读 `runtime/audio_templates/*.npz`。参考指纹由用户本地标定
  生成（`tools/build_fingerprints.py`），**不进版本库** —— 游戏音频不随仓库分发。

来源与授权：`combase.py` / `loopback.py` / `detect.py` / `wav_io.py` 原样搬自本项目 2 代
（另一个仓库），那边已通过 141 项断言 + 6 个端到端场景。唯一改动是把
`from .state import Detection, Event` 改成本包的 `.events`（避免与 c4timer 的状态机模块重名）。

明确不搬 1 代的 `csc4/sounds.py`：它有已知 bug —— `max_duration`(2.5s) 从未用于切段，
交火时会把 171 秒的枪声整段当成一个 explode 事件。

2026-09-18 清理：删掉 `templates.py` / `boom.py` / `spectral.py`（共 756 行）。
它们是 2026-09-16"音频整条链路已删"时期的遗留，全仓无人 import；
需要取回就从 git 历史里找（删除它的那次提交的父提交仍带着这三个文件）。
"""

# 全链路唯一的采样率定义：`detect.py` / `plant.py` / `fingerprint.py` 都从这里取。
# 从前四处各写一个 48000，改一处忘一处会让采样率换算静默出错（时间偏几百毫秒都看不出来）。
ANALYSIS_RATE = 48_000
