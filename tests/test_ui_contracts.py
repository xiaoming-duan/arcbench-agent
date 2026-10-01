"""UI 契约（P0-1）的验证用例。

运行：pytest tests/test_ui_contracts.py -v

按 19.2.7/19.2.8 纪律：每条判据都给出**正确样本**与**错误样本**；
修改型夹具带**前提断言**。
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

UI_SPEC = ROOT / "requirements_probe_ui" / "requirements.yaml"
CLOSURE6 = ROOT / "requirements_probe_closure6" / "requirements.yaml"


class _C:
    model = "x"

    def is_available(self):  # noqa: ANN201
        return True


def _reqs(path):
    return _adapt(yaml.safe_load(path.read_text(encoding="utf-8")),
                  source=path).requirements


@pytest.fixture()
def ui_req():
    return [r for r in _reqs(UI_SPEC) if r.req_id == "REQ-1-1-1"][0]


# ---- 解析 ----

def test_parses_ui_contracts(ui_req):
    assert len(ui_req.ui_contracts) == 2
    home, editor = ui_req.ui_contracts
    assert home.element_ids == ("workbook-list", "last-updated", "workbook-link")
    assert "grid" in editor.element_ids and "pivot-table" in editor.element_ids
    assert len(editor.invariants) == 5
    assert any("不得出现" in x for x in editor.invariants)


def test_element_fields_parsed(ui_req):
    editor = ui_req.ui_contracts[1]
    grid = [e for e in editor.elements if e.element_id == "grid"][0]
    assert grid.type == "grid"
    assert grid.label == "Grid"
    assert grid.validation
    link = [e for e in ui_req.ui_contracts[0].elements if e.element_id == "workbook-link"][0]
    assert link.type == "link"
    assert link.label == "<workbook name>"      # 占位符原样保留


def test_empty_when_undeclared():
    """反向：未声明 ui_contracts 的需求，字段为空 —— 行为不变。"""
    assert all(not r.ui_contracts for r in _reqs(CLOSURE6))


def test_malformed_entries_skipped_not_raised():
    """容错：形状不对的条目跳过而不是抛错（可选字段写坏了不该让整轮失败）。"""
    spec = {"requirements": [{"id": "R", "ui_contracts": [
        {"title": "no page"}, "not_a_dict", {"page": "/ok", "elements": "bad"},
    ]}]}
    reqs = _adapt(spec, source=Path("x")).requirements
    assert len(reqs[0].ui_contracts) == 1
    assert reqs[0].ui_contracts[0].page == "/ok"
    assert reqs[0].ui_contracts[0].elements == ()


def test_elements_as_mapping_supported():
    """`elements` 也允许写成映射（id -> 属性）。"""
    spec = {"requirements": [{"id": "R", "ui_contracts": [
        {"page": "/p", "elements": {"a": {"type": "text", "label": "A"}}},
    ]}]}
    c = _adapt(spec, source=Path("x")).requirements[0].ui_contracts[0]
    assert c.element_ids == ("a",) and c.elements[0].label == "A"


# ---- prompt 注入 ----

def test_prompt_injects_ui_contract(ui_req):
    b = LLMGenerator(_C(), "vitest")._requirement_brief(ui_req)
    assert "UI 契约" in b
    for c in ui_req.ui_contracts:
        assert c.page in b
        for e in c.elements:
            assert e.element_id in b, f"元素 {e.element_id} 未注入"
        for inv in c.invariants:
            assert inv in b, f"不变量未注入: {inv[:30]}"


def test_prompt_has_no_ui_section_when_undeclared():
    """反向：未声明时提示词里**完全不含** UI 段。"""
    req = _reqs(CLOSURE6)[0]
    b = LLMGenerator(_C(), "vitest")._requirement_brief(req)
    assert "UI 契约" not in b
    assert "Playwright E2E" not in b


def test_prompt_instructs_accessible_name_exactness(ui_req):
    """实现要求必须写清「可访问名/文本/校验/错误消息逐字一致」。"""
    b = LLMGenerator(_C(), "vitest")._requirement_brief(ui_req)
    assert "逐字一致" in b
    assert "不变量" in b
