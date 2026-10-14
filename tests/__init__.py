"""c4timer 自检套件（不属于产品包，发布时不需要）。

五个模块，各自暴露 `run(verbose=True) -> (通过数, 失败数, 失败标签列表)`：

| 模块 | 覆盖面 |
|---|---|
| `test_gsi.py` | P0：210 个真实 GSI 报文的解析回归（夹具 `fixtures/gsi_capture_20260915.jsonl`） |
| `test_state.py` | P1：状态机（安放锚点/补偿/标定/音频修正/爆炸判定） |
| `test_render.py` | P1：显示层（文案、颜色、尺寸规则） |
| `test_e2e.py` | P3：真实报文走完整链路 |
| `test_app.py` | P3：应用层门控与接线 |

跑法（唯一入口，与从前完全一致）：

    python -m c4timer test

为什么放在顶层 `tests/` 而不是 `c4timer/` 里：这些文件用 pytest 的命名约定，但实现是
**自研断言框架**（全仓没有 `def test_*`，也不依赖 pytest）—— 放在产品包里会让 pytest
收集到 0 个测试、也模糊了"产品代码"的边界。搬家后产品包只含运行时代码。
"""
