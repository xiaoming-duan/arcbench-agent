"""UI_TEST_WEAK 判据修复（red_failed 设计缺口）的验证用例。

运行：pytest tests/test_ui_test_weak.py -v

═══════════════════════════════════════════════════════════════════════════
 缺口是什么
═══════════════════════════════════════════════════════════════════════════
UI_TEST_WEAK **从未触发过**。原因不是漏配，而是**语义错位**：

    UI_TEST_WEAK 的语义 = 「E2E 测试存在，但没真正验证 UI」
    它的判据却是        = 「E2E 测试在 RED 阶段是否失败」

而「RED 阶段是否失败」在写测试时**根本观测不到**（调用点只能传
`red_failed=None`），于是 `if red_failed is False` 永远为假 —— 这道门是死的。

判据改为**源码本身可判定的事实**：
    「测试是否真的断言了 UI 元素的存在与状态」

这与 WEAK_TEST 对 `type=e2e` 的规则**共用同一套判据**
（`factory.uigate.has_meaningful_ui_assertions`，全仓库只此一份实现）。
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.adapter import _adapt  # noqa: E402
from factory.uigate import (  # noqa: E402
    UI_TEST_WEAK,
    check_ui,
    has_meaningful_ui_assertions,
)

UI_SPEC = ROOT / "requirements_probe_ui" / "requirements.yaml"


@pytest.fixture()
def ui_req():
    reqs = _adapt(yaml.safe_load(UI_SPEC.read_text(encoding="utf-8")),
                  source=UI_SPEC).requirements
    return [r for r in reqs if r.req_id == "REQ-1-1-1"][0]


def _good_source(req) -> str:
    """一份真的断言了每个 UI 元素的 E2E 源码。"""
    lines = ["import { test, expect } from '@playwright/test';",
             "test('x', async ({ page }) => {"]
    for c in req.ui_contracts:
        for e in c.elements:
            lines.append(f"  await expect(page.getByTestId('{e.element_id}')).toBeVisible();")
            for _k, m in e.error_messages:
                lines.append(f'  await expect(page.getByText("{m}")).toBeVisible();')
    lines.append("});")
    return "\n".join(lines)


HOLLOW = (
    "import { test } from '@playwright/test';\n"
    "test('x', async ({ page }) => {\n"
    "  await page.goto('/');\n"
    "  await page.getByRole('link').click();\n"
    "});\n"
)

VACUOUS = (
    "import { test, expect } from '@playwright/test';\n"
    "test('x', async () => { expect(true).toBe(true); });\n"
)


def _weak(src: str):
    return has_meaningful_ui_assertions(src)


# ---------------------------------------------------------------------------
# 判据本身
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("label", "src", "expect"),
    [
        ("真实元素断言", "await expect(page.getByLabel('Email')).toBeVisible();", True),
        ("角色+状态", "await expect(page.getByRole('button')).toBeEnabled();", True),
        ("URL 断言", "await expect(page).toHaveURL('/x');", True),
        ("文本断言", "await expect(page.getByTestId('t')).toHaveText('Hi');", True),
        ("只跑流程", "await page.goto('/'); await page.click('a');", False),
        ("空断言", "expect(true).toBe(true);", False),
        ("无断言", "const x = 1;", False),
    ],
)
def test_criterion_table(label, src, expect):
    got, why = _weak(src)
    assert got is expect, f"{label}: got={got} why={why}"


def test_aliased_assert_library_counts():
    """`const a = require('node:assert'); a.equal(...)` 是**别名调用**，也要认出来。

    否则一份合法测试会被判成「没有任何断言」-> 无谓重写。
    """
    got, why = _weak("const t=require('node:test'),a=require('node:assert');\na.equal(1,1);")
    assert got is True, why


# ---------------------------------------------------------------------------
# UI_TEST_WEAK 真的会触发（修复前从不触发）
# ---------------------------------------------------------------------------

def test_hollow_e2e_triggers_ui_test_weak(ui_req):
    """★核心回归：E2E 只跑流程不断言 -> UI_TEST_WEAK 必须触发。"""
    c = check_ui(ui_req, e2e_sources=[("x", HOLLOW)], planned_e2e=True)
    assert not c.ok
    assert any(v.verdict == UI_TEST_WEAK for v in c.violations), \
        f"UI_TEST_WEAK 未触发（这正是修复前的状态）: {[v.verdict for v in c.violations]}"


def test_vacuous_e2e_triggers_ui_test_weak(ui_req):
    """空断言同样测不出东西 -> 触发。"""
    c = check_ui(ui_req, e2e_sources=[("x", VACUOUS)], planned_e2e=True)
    assert any(v.verdict == UI_TEST_WEAK for v in c.violations)


def test_good_e2e_does_not_trigger_weak(ui_req):
    """反向：真实全覆盖的 E2E 不得被判弱（否则就是无谓重写的来源）。"""
    c = check_ui(ui_req, e2e_sources=[("x", _good_source(ui_req))], planned_e2e=True)
    assert not any(v.verdict == UI_TEST_WEAK for v in c.violations), \
        f"真实断言被判弱: {[v.verdict for v in c.violations]}"
    assert c.ok, f"全覆盖却仍有违规: {[v.verdict for v in c.violations]}"


# ---------------------------------------------------------------------------
# 语义修正的回归守卫
# ---------------------------------------------------------------------------

def test_red_failed_parameter_removed():
    """判据不再依赖 RED 阶段；若有人把它加回来，这道门会重新变成死的。"""
    assert "red_failed" not in inspect.signature(check_ui).parameters


def test_no_red_failed_usage_in_criterion():
    """判据的**可执行代码**里不得再出现 red_failed（防止换个写法复活）。

    用 AST 而不是字符串搜索：文档/注释里必须能自由地解释「为什么删掉它」，
    字符串搜索会把说明文字也一起判红。
    """
    import ast

    from factory import uigate
    tree = ast.parse(inspect.getsource(uigate))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "red_failed" not in used, "uigate 的可执行代码里仍引用 red_failed"


def test_criterion_has_single_definition():
    """★两个门禁必须共用**同一份**判据实现，不许各写一套。

    历史坑：DYNAMIC_IMPORT 那次两处各写 `verdict != X`，静默分叉 ——
    一个放行、一个阻断，且不报错。
    """
    defs = []
    for py in (ROOT / "factory").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if re.search(r"^def has_meaningful_ui_assertions", text, re.MULTILINE):
            defs.append(py.name)
    assert defs == ["uigate.py"], f"判据定义处不唯一: {defs}"

    # testaudit（WEAK_TEST 对 e2e）必须**引用**它，而不是自己实现
    from factory import testaudit
    assert "has_meaningful_ui_assertions" in inspect.getsource(testaudit), \
        "WEAK_TEST 的 e2e 判据没有复用共用实现"
