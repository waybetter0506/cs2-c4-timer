"""自研断言计数器：五个自检模块共用一个（从前 5 个模块各抄了一份，150 行重复）。

为什么不用 pytest：这个项目的自检是**可交付的运行时报导体**（`python -m c4timer test`
给用户看"多少项通过"），而且要在没有 pytest 的机器上跑（用户装完 Python + numpy 就要能用）。
"""

from __future__ import annotations


class Checks:
    """累计通过/失败数；失败时立刻打印，方便定位。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures: list[str] = []

    def ok(self, cond: bool, label: str) -> bool:
        if cond:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(label)
            print(f"  [X] {label}")
        return bool(cond)

    def eq(self, got, want, label: str) -> bool:
        return self.ok(got == want, f"{label} —— 期望 {want!r}，实际 {got!r}")

    def ge(self, got, floor, label: str) -> bool:
        return self.ok(got >= floor, f"{label} —— 期望 >= {floor}，实际 {got!r}")

    def near(self, got, want, tol: float, label: str) -> bool:
        good = got is not None and abs(got - want) <= tol
        return self.ok(good, f"{label} —— 期望 {want:.3f}±{tol}，实际 {got!r}")
