"""WEAK_TEST 按 **type** 分规则（P0 修复）的验证用例。

运行：pytest tests/test_weak_test_classification.py -v

═══════════════════════════════════════════════════════════════════════════
 修的是什么
═══════════════════════════════════════════════════════════════════════════
WEAK_TEST 的原判据是「测试有没有 import 实现」。这条判据对
**unit / integration 成立**，对 **e2e 不成立**：Playwright 测试驱动的是浏览器，
**本来就不会 import 后端实现**。用 unit 判据去判它 → 每次都误报 →
每次都触发无谓重写 → 白烧 token。

分规则后：
    type: unit / integration -> 保留原判据（是否 import 实现）
    type: e2e                -> 不要求 import 实现，
                                改判「是否真的断言了 UI 元素的存在与状态」
                                （与 UI 门禁 UI_TEST_WEAK **共用同一判据**）

同时修掉两个使它更严重的缺陷：
  ① `looks_like_alias` 只看首字符 `@`，把 `@playwright/test` 这类
     **scoped npm 包**误判成路径别名 -> ALIAS_UNRESOLVED -> 弱。
  ② `declared_types` 的键是**设计产物**的 file_path（backend 相对），
     而查找键是 output 相对 —— 键对不上 -> types 为空 -> e2e 拿不到豁免。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.loop import TddLoop  # noqa: E402
from factory.testaudit import (  # noqa: E402
    V_E2E,
    V_E2E_NO_ASSERTION,
    V_NO_IMPORT,
    audit_imports,
    looks_like_alias,
)
from factory.testplan import RequirementTestPlan, TestFileSpec  # noqa: E402

E2E_REL = "backend/test-e2e/login.spec.js"
UNIT_REL = "backend/tests/login.test.js"

#: 有真实 UI 断言的 Playwright 测试
PLAYWRIGHT_GOOD = (
    "import { test, expect } from '@playwright/test';\n"
    "test('login', async ({ page }) => {\n"
    "  await page.goto('/login');\n"
    "  await expect(page.getByLabel('Email')).toBeVisible();\n"
    "  await expect(page.getByRole('button', { name: 'Sign in' })).toBeEnabled();\n"
    "});\n"
)

#: 只跑流程、一句断言都没有的 Playwright 测试
PLAYWRIGHT_HOLLOW = (
    "import { test } from '@playwright/test';\n"
    "test('login', async ({ page }) => {\n"
    "  await page.goto('/login');\n"
    "  await page.getByRole('button').click();\n"
    "});\n"
)

UNIT_GOOD = (
    "const t=require('node:test'),a=require('node:assert');\n"
    "const {login}=require('../src/auth');\n"
    "t('login',()=>{a.equal(login('a','b'),true)});\n"
)

UNIT_NO_IMPORT = (
    "const t=require('node:test'),a=require('node:assert');\n"
    "t('login',()=>{a.equal(1,1)});\n"
)


@pytest.fixture()
def ws(tmp_path):
    (tmp_path / "backend/src").mkdir(parents=True)
    (tmp_path / "backend/tests").mkdir(parents=True)
    (tmp_path / "backend/test-e2e").mkdir(parents=True)
    (tmp_path / "backend/src/auth.js").write_text("module.exports={login:()=>true};\n",
                                                 encoding="utf-8")
    return tmp_path


def _write(ws: Path, rel: str, src: str) -> None:
    (ws / rel).write_text(src, encoding="utf-8")


def _verdict(ws: Path, rel: str, declared: dict) -> str:
    audit = audit_imports(ws, [rel], implementation_root="backend/src",
                          declared_types=declared, aliases={"@/": "backend/src/"})
    return audit.files[0].verdict


# ---------------------------------------------------------------------------
# ① scoped npm 包不得被当作路径别名
# ---------------------------------------------------------------------------

def test_scoped_package_is_not_an_alias():
    """`@playwright/test` 是外部包，不是别名 —— 这是 E2E 被判弱的直接原因之一。"""
    assert looks_like_alias("@playwright/test") is False
    assert looks_like_alias("@testing-library/react") is False
    assert looks_like_alias("@vitest/expect") is False


def test_real_aliases_still_detected():
    """反向：真别名还要认得出来，否则修过头会把真问题放行。"""
    assert looks_like_alias("@/components/Button") is True
    assert looks_like_alias("~/utils/x") is True
    # 配置过前缀的具名别名也算
    assert looks_like_alias("@app/x", {"@app/": "backend/src/"}) is True
    # 但未配置时不算（避免把任意 scoped 包当别名）
    assert looks_like_alias("@app/x") is False
    # 普通外部包
    assert looks_like_alias("express") is False


# ---------------------------------------------------------------------------
# ② e2e 不要求 import 实现，改判「是否真的断言了 UI」
# ---------------------------------------------------------------------------

def test_e2e_with_real_assertions_is_not_weak(ws):
    """★核心回归：e2e 不 import 实现 + 有真实 UI 断言 -> **不判弱**。

    修复前这条必被判 WEAK_TEST（ALIAS_UNRESOLVED / NO_IMPLEMENTATION_IMPORT），
    于是每次都触发无谓重写。
    """
    _write(ws, E2E_REL, PLAYWRIGHT_GOOD)
    verdict = _verdict(ws, E2E_REL, {E2E_REL: ("e2e",)})
    assert verdict == V_E2E, f"e2e 有真实断言却判成 {verdict}"


def test_e2e_without_assertions_is_weak(ws):
    """反向：e2e 只跑流程、不断言 -> 测不出东西 -> 判弱（新判据抓得住的）。"""
    _write(ws, E2E_REL, PLAYWRIGHT_HOLLOW)
    verdict = _verdict(ws, E2E_REL, {E2E_REL: ("e2e",)})
    assert verdict == V_E2E_NO_ASSERTION, f"空转 e2e 未被判弱，verdict={verdict}"


def test_unit_still_requires_implementation_import(ws):
    """unit 规则不变：不 import 实现 -> 弱（不能因为改 e2e 把 unit 一起放行）。"""
    _write(ws, UNIT_REL, UNIT_NO_IMPORT)
    assert _verdict(ws, UNIT_REL, {UNIT_REL: ("unit",)}) == V_NO_IMPORT


def test_unit_with_import_is_meaningful(ws):
    """对照：unit import 了实现 -> 不判弱。"""
    _write(ws, UNIT_REL, UNIT_GOOD)
    verdict = _verdict(ws, UNIT_REL, {UNIT_REL: ("unit",)})
    assert verdict != V_NO_IMPORT, f"import 了实现却判 {verdict}"


def test_integration_uses_unit_rule(ws):
    """integration 与 unit 同规则（都要 import 实现）。"""
    _write(ws, UNIT_REL, UNIT_NO_IMPORT)
    assert _verdict(ws, UNIT_REL, {UNIT_REL: ("integration",)}) == V_NO_IMPORT


# ---------------------------------------------------------------------------
# ③ 类型来源：键的坐标空间必须一致
# ---------------------------------------------------------------------------

class _Cfg:
    backend_dir = "backend"


def _declared_types(test_files, design_file_paths):
    """直接驱动 TddLoop._declared_types（轻量构造，不拉起整条流水线）。"""
    from factory.models import DesignPlan, TestSpec

    loop = object.__new__(TddLoop)
    loop.config = _Cfg()
    loop._plans = {
        "REQ-1": RequirementTestPlan(
            req_id="REQ-1",
            test_files=tuple(TestFileSpec(path=p, type=t) for p, t in test_files),
        )
    }
    plan = DesignPlan(
        req_id="REQ-1", summary="s",
        tests=tuple(TestSpec(test_id=f"T{i}", req_id="REQ-1", type="e2e", file_path=fp)
                    for i, fp in enumerate(design_file_paths)),
    )
    return loop._declared_types("REQ-1", plan)


def test_type_lookup_uses_output_relative_key():
    """★测试计划的 path 是「output_dir 相对」，必须原样命中查找键。"""
    mapping = _declared_types([("test-e2e/login.spec.js", "e2e")], [])
    assert mapping.get("test-e2e/login.spec.js") == ("e2e",)
    # 同时也登记 backend 前缀形式，避免约定变化再次静默不匹配
    assert mapping.get("backend/test-e2e/login.spec.js") == ("e2e",)


def test_type_lookup_normalizes_backend_relative_design_path():
    """★原 bug：设计产物 file_path 是 backend 相对，与查找键（output 相对）不同空间。

    修复前 -> types 为空 -> e2e 拿不到豁免 -> WEAK_TEST 误报。
    """
    mapping = _declared_types([], ["test-e2e/login.spec.js"])
    assert mapping.get("backend/test-e2e/login.spec.js") == ("e2e",), \
        "设计产物的 backend 相对路径未被归一到 output 相对"


def test_design_path_absent_still_gets_type_from_plan():
    """设计没填 file_path（实测常见）时，仍要能从**测试计划**拿到类型。"""
    mapping = _declared_types([("test-e2e/login.spec.js", "e2e")], [])
    assert "e2e" in mapping.get("backend/test-e2e/login.spec.js", ())


def test_unknown_type_falls_back_to_import_rule(ws):
    """没有类型信息时退回原判据（宁可判弱，也不放行未经验证的测试）。"""
    _write(ws, UNIT_REL, UNIT_NO_IMPORT)
    assert _verdict(ws, UNIT_REL, {}) == V_NO_IMPORT
