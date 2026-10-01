"""判据双向验证的元测试（pytest）。

把历史上的**四次同类自伤**做成可执行的判据登记 ——
每条判据都带「正确样本」与「错误样本」，两者都满足才算有效。

运行：
    pytest tests/test_assertion_meta.py -v

═══════════════════════════════════════════════════════════════════════════
 这为什么是元测试
═══════════════════════════════════════════════════════════════════════════
普通测试验证**被测对象**；本文件验证**判据本身**。

四次自伤的共同结构：判据选错了，但看起来像被测对象有 bug。
若没有这一层，正确的修复可能被判据误报为「坏的」而被回滚（事故 1 就是这样）。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.criterion import REGISTRY, clear, criterion, verify_all  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# 事故 1：T36 自伤 —— 裸子串匹配命中了注释里引用的同一串文字
# ═══════════════════════════════════════════════════════════════════════════
@criterion(
    "T36 审计位于 if outcome.passed 的代码行之前",
    valid=("\n            dep_audit = self._audit_dependencies(requirement)\n"
           "\n"
           "            if outcome.passed:\n"),
    invalid=("\n            if outcome.passed:\n"
             "                dep_audit = self._audit_dependencies(requirement)\n"),
    note="必须按**代码行**判定位置，不能按裸子串 —— 初版用 tail.find('if outcome.passed:') "
         "命中了注释里引用的同一串文字，修复已生效却报红",
    tags=("historical", "t36"),
)
def probe_audit_before_gate(text: str) -> bool:
    marker = "\n            if outcome.passed:\n"
    idx_if = text.find(marker)
    if idx_if == -1:
        return False
    return "dep_audit = self._audit_dependencies(" in text[:idx_if]


# ═══════════════════════════════════════════════════════════════════════════
# 事故 2：fixture 转义过度 —— 夹具把换行写成字面量，整段变一行
# ═══════════════════════════════════════════════════════════════════════════
@criterion(
    "夹具必须含真实换行，且调用独占一行",
    valid="const { foo } = require('./up');\nconst p = 1;\nfoo(p, p);\n",
    invalid="const { foo } = require('./up');\\nconst p = 1;\\nfoo(p, p);\\n",  # 字面反斜杠+n
    note="夹具错误会造成「被测对象有 bug」的假象 —— 整段变一行后，"
         "import 与调用同行，`_bindings_in_line` 会排除同行内容",
    tags=("historical", "fixture"),
)
def probe_fixture_well_formed(src: str) -> bool:
    lines = src.splitlines()
    if len(lines) < 3:
        return False
    return "foo(p, p);" in lines and "require" in lines[0] and "foo(p, p);" not in lines[0]


# ═══════════════════════════════════════════════════════════════════════════
# 事故 3：元断言 detail 误导 —— 无论通过与否都打印失败语
# ═══════════════════════════════════════════════════════════════════════════
_FAILURE_WORDS = ("未找到", "不存在", "失败", "错误", "missing")


@criterion(
    "元断言的 detail 文字必须中性（不含失败语）",
    valid="独立行 = 'foo(p, p);'",
    invalid="未找到独立行 'foo(p, p);'",
    note="detail 无论通过与否都会打印，写成失败语会让**通过**看起来像**失败**",
    tags=("historical", "meta-output"),
)
def probe_detail_is_neutral(detail: str) -> bool:
    return not any(w in detail for w in _FAILURE_WORDS)


# ═══════════════════════════════════════════════════════════════════════════
# 事故 4：提示词校验用整串相等，忽略了子节点本就会多出依赖行
# ═══════════════════════════════════════════════════════════════════════════
@criterion(
    "判断「上限提示只作用于根节点」应按片段存在性，而非整串相等",
    valid={"root_off": "描述: d", "child_on": "描述: d\n依赖: R1", "mark": "不超过"},
    invalid={"root_off": "描述: d", "child_on": "描述: d\n依赖: R1", "mark": ""},
    note="初版用 `child_brief == root_brief_off` 判断，但子节点本来就多一行「依赖: R1」，"
         "于是判据报红 —— 与上限提示无关",
    tags=("historical", "prompt-check"),
)
def probe_limit_only_affects_root(case: dict) -> bool:
    # 判据：非根节点的提示里**不含**上限标记
    return bool(case["mark"]) and case["mark"] not in case["child_on"]


# ═══════════════════════════════════════════════════════════════════════════
# 非历史：当前真实判据也纳入双向验证
# ═══════════════════════════════════════════════════════════════════════════
@criterion(
    "CONTRACT_MISMATCH：声明 4 参、实际 3 参应判违约",
    valid={"declared": 4, "actual": 3},
    invalid={"declared": 4, "actual": 4},
    note="签名校验按**参数个数**比对",
    tags=("contract",),
)
def probe_contract_arity(case: dict) -> bool:
    return case["declared"] != case["actual"]


@criterion(
    "根节点判定：无依赖为根，有依赖非根",
    valid=[],
    invalid=["REQ-1"],
    note="空依赖列表 -> 根节点",
    tags=("limit",),
)
def probe_is_root(deps: list) -> bool:
    return not deps


@criterion(
    "冻结合同必须 frozen=true",
    valid={"frozen": True},
    invalid={"frozen": False},
    note="未冻结的合同不得在实现阶段使用",
    tags=("contract-freeze",),
)
def probe_frozen_flag(payload: dict) -> bool:
    return payload.get("frozen") is True


# ═══════════════════════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════════════════════


def test_all_registered_criteria_are_bidirectionally_valid() -> None:
    """每条判据都要通过双向验证：正确样本过 / 错误样本败。"""
    results = verify_all()
    assert results, "判据注册表为空 —— 元测试没有覆盖任何判据"
    bad = [(n, why) for n, ok, why in results if not ok]
    assert not bad, "以下判据未通过双向验证：\n" + "\n".join(f"  - {n}: {w}" for n, w in bad)


def test_registry_covers_all_historical_incidents() -> None:
    """四次历史自伤都必须在注册表里留下可执行判据。"""
    tags = {t for c in REGISTRY for t in c.tags}
    assert "historical" in tags
    historical = [c for c in REGISTRY if "historical" in c.tags]
    assert len(historical) >= 4, (
        f"历史自伤判据不足 4 条（实际 {len(historical)}）—— "
        "第 19 章记录的四次事故应各有可执行判据"
    )


def test_framework_detects_a_broken_criterion() -> None:
    """★ 框架自身的双向验证：一个**坏判据**必须被本框架判为无效。

    这是对验证器的验证 —— 否则框架可能恒真，永远报「全部通过」。
    """
    clear()
    try:
        # 恒真判据：两个方向都过 -> 漏检
        @criterion("恒真（坏）", valid="a", invalid="b")
        def always_true(_: str) -> bool:
            return True

        # 恒假判据：两个方向都败 -> 噪音
        @criterion("恒假（坏）", valid="a", invalid="b")
        def always_false(_: str) -> bool:
            return False

        # 反向判据：正确样本败、错误样本过 -> 完全反了
        @criterion("反向（坏）", valid="x", invalid="y")
        def inverted(s: str) -> bool:
            return s == "y"

        results = {n: (ok, why) for n, ok, why in verify_all()}
        assert len(results) == 3, "三条坏判据都应被验证"
        assert not any(ok for ok, _ in results.values()), (
            "坏判据被误判为有效 —— 框架本身失效"
        )
        # ★ 注意：`in` 对 dict 是**查键**，不是子串匹配 ——
        # 初版写 `"恒真" in results`，而键是 `"恒真（坏）"`，于是判据报红。
        # 这恰好是**第五次同类自伤**（判据选错），且发生在本元测试自己身上：
        # 为「判据正确性」写的测试，自己用错了判据。
        def why_of(fragment: str) -> str:
            for key, (_, why) in results.items():
                if fragment in key:
                    return why
            raise AssertionError(f"注册表里找不到含 {fragment!r} 的判据：{sorted(results)}")

        assert "漏检" in why_of("恒真"), f"恒真判据应报漏检，实际: {why_of('恒真')}"
        assert "恒假" in why_of("恒假"), "恒假判据应报恒假"
        # 反向判据：正确样本未通过（误报）
        assert "误报" in why_of("反向"), f"反向判据应报误报，实际: {why_of('反向')}"
    finally:
        clear()


def test_framework_accepts_a_good_criterion() -> None:
    """一个正确构造的判据必须被判为有效（反向验证，防框架恒假）。"""
    clear()
    try:
        @criterion("正确（好）", valid="3", invalid="4")
        def is_three(s: str) -> bool:
            return s == "3"

        results = verify_all()
        assert len(results) == 1
        name, ok, why = results[0]
        assert ok, f"合法判据被判为无效: {why}"
    finally:
        clear()


@pytest.fixture(autouse=True)
def _restore_registry():
    """保留模块级注册的判据（防止 clear() 影响其它用例）。"""
    snapshot = list(REGISTRY)
    yield
    REGISTRY[:] = snapshot
