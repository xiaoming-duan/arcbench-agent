"""错误分类与**分类重试预算**。

═══════════════════════════════════════════════════════════════════════════
 为什么要分类（P0-2）
═══════════════════════════════════════════════════════════════════════════
此前实现循环只有一个统一预算：

    while attempts <= self.config.max_repairs:   # 四类错误共用一本账

后果（实测）：
  • **环境错误吃掉实现预算** —— 网关瞬时故障重试 2 次，实现只写了 1 次
  • 一次 DNS 失败（`[Errno -3] name resolution`）让 REQ-11 连设计都没做完，
    整轮判失败 —— 而那与模型能力无关
  • 「测试自身坏了」（TEST_BROKEN）与「实现没写对」（TEST_FAILED）
    消耗同一份额度，于是两类问题互相挤占

Governing Autonomous AI Agents 把「无界修复-破坏重试循环」列为
传统软件开发中不存在的失败模式之一；TDAD 的研究也显示
**仅加流程约束而不给足上下文，回归率反而上升**（6.08% -> 9.94%）。
结论一致：重试必须**按错误性质分配**，而不是一视同仁地数次数。

═══════════════════════════════════════════════════════════════════════════
 四类与预算
═══════════════════════════════════════════════════════════════════════════
    设计错误    design          2 次   —— 契约/设计不成立，重试收益中等
    实现错误    implementation  3 次   —— 最常见，给足额度
    测试错误    test            2 次   —— 测试自身坏了，重写即可
    环境错误    environment     1 次   —— **不该浪费预算**：重试 1 次仍不行就终止并报环境失败

关键：**环境错误独立计数**，不占用实现预算。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# ---- 类别 ----
CLASS_DESIGN = "design"
CLASS_IMPLEMENTATION = "implementation"
CLASS_TEST = "test"
CLASS_ENVIRONMENT = "environment"

ALL_CLASSES = (CLASS_DESIGN, CLASS_IMPLEMENTATION, CLASS_TEST, CLASS_ENVIRONMENT)

#: 各类默认预算（次数 = **允许的重试次数**，不含首次）。
DEFAULT_BUDGETS: dict[str, int] = {
    CLASS_DESIGN: 2,
    CLASS_IMPLEMENTATION: 3,
    CLASS_TEST: 2,
    CLASS_ENVIRONMENT: 1,
}

# ---- 信号 -> 类别 ----
# 信号取自循环里已有的判定名，便于就地接入而不必改判据本身。
_SIGNAL_TO_CLASS: dict[str, str] = {
    # 测试自身的问题：改实现修不好，只能重写测试
    "TEST_BROKEN": CLASS_TEST,
    "INVALID_TEST_SYNTAX": CLASS_TEST,
    "WEAK_TEST": CLASS_TEST,
    "TEST_FILE_DRIFT": CLASS_TEST,
    # 测试是好的，实现没过
    "TEST_FAILED": CLASS_IMPLEMENTATION,
    "IMPLEMENTATION_REGRESSION": CLASS_IMPLEMENTATION,
    # 设计/契约层面不成立
    "DESIGN_FAILED": CLASS_DESIGN,
    "CONTRACT_MISSING": CLASS_DESIGN,
    "CONTRACT_TAMPERED": CLASS_DESIGN,
    "CONTRACT_MISMATCH": CLASS_DESIGN,
    "DEPENDENCY_NOT_USED": CLASS_DESIGN,
    "UNVERIFIED_DEPENDENCY": CLASS_DESIGN,
    # 环境
    "ENV_ERROR": CLASS_ENVIRONMENT,
    "MODEL_ERROR": CLASS_ENVIRONMENT,
    "DNS_ERROR": CLASS_ENVIRONMENT,
    "QUOTA_EXHAUSTED": CLASS_ENVIRONMENT,
    "GATEWAY_ERROR": CLASS_ENVIRONMENT,
}

# 文本兜底：信号没登记时，靠关键字判断
_ENV_MARKERS = (
    "name resolution", "temporary failure", "connection reset",
    "connection timed out", "timed out", "timeout", "remote disconnected",
    "quota", "insufficient_quota", "429", "503", "502", "504",
    "proxy_error", "network", "unreachable", "ssl",
)


def classify(signal: str, detail: str = "") -> str:
    """把一个失败信号映射到四类之一。

    未知信号保守归入 **implementation** —— 它有最大预算（3），
    宁可多给一次机会，也不要把可能是真问题的失败当成环境问题直接终止。
    """
    key = (signal or "").strip().upper()
    if key in _SIGNAL_TO_CLASS:
        return _SIGNAL_TO_CLASS[key]
    text = f"{signal} {detail}".lower()
    if any(m in text for m in _ENV_MARKERS):
        return CLASS_ENVIRONMENT
    if "test_broken" in text or "weak_test" in text:
        return CLASS_TEST
    if "design" in text:
        return CLASS_DESIGN
    return CLASS_IMPLEMENTATION


def is_environment(signal: str, detail: str = "") -> bool:
    return classify(signal, detail) == CLASS_ENVIRONMENT


# ---- 预算账本 ----


@dataclass
class RepairBudget:
    """按类别独立计数的重试账本。

    每类**独立**累加，互不挤占 —— 这正是 P0-2 的核心：
    环境错误重试 1 次，不会让实现少一次机会。
    """

    budgets: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BUDGETS))
    spent: dict[str, int] = field(default_factory=lambda: {k: 0 for k in ALL_CLASSES})
    #: 按发生顺序记录 (类别, 信号)，供报告与诊断
    history: list[tuple[str, str]] = field(default_factory=list)

    def budget(self, kind: str) -> int:
        return int(self.budgets.get(kind, DEFAULT_BUDGETS.get(kind, 1)))

    def charge(self, signal: str, detail: str = "") -> tuple[str, bool]:
        """记一次失败。返回 (类别, 是否还能再试)。

        「还能再试」= 该类已用次数 **<=** 预算。

        预算语义是**允许的重试次数**（不含首次），所以是 `<=` 而不是 `<`。
        初版写成 `<`，于是 `budget=1` 的环境错误在**首次失败就判死** ——
        「重试 1 次」变成了「重试 0 次」，被现有断言 T35a 抓出。
        """
        kind = classify(signal, detail)
        self.spent[kind] = self.spent.get(kind, 0) + 1
        self.history.append((kind, signal))
        return kind, self.spent[kind] <= self.budget(kind)

    def left(self, kind: str) -> int:
        """该类**还能再试几次**。"""
        return max(0, self.budget(kind) - self.spent.get(kind, 0))

    def exhausted(self, kind: str) -> bool:
        """是否已用尽（已用次数 **>** 预算，与 charge 的 `<=` 对称）。"""
        return self.spent.get(kind, 0) > self.budget(kind)

    def to_dict(self) -> dict[str, object]:
        return {
            "budgets": dict(self.budgets),
            "spent": dict(self.spent),
            "left": {k: self.left(k) for k in ALL_CLASSES},
            "history": [f"{k}:{s}" for k, s in self.history],
            "exhausted": [k for k in ALL_CLASSES if self.exhausted(k)],
        }


def budgets_from_config(config: object) -> dict[str, int]:
    """从 FactoryConfig 读取四类预算（缺失的用默认值）。"""
    out = dict(DEFAULT_BUDGETS)
    for kind in ALL_CLASSES:
        raw = getattr(config, f"repair_budget_{kind}", None)
        if raw is not None:
            try:
                out[kind] = max(0, int(raw))
            except (TypeError, ValueError):
                pass
    return out


def describe_budget(b: RepairBudget) -> str:
    """一行摘要，供报告与日志。"""
    parts: list[str] = []
    for k in ALL_CLASSES:
        parts.append(f"{k}={b.spent.get(k, 0)}/{b.budget(k)}")
    return " ".join(parts)


def signals_of(b: RepairBudget, kind: str) -> list[str]:
    return [s for k, s in b.history if k == kind]


def joined_signals(history: Iterable[tuple[str, str]]) -> str:
    return "; ".join(f"{k}:{s}" for k, s in history)
