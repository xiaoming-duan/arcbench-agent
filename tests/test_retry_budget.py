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
    b = RepairBudget(budgets={CLASS_ENVIRONMENT: 1})
    _, can1 = b.charge("ENV_ERROR")
    assert can1 is True, "budget=1 的首次失败应仍可再试一次"
    _, can2 = b.charge("ENV_ERROR")
    assert can2 is False, "第二次失败后应耗尽"
    assert b.exhausted(CLASS_ENVIRONMENT)
    assert b.left(CLASS_ENVIRONMENT) == 0


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
    b.charge("ENV_ERROR")
    b.charge("ENV_ERROR")
    assert b.spent[CLASS_ENVIRONMENT] == 2
    assert b.spent[CLASS_IMPLEMENTATION] == 0, "环境错误不应计入实现"
    assert b.left(CLASS_IMPLEMENTATION) == DEFAULT_BUDGETS[CLASS_IMPLEMENTATION]

    # 反向：实现错误也不挤占环境预算
    b2 = RepairBudget()
    b2.charge("TEST_FAILED")
    assert b2.spent[CLASS_ENVIRONMENT] == 0


def test_each_class_counts_independently():
    """四类各自独立计数，互不影响。"""
    b = RepairBudget()
    for sig in ("DESIGN_FAILED", "TEST_FAILED", "TEST_BROKEN", "ENV_ERROR"):
        b.charge(sig)
    assert b.spent == {CLASS_DESIGN: 1, CLASS_IMPLEMENTATION: 1,
                       CLASS_TEST: 1, CLASS_ENVIRONMENT: 1}
    assert set(b.spent) == set(ALL_CLASSES)


def test_history_preserves_order_and_exhaustion():
    """历史按发生顺序保留；耗尽列表正确。"""
    b = RepairBudget(budgets={CLASS_TEST: 1})
    b.charge("TEST_FAILED")
    b.charge("TEST_BROKEN")
    b.charge("TEST_BROKEN")
    assert [s for _, s in b.history] == ["TEST_FAILED", "TEST_BROKEN", "TEST_BROKEN"]
    d = b.to_dict()
    assert d["spent"][CLASS_TEST] == 2
    assert CLASS_TEST in d["exhausted"]
    assert CLASS_IMPLEMENTATION not in d["exhausted"]


def test_tighter_budget_stops_sooner_than_looser():
    """对照：同一串失败下，预算小的一类更早耗尽（判据有区分度）。"""
    def run(budget: int) -> int:
        b = RepairBudget(budgets={CLASS_ENVIRONMENT: budget})
        n = 0
        for _ in range(10):
            _, more = b.charge("ENV_ERROR")
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
    """账本摘要可读（供报告/日志）。"""
    b = RepairBudget()
    b.charge("TEST_FAILED")
    b.charge("ENV_ERROR")
    text = describe_budget(b)
    assert "implementation=1/3" in text
    assert "environment=1/1" in text
