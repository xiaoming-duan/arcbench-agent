"""UI 探针（含 error_messages）的验证用例。

运行：pytest tests/test_ui_probe.py -v

═══════════════════════════════════════════════════════════════════════════
 为什么补这个探针
═══════════════════════════════════════════════════════════════════════════
第五道 UI 门禁有四个判定，但 `UI_ERROR_MESSAGE_MISMATCH` **没有真实触发对象**：
`requirements_probe_ui` 的 12 条元素声明里，`error_messages` 全是空的
（只有契约级的自由文本 `error_display`，那不是结构化字段，门禁读不到）。

于是这条判定从未被真实规格驱动过 —— 只在合成用例里验证过。

本探针给它补上**具体、逐字可断言**的错误消息，使
「声明 → 提示词注入 → 测试断言 → 门禁判定」这条闭环有真实对象。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.adapter import _adapt  # noqa: E402
from factory.generator import LLMGenerator  # noqa: E402
from factory.uigate import (  # noqa: E402
    UI_ERROR_MESSAGE_MISMATCH,
    check_ui,
)

UI_SPEC = ROOT / "requirements_probe_ui" / "requirements.yaml"
REQ_ID = "REQ-1-1-1"


class _C:
    model = "x"

    def is_available(self):  # noqa: ANN201
        return True


@pytest.fixture()
def ui_req():
    reqs = _adapt(yaml.safe_load(UI_SPEC.read_text(encoding="utf-8")),
                  source=UI_SPEC).requirements
    return [r for r in reqs if r.req_id == REQ_ID][0]


def _all_messages(req) -> list[str]:
    out: list[str] = []
    for c in req.ui_contracts:
        for e in c.elements:
            for _key, msg in e.error_messages:
                if str(msg).strip():
                    out.append(str(msg))
    return out


# ---------------------------------------------------------------------------
# 探针确实声明了 error_messages
# ---------------------------------------------------------------------------

def test_probe_declares_error_messages(ui_req):
    """★核心：探针里必须**非空**的 error_messages（否则门禁无对象可判）。"""
    msgs = _all_messages(ui_req)
    assert msgs, "UI 探针没有声明任何 error_messages —— UI_ERROR_MESSAGE_MISMATCH 仍无触发对象"
    assert len(msgs) >= 2, f"错误消息太少，覆盖不足: {msgs}"


def test_error_messages_are_concrete_and_assertable(ui_req):
    """消息文本必须**具体、逐字可断言**，不能是占位符或空串。

    门禁 `message_mentioned()` 是**逐字**比对：`<某值>` 这类占位符
    在测试里无法被字面写出，等于把判定变成永不触发。
    """
    for c in ui_req.ui_contracts:
        for e in c.elements:
            for key, msg in e.error_messages:
                assert str(key).strip(), f"{e.element_id} 的场景名为空"
                text = str(msg).strip()
                assert text, f"{e.element_id} 的错误消息为空"
                assert "<" not in text and ">" not in text, \
                    f"{e.element_id} 的错误消息含占位符，无法逐字断言: {text!r}"


def test_error_messages_attached_to_real_elements(ui_req):
    """错误消息必须挂在**声明过的元素**上（我们用的是真实元素，不是新造壳）。"""
    declared = {e.element_id for c in ui_req.ui_contracts for e in c.elements}
    carriers = {
        e.element_id
        for c in ui_req.ui_contracts
        for e in c.elements
        if e.error_messages
    }
    assert carriers, "没有任何元素带 error_messages"
    assert carriers <= declared, f"错误消息挂在不存在的元素上: {carriers - declared}"


def test_probe_has_matching_scenario(ui_req):
    """声明了错误消息就该有对应场景 —— 否则规格自相矛盾。"""
    def _text(step):  # 步骤在模型里是 dict（keyword/content），兼容对象写法
        if isinstance(step, dict):
            return str(step.get("content") or "")
        return str(getattr(step, "content", "") or "")

    scn = " ".join(_text(step) for s in ui_req.scenarios for step in s.steps)
    for msg in _all_messages(ui_req):
        assert msg in scn, f"错误消息 {msg!r} 在场景里无对应描述"


# ---------------------------------------------------------------------------
# 门禁：可触发 + 可满足（双向）
# ---------------------------------------------------------------------------

def _covered_source(req, include_messages: bool) -> str:
    lines = ["import { test, expect } from '@playwright/test';",
             "test('probe', async ({ page }) => {"]
    for c in req.ui_contracts:
        for e in c.elements:
            lines.append(f"  await expect(page.getByTestId('{e.element_id}')).toBeVisible();")
            if include_messages:
                for _k, m in e.error_messages:
                    lines.append(f'  await expect(page.getByText("{m}")).toBeVisible();')
    lines.append("});")
    return "\n".join(lines)


def test_error_message_mismatch_is_triggerable(ui_req):
    """★错误样本：元素全覆盖但**不写**错误消息断言 -> 必须报 UI_ERROR_MESSAGE_MISMATCH。"""
    c = check_ui(ui_req, e2e_sources=[("x", _covered_source(ui_req, False))],
                 planned_e2e=True)
    hits = [v for v in c.violations if v.verdict == UI_ERROR_MESSAGE_MISMATCH]
    assert hits, f"门禁未触发，报的是 {[v.verdict for v in c.violations]}"
    for msg in _all_messages(ui_req):
        assert msg in hits[0].detail, f"违规详情没点名 {msg!r}"


def test_error_message_coverage_passes(ui_req):
    """正确样本：逐字断言了错误消息 -> 不再报该判定（证明不是恒真）。"""
    c = check_ui(ui_req, e2e_sources=[("x", _covered_source(ui_req, True))],
                 planned_e2e=True)
    assert not any(v.verdict == UI_ERROR_MESSAGE_MISMATCH for v in c.violations)
    assert c.ok, f"全覆盖却仍报违规: {[v.verdict for v in c.violations]}"


def test_message_coverage_map_records_each_message(ui_req):
    """覆盖映射要逐个消息记录，便于平台侧查看缺了哪条。"""
    c = check_ui(ui_req, e2e_sources=[("x", _covered_source(ui_req, True))],
                 planned_e2e=True)
    for msg in _all_messages(ui_req):
        assert c.message_coverage.get(msg) is True, f"{msg!r} 未被记为已覆盖"


# ---------------------------------------------------------------------------
# 提示词注入：模型必须拿到逐字文本才可能写对
# ---------------------------------------------------------------------------

def test_prompt_injects_error_messages(ui_req):
    brief = LLMGenerator(_C(), "vitest")._requirement_brief(ui_req)
    for msg in _all_messages(ui_req):
        assert msg in brief, f"提示词未注入错误消息 {msg!r} —— 模型无从逐字断言"
