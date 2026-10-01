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
    # ★ 裸信号名 `ENVIRONMENT` 也要登记 —— 它是 RETRY_BUDGET 的键，
    #   漏登记会让它掉进「未知信号 -> implementation」的兜底，
    #   于是环境预算（1）被当成实现预算（3）。
    "ENVIRONMENT": CLASS_ENVIRONMENT,
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


#: 分类型错误重试预算（P0-2）。按**错误信号**分配，每类独立计数。
#:
#: 与四类兜底（DEFAULT_BUDGETS）的分工：
#:   • 表内列出的信号 —— 用**该信号自己的**预算（更细，如 CONTRACT_MISSING 只给 1）
#:   • 表外的信号     —— 回退到它所属**类别**的预算（不会没预算可用）
#:
#: 数字语义是「允许的重试次数」，**不含首次**。
RETRY_BUDGET: dict[str, int] = {
    "TEST_FAILED": 3,
    "DEPENDENCY_NOT_USED": 3,
    "CONTRACT_MISMATCH": 2,
    "CONTRACT_MISSING": 1,
    "CONTRACT_TAMPERED": 1,
    "TEST_BROKEN": 2,
    "WEAK_TEST": 2,
    "ENVIRONMENT": 1,
}


@dataclass
class RepairBudget:
    """按**错误类型**独立计数的重试账本（P0-2）。

    每类**独立**累加，互不挤占 —— 这正是 P0-2 的核心：
    环境错误重试 1 次，不会让实现少一次机会。

    计数键是**信号**（如 `TEST_FAILED` / `CONTRACT_MISSING`），
    不是粗粒度的类别 —— 因为「合同缺失」（模型改了也没用，只给 1 次）
    与「实现没写对」（给 3 次）需要不同预算。
    """

    #: 信号级预算（来自 config.RETRY_BUDGET）
    signal_budgets: dict[str, int] = field(default_factory=lambda: dict(RETRY_BUDGET))
    #: 类别级兜底预算（表外信号用）
    class_budgets: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BUDGETS))
    #: 已用次数，**按信号**计数
    spent: dict[str, int] = field(default_factory=dict)
    #: 按发生顺序记录 (类别, 信号)，供报告与诊断
    history: list[tuple[str, str]] = field(default_factory=list)

    # 兼容旧构造：RepairBudget(budgets={CLASS_ENVIRONMENT: 1})
    def __post_init__(self) -> None:
        legacy = getattr(self, "budgets", None)          # type: ignore[attr-defined]
        if isinstance(legacy, dict):
            # 旧口径给的是**类别**预算；并入 class_budgets，并让信号级让位
            self.class_budgets.update({k: int(v) for k, v in legacy.items()})
            self.signal_budgets.clear()
            object.__setattr__(self, "budgets", None) if False else None

    def budget_for(self, signal: str) -> int:
        """取某信号的预算：先查信号表，再回退到类别表。"""
        key = (signal or "").strip().upper()
        if key in self.signal_budgets:
            return int(self.signal_budgets[key])
        return int(self.class_budgets.get(classify(signal), 1))

    # 旧名保留（测试与报告用）
    def budget(self, key: str) -> int:
        return self.budget_for(key)

    def charge(self, signal: str, detail: str = "") -> tuple[str, bool]:
        """记一次失败。返回 (类别, 是否还能再试)。

        「还能再试」= 该**信号**已用次数 **<=** 其预算。

        预算语义是**允许的重试次数**（不含首次），所以是 `<=` 而不是 `<`。
        初版写成 `<`，于是 `budget=1` 的环境错误在**首次失败就判死** ——
        「重试 1 次」变成了「重试 0 次」，被现有断言 T35a 抓出。
        """
        kind = classify(signal, detail)
        key = (signal or "").strip().upper() or kind.upper()
        self.spent[key] = self.spent.get(key, 0) + 1
        self.history.append((kind, key))
        return kind, self.spent[key] <= self.budget_for(key)

    def left(self, key: str) -> int:
        """该信号**还能再试几次**。"""
        return max(0, self.budget_for(key) - self.spent.get((key or "").upper(), 0))

    def exhausted(self, key: str) -> bool:
        """是否已用尽（已用次数 **>** 预算，与 charge 的 `<=` 对称）。"""
        return self.spent.get((key or "").upper(), 0) > self.budget_for(key)

    def spent_in_class(self, kind: str) -> int:
        """某**类别**下的总花费（跨该类所有信号）。

        按 history 逐条归类累加 —— history 里每条只记一次发生，
        所以不受「同一信号重复出现被合并」的影响。
        """
        return sum(1 for _cls, _sig in self.history if _cls == kind)

    def to_dict(self) -> dict[str, object]:
        return {
            "signal_budgets": dict(self.signal_budgets),
            "class_budgets": dict(self.class_budgets),
            "spent": dict(self.spent),
            "left": {s: self.left(s) for s in sorted(set(self.spent) | set(self.signal_budgets))},
            "history": [f"{k}:{s}" for k, s in self.history],
            "exhausted": sorted({s for s in self.spent if self.exhausted(s)}),
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
    """一行摘要，供报告与日志。

    只列**实际发生过**的信号 —— 把 8 个 `=0/N` 也打出来会淹没日志里真正有用的信息
    （初版就是这样，空账本会输出一整行零）。
    """
    parts = [f"{k}={b.spent[k]}/{b.budget_for(k)}" for k in sorted(b.spent)]
    return " ".join(parts) if parts else "(未发生重试)"


def signals_of(b: RepairBudget, kind: str) -> list[str]:
    return [s for k, s in b.history if k == kind]


def joined_signals(history: Iterable[tuple[str, str]]) -> str:
    return "; ".join(f"{k}:{s}" for k, s in history)
