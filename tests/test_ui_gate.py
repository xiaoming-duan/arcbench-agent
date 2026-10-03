"""UI 门禁（P0-3，第五道门）的验证用例。

运行：pytest tests/test_ui_gate.py -v

按 19.2.7/19.2.8 纪律：每条判据都给出**正确样本**与**错误样本**，
且构造型夹具带前提断言。
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
from factory.testplan import RequirementTestPlan, TestFileSpec  # noqa: E402
from factory.uigate import (  # noqa: E402
    UI_ELEMENT_MISSING,
    UI_ERROR_MESSAGE_MISMATCH,
    UI_TEST_MISSING,
    UI_TEST_WEAK,
    check_ui,
    describe,
    e2e_sources_of,
    element_mentioned,
)

UI_SPEC = ROOT / "requirements_probe_ui" / "requirements.yaml"
CLOSURE6 = ROOT / "requirements_probe_closure6" / "requirements.yaml"


def _req(path, rid=None):
    reqs = _adapt(yaml.safe_load(path.read_text(encoding="utf-8")), source=path).requirements
    return [r for r in reqs if rid is None or r.req_id == rid][0]


@pytest.fixture()
def ui_req():
    return _req(UI_SPEC, "REQ-1-1-1")


def _good_source(req, skip: tuple[str, ...] = ()) -> str:
    """一份**真实写法**的全覆盖 E2E 源码。`skip` 里的元素 id 完全不生成。"""
    lines = ["import { test, expect } from '@playwright/test';",
             "test('req-1-1-1', async ({ page }) => {"]
    for c in req.ui_contracts:
        for e in c.elements:
            if e.element_id in skip:
                continue
            if "<" in e.label:
                lines.append(f"  await expect(page.getByTestId('{e.element_id}')).toBeVisible();")
            else:
                lines.append(f"  await expect(page.getByLabel('{e.label}')).toBeVisible();")
            for _k, m in e.error_messages:
                lines.append(f"  await expect(page.getByText(\"{m}\")).toBeVisible();")
    lines.append("});")
    return "\n".join(lines)


# ---- 正确样本（证明不误报）----

def test_full_coverage_passes(ui_req):
    c = check_ui(ui_req, e2e_sources=[("x", _good_source(ui_req))], planned_e2e=True)
    assert c.ok, f"全覆盖却报违规: {[v.verdict for v in c.violations]}"
    assert all(c.element_coverage.values())
    # 覆盖映射按 **element_id** 建键：`last-updated` 在首页与编辑器**都**出现，
    # 故 12 条元素声明 -> 11 个唯一 id。（初版断言 12，是我的测试写错了。）
    declared = sum(len(x.elements) for x in ui_req.ui_contracts)
    assert declared == 12
    assert len(c.element_coverage) == 11


def test_undeclared_ui_is_not_checked():
    """反向：未声明 ui_contracts 的需求不做任何检查（可选语义）。"""
    c = check_ui(_req(CLOSURE6, "REQ-1"), e2e_sources=[], planned_e2e=False)
    assert c.ok and c.reason == "NO_UI_CONTRACT_DECLARED"


# ---- 错误样本（证明能拦住）----

def test_missing_e2e_plan_blocks(ui_req):
    c = check_ui(ui_req, e2e_sources=[], planned_e2e=False)
    assert not c.ok and c.violations[0].verdict == UI_TEST_MISSING


def test_planned_but_file_absent_blocks(ui_req):
    c = check_ui(ui_req, e2e_sources=[], planned_e2e=True)
    assert not c.ok and c.violations[0].verdict == UI_TEST_MISSING


def test_weak_test_blocks(ui_req):
    """E2E 存在但**没有真实 UI 断言** -> 测不出东西 -> UI_TEST_WEAK。

    判据已从「E2E 在 RED 阶段是否失败」改为「是否真的断言了 UI 元素的存在/状态」。
    原判据的问题：RED 是否失败**在写测试时无法观测**，调用点只能传
    `red_failed=None`，于是这道门从未触发过（语义错位）。
    """
    hollow = (
        "import { test } from '@playwright/test';\n"
        "test('req-1-1-1', async ({ page }) => {\n"
        "  await page.goto('/');\n"
        "  await page.getByRole('link').click();\n"
        "});\n"
    )
    # ★ 前提断言（19.2.8）：这份样本里确实一句断言都没有
    assert "expect" not in hollow and "assert" not in hollow
    c = check_ui(ui_req, e2e_sources=[("x", hollow)], planned_e2e=True)
    assert not c.ok
    assert any(v.verdict == UI_TEST_WEAK for v in c.violations)
    # 反向：同一需求换成**真有断言**的源码，就不该再报 WEAK
    good = check_ui(ui_req, e2e_sources=[("x", _good_source(ui_req))], planned_e2e=True)
    assert not any(v.verdict == UI_TEST_WEAK for v in good.violations)


def test_vacuous_assertion_is_weak(ui_req):
    """空断言（expect(true).toBe(true)）不算「断言了 UI」—— 应用坏掉也照样通过。"""
    vacuous = (
        "import { test, expect } from '@playwright/test';\n"
        "test('req-1-1-1', async () => { expect(true).toBe(true); });\n"
    )
    c = check_ui(ui_req, e2e_sources=[("x", vacuous)], planned_e2e=True)
    assert any(v.verdict == UI_TEST_WEAK for v in c.violations), \
        f"空断言未被判弱: {[v.verdict for v in c.violations]}"


def test_red_failed_parameter_is_gone():
    """语义修正的回归守卫：判据不再依赖 RED 阶段。

    若有人把 `red_failed` 加回来，说明判据又回到「拿不到的事实」上，
    这道门会重新变成永不触发。用签名断言钉死。
    """
    import inspect
    assert "red_failed" not in inspect.signature(check_ui).parameters


def test_missing_element_blocks(ui_req):
    """把某个元素的**引用**去掉（只改引用，不改 label 文本）。

    初版直接 `.replace("grid", "g__one")`，前提断言立刻报红 ——
    因为 "grid" 也出现在 `getByLabel('Grid')` 里，整词替换改不干净。
    **这正是 19.2.8「修改型夹具必须证明修改生效」在起作用。**
    """
    full = _good_source(ui_req)
    src = _good_source(ui_req, skip=("grid",))
    # ★ 前提断言：目标元素的两条引用路径（id 与 label）都确实不在源码里。
    #   初版只替换了 testid，却留着 `getByLabel('Grid')` ——
    #   元素**仍被 label 覆盖**，断言前提不成立（19.2.8 的意义所在）。
    assert src != full
    assert "getbytestid('grid')" not in src.lower()
    assert "getbylabel('grid')" not in src.lower()
    c = check_ui(ui_req, e2e_sources=[("x", src)], planned_e2e=True)
    assert any(v.verdict == UI_ELEMENT_MISSING for v in c.violations)
    assert "grid" in c.violations[0].detail


def test_missing_error_message_blocks(tmp_path):
    """构造一个**含 error_messages** 的 UI 契约（工作簿需求没有错误消息）。"""
    spec = {"requirements": [{"id": "R", "ui_contracts": [{
        "page": "/register", "elements": [
            {"id": "username", "type": "text", "label": "Username",
             "error_messages": {"format": "Username format is invalid"}}]}]}]}
    req = _adapt(spec, source=Path("x")).requirements[0]
    src = "await expect(page.getByLabel('Username')).toBeVisible();"
    c = check_ui(req, e2e_sources=[("x", src)], planned_e2e=True)
    assert any(v.verdict == UI_ERROR_MESSAGE_MISMATCH for v in c.violations)
    # 正确样本：把消息文本写进断言后应通过
    ok = check_ui(req, e2e_sources=[("x", src + "\nawait expect(e).toHaveText('Username format is invalid');")],
                  planned_e2e=True)
    assert ok.ok, f"补齐消息后仍报违规: {[v.verdict for v in ok.violations]}"


# ---- matcher 的三级判定（防误报）----

@pytest.mark.parametrize("src,eid,label,etype,expect", [
    ("page.getByTestId('last-updated')", "last-updated", "", "", True),
    ("page.getByLabel('Last updated:')", "last-updated", "Last updated:", "", True),
    ("page.getByLabel('lastUpdated')", "last-updated", "", "", True),
    ("page.getByLabel('last_updated')", "last-updated", "", "", True),
    ("page.getByRole('link', { name: x })", "workbook-link", "<workbook name>", "link", True),
    ("page.getByRole('button', { name: 'X' })", "workbook-link", "<workbook name>", "link", False),
    ("nothing relevant", "workbook-link", "<workbook name>", "link", False),
])
def test_element_mentioned_matrix(src, eid, label, etype, expect):
    assert element_mentioned(src.lower(), eid, label, etype) is expect


def test_placeholder_label_does_not_require_literal(ui_req):
    """★ 占位符型 label（`<workbook name>`）不得要求字面匹配 —— 误报会让人关掉门禁。"""
    link = [e for e in ui_req.ui_contracts[0].elements if e.element_id == "workbook-link"][0]
    assert link.label == "<workbook name>"
    assert element_mentioned("page.getbyrole('link', { name: wb })", link.element_id,
                             link.label, link.type)


# ---- e2e_sources_of + 可执行理由 ----

def test_e2e_sources_of_reads_only_declared(tmp_path):
    plan = RequirementTestPlan(req_id="R", test_files=(
        TestFileSpec(path="backend/tests/a.test.js", type="unit"),
        TestFileSpec(path="backend/test-e2e/b.spec.js", type="e2e"),
    ))
    d = tmp_path / "backend" / "test-e2e"
    d.mkdir(parents=True)
    (d / "b.spec.js").write_text("content", encoding="utf-8")
    # ★ 前提断言：确实只应有 1 个 e2e 文件
    src, planned = e2e_sources_of(tmp_path, plan)
    assert planned and len(src) == 1 and src[0][0].endswith("b.spec.js")


def test_e2e_sources_of_skips_empty_file(tmp_path):
    plan = RequirementTestPlan(req_id="R", test_files=(
        TestFileSpec(path="backend/test-e2e/empty.spec.js", type="e2e"),))
    d = tmp_path / "backend" / "test-e2e"
    d.mkdir(parents=True)
    (d / "empty.spec.js").write_text("", encoding="utf-8")
    src, planned = e2e_sources_of(tmp_path, plan)
    assert planned and src == []


def test_reason_is_executable(ui_req):
    c = check_ui(ui_req, e2e_sources=[], planned_e2e=False)
    text = describe([c])
    assert "修正指令" in text
    assert "getByLabel" in text and "逐字" in text
