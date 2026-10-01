"""错误分类与分类重试预算的验证用例（pytest）。

运行：
    pytest tests/test_retry_budget.py -v

═══════════════════════════════════════════════════════════════════════════
 验证什么
═══════════════════════════════════════════════════════════════════════════
P0-2 的核心主张只有一条，但很关键：

    **环境错误不该占用实现预算。**

在此之前实现循环只有一个统一预算（`while attempts <= max_repairs`），
于是一次 DNS 失败 / 网关瞬时故障会实质性地减少模型能得到的实现机会 ——
用一个不可控因素压低了对模型能力的评估。

本文件按**判据双向验证**（架构文档 19.2.7）编写：每条判据都同时给出
正确样本与错误样本。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.errors import (  # noqa: E402
    ALL_CLASSES,
    RETRY_BUDGET,
    CLASS_DESIGN,
    CLASS_ENVIRONMENT,
    CLASS_IMPLEMENTATION,
    CLASS_TEST,
    DEFAULT_BUDGETS,
    RepairBudget,
    budgets_from_config,
    classify,
    describe_budget,
    is_environment,
)


# ═══════════════════════════════════════════════════════════════════════════
# 分类器
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("signal,expected", [
    # 测试自身坏了
    ("TEST_BROKEN", CLASS_TEST),
    ("WEAK_TEST", CLASS_TEST),
    ("INVALID_TEST_SYNTAX", CLASS_TEST),
    # 测试是好的、实现没过
    ("TEST_FAILED", CLASS_IMPLEMENTATION),
    ("IMPLEMENTATION_REGRESSION", CLASS_IMPLEMENTATION),
    # 设计/契约层面
    ("DESIGN_FAILED", CLASS_DESIGN),
    ("CONTRACT_MISSING", CLASS_DESIGN),
    ("CONTRACT_TAMPERED", CLASS_DESIGN),
    ("CONTRACT_MISMATCH", CLASS_DESIGN),
    ("DEPENDENCY_NOT_USED", CLASS_DESIGN),
])
def test_signal_classification(signal, expected):
    """已登记的信号必须落到预期类别（正确样本）。"""
    assert classify(signal) == expected


@pytest.mark.parametrize("detail", [
    "网络错误: [Errno -3] Temporary failure in name resolution",
    "openai SDK 调用失败: Request timed out.",
    "模型配额/余额耗尽（HTTP 429）: insufficient_quota",
    "proxy_error: connection reset by peer",
    "[Errno 110] Connection timed out",
])
def test_environment_detected_from_text(detail):
    """环境类失败靠**文本兜底**识别 —— 未登记的信号也能判对环境（正确样本）。"""
    assert is_environment("", detail), f"应判环境: {detail}"
    assert classify("", detail) == CLASS_ENVIRONMENT


@pytest.mark.parametrize("signal,detail", [
    ("TEST_FAILED", "expected 3 to be 4"),
    ("TEST_BROKEN", "SyntaxError: Unexpected token"),
    ("DESIGN_FAILED", "接口未声明"),
])
def test_non_environment_not_misclassified(signal, detail):
    """**反向**：非环境错误不得被文本兜底误判为环境（否则会因预算 1 而提前放弃）。"""
    assert not is_environment(signal, detail), f"误判为环境: {signal} / {detail}"


def test_unknown_signal_defaults_to_implementation():
    """未知信号保守归 implementation（预算最大），不轻易当环境终止。"""
    assert classify("SOMETHING_NEW_XYZ") == CLASS_IMPLEMENTATION
    assert CLASS_IMPLEMENTATION == classify("", "")
    assert DEFAULT_BUDGETS[CLASS_IMPLEMENTATION] == max(DEFAULT_BUDGETS.values())


# ═══════════════════════════════════════════════════════════════════════════
# 预算语义
# ═══════════════════════════════════════════════════════════════════════════


def test_budget_semantics_is_retries_not_attempts():
    """预算是**允许的重试次数**（不含首次）：budget=1 应允许 1 次重试。

    初版写成 `spent < budget`，于是 budget=1 在首轮就判死 ——
    「重试 1 次」变成「重试 0 次」。该错误被 T35a 抓出。
    """
    b = RepairBudget()                       # ENVIRONMENT 在 RETRY_BUDGET 里就是 1
    _, can1 = b.charge("ENVIRONMENT")
    assert can1 is True, "budget=1 的首次失败应仍可再试一次"
    _, can2 = b.charge("ENVIRONMENT")
    assert can2 is False, "第二次失败后应耗尽"
    assert b.exhausted("ENVIRONMENT")
    assert b.left("ENVIRONMENT") == 0


def test_retry_budget_map_matches_spec():
    """config.RETRY_BUDGET 的八个信号与预算值必须与设计一致（逐项钉住）。"""
    from factory.config import FactoryConfig
    assert FactoryConfig.RETRY_BUDGET == RETRY_BUDGET
    assert RETRY_BUDGET == {
        "TEST_FAILED": 3,
        "DEPENDENCY_NOT_USED": 3,
        "CONTRACT_MISMATCH": 2,
        "CONTRACT_MISSING": 1,
        "CONTRACT_TAMPERED": 1,
        "TEST_BROKEN": 2,
        "WEAK_TEST": 2,
        "ENVIRONMENT": 1,
    }


def test_signal_budget_overrides_class_budget():
    """信号级预算优先于类别兜底 —— 这是「分类型」而非「分四类」的关键。"""
    b = RepairBudget()
    # CONTRACT_MISSING 与 DEPENDENCY_NOT_USED 同属 design 类，但预算不同
    assert classify("CONTRACT_MISSING") == classify("DEPENDENCY_NOT_USED") == CLASS_DESIGN
    assert b.budget_for("CONTRACT_MISSING") == 1
    assert b.budget_for("DEPENDENCY_NOT_USED") == 3
    # 表外信号回退到类别预算
    assert b.budget_for("SOME_UNLISTED_DESIGN_SIGNAL") == DEFAULT_BUDGETS[CLASS_DESIGN]


def test_contract_signals_have_tight_budgets():
    """★ 合同类信号预算最紧（1–2）：合同问题**改实现修不好**，多给轮次是浪费。"""
    for sig in ("CONTRACT_MISSING", "CONTRACT_TAMPERED"):
        assert RETRY_BUDGET[sig] == 1, f"{sig} 应只给 1 次"
    assert RETRY_BUDGET["CONTRACT_MISMATCH"] == 2
    # 反向：实现类信号预算最宽
    assert RETRY_BUDGET["TEST_FAILED"] == max(RETRY_BUDGET.values())


def test_default_budgets_match_spec():
    """四类默认预算与设计一致。"""
    assert DEFAULT_BUDGETS == {
        CLASS_DESIGN: 2,
        CLASS_IMPLEMENTATION: 3,
        CLASS_TEST: 2,
        CLASS_ENVIRONMENT: 1,
    }


def test_environment_does_not_consume_implementation_budget():
    """★ P0-2 的核心主张：环境错误**不挤占**实现预算。"""
    b = RepairBudget()
    b.charge("ENVIRONMENT")
    b.charge("ENVIRONMENT")
    assert b.spent.get("ENVIRONMENT") == 2
    assert b.spent.get("TEST_FAILED", 0) == 0, "环境错误不应计入实现"
    assert b.left("TEST_FAILED") == RETRY_BUDGET["TEST_FAILED"]

    # 反向：实现错误也不挤占环境预算
    b2 = RepairBudget()
    b2.charge("TEST_FAILED")
    assert b2.spent.get("ENVIRONMENT", 0) == 0
    assert b2.left("ENVIRONMENT") == RETRY_BUDGET["ENVIRONMENT"]


def test_each_signal_counts_independently():
    """每个**信号**各自独立计数，互不影响（P0-2 的计费键是信号）。"""
    b = RepairBudget()
    for sig in ("CONTRACT_MISSING", "TEST_FAILED", "TEST_BROKEN", "ENVIRONMENT"):
        b.charge(sig)
    assert b.spent == {"CONTRACT_MISSING": 1, "TEST_FAILED": 1,
                       "TEST_BROKEN": 1, "ENVIRONMENT": 1}
    # 反向：同一类别下的两个信号**各自**计数，不合并
    b2 = RepairBudget()
    b2.charge("CONTRACT_MISSING")
    b2.charge("CONTRACT_MISMATCH")
    assert b2.spent == {"CONTRACT_MISSING": 1, "CONTRACT_MISMATCH": 1}
    assert b2.spent_in_class(CLASS_DESIGN) == 2


def test_history_preserves_order_and_exhaustion():
    """历史按发生顺序保留；耗尽列表正确。"""
    b = RepairBudget()
    b.charge("TEST_FAILED")
    b.charge("TEST_BROKEN")
    b.charge("TEST_BROKEN")
    assert [s for _, s in b.history] == ["TEST_FAILED", "TEST_BROKEN", "TEST_BROKEN"]
    d = b.to_dict()
    assert d["spent"]["TEST_BROKEN"] == 2
    # 预算 2 = 允许 2 次重试。用了 2 次**仍可再试一次**，故此时尚未耗尽。
    assert "TEST_BROKEN" not in d["exhausted"]
    b.charge("TEST_BROKEN")                       # 第 3 次 -> 超出预算 2
    assert "TEST_BROKEN" in b.to_dict()["exhausted"]
    assert "CONTRACT_MISSING" not in b.to_dict()["exhausted"]


def test_tighter_budget_stops_sooner_than_looser():
    """对照：同一串失败下，预算小的一类更早耗尽（判据有区分度）。"""
    def run(budget: int) -> int:
        b = RepairBudget(signal_budgets={"X": budget}, class_budgets={})
        n = 0
        for _ in range(10):
            _, more = b.charge("X")
            n += 1
            if not more:
                break
        return n

    assert run(0) == 1, "预算 0 = 不许重试（只承受首次失败）"
    assert run(1) == 2
    assert run(3) == 4
    assert run(0) < run(1) < run(3)


# ═══════════════════════════════════════════════════════════════════════════
# 与配置的对接
# ═══════════════════════════════════════════════════════════════════════════


def test_budgets_from_config_reads_four_fields():
    """从 config 读取四类预算。"""
    class _C:
        repair_budget_design = 5
        repair_budget_implementation = 6
        repair_budget_test = 7
        repair_budget_environment = 8

    got = budgets_from_config(_C())
    assert got == {CLASS_DESIGN: 5, CLASS_IMPLEMENTATION: 6,
                   CLASS_TEST: 7, CLASS_ENVIRONMENT: 8}


def test_budgets_from_config_falls_back_when_missing():
    """config 缺字段时用默认值（不炸）。"""
    class _Bare:
        pass

    assert budgets_from_config(_Bare()) == DEFAULT_BUDGETS


def test_budgets_from_config_rejects_bad_values():
    """非法值回退到默认，而不是抛错或产生负预算。"""
    class _Bad:
        repair_budget_design = "not_a_number"
        repair_budget_implementation = -3
        repair_budget_test = None
        repair_budget_environment = 2

    got = budgets_from_config(_Bad())
    assert got[CLASS_DESIGN] == DEFAULT_BUDGETS[CLASS_DESIGN]
    assert got[CLASS_IMPLEMENTATION] == 0, "负值应被归一（max(0,...)），不是原样保留"
    assert got[CLASS_TEST] == DEFAULT_BUDGETS[CLASS_TEST]
    assert got[CLASS_ENVIRONMENT] == 2


def test_factory_config_exposes_four_budgets():
    """FactoryConfig 必须暴露四个预算字段（否则 budgets_from_config 静默用默认）。"""
    from factory.config import FactoryConfig
    cfg = FactoryConfig()
    for kind in ALL_CLASSES:
        assert hasattr(cfg, f"repair_budget_{kind}"), f"缺字段 repair_budget_{kind}"
    assert cfg.repair_budget_environment == 1
    assert cfg.repair_budget_implementation == 3


def test_describe_budget_is_readable():
    """账本摘要可读（供报告/日志）——按**信号**列出。"""
    b = RepairBudget()
    b.charge("TEST_FAILED")
    b.charge("ENVIRONMENT")
    text = describe_budget(b)
    assert "TEST_FAILED=1/3" in text
    assert "ENVIRONMENT=1/1" in text
    assert describe_budget(RepairBudget()) == "(未发生重试)"
