"""Playwright E2E 生成路径（P0-2）的验证用例。

运行：pytest tests/test_playwright_e2e.py -v

关注三件事：
  ① 模板是否**已经**具备 Playwright（不需新增）
  ② `type: e2e` 是否被计划门禁接受，且路径白名单按类型分流
  ③ 提示词是否注入了 E2E 生成指引
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
from factory.testplan import (  # noqa: E402
    RequirementTestPlan,
    TestFileSpec,
    normalize_test_path,
    validate_requirement_plan,
)

UI_SPEC = ROOT / "requirements_probe_ui" / "requirements.yaml"


class _C:
    model = "x"

    def is_available(self):  # noqa: ANN201
        return True


def _validate(spec_type: str, path: str) -> list[str]:
    return validate_requirement_plan(
        RequirementTestPlan(req_id="R", test_files=(TestFileSpec(path=path, type=spec_type),))
    )


# ---- ① 模板已具备 Playwright（任务的前置检查）----

def test_template_already_has_playwright_config():
    """模板**已经**有 Playwright 配置 —— 不需要新增。"""
    cfg = ROOT / "template" / "backend" / "playwright.config.js"
    assert cfg.is_file(), "playwright.config.js 应已存在（实测存在）"
    text = cfg.read_text(encoding="utf-8")
    assert "testDir" in text and "test-e2e" in text


def test_backend_declares_playwright_dependency():
    pkg = yaml.safe_load((ROOT / "template" / "backend" / "package.json")
                         .read_text(encoding="utf-8"))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    assert "playwright" in deps and "@playwright/test" in deps


def test_vitest_excludes_e2e_dir():
    """★ vitest 必须排除 test-e2e/，否则裸跑 `vitest run` 会执行 Playwright 用例。"""
    cfg = (ROOT / "template" / "backend" / "vitest.config.js").read_text(encoding="utf-8")
    assert "test-e2e" in cfg and "exclude" in cfg


# ---- ② 计划门禁按类型分流 ----

def test_e2e_path_accepted():
    assert _validate("e2e", "backend/test-e2e/a.spec.js") == []


def test_unit_path_accepted():
    assert _validate("unit", "backend/tests/a.test.js") == []


def test_e2e_in_unit_dir_rejected():
    v = _validate("e2e", "backend/tests/a.test.js")
    assert v and "test-e2e" in v[0]


def test_unit_in_e2e_dir_rejected():
    """反向：unit 测试不得放进 test-e2e/（否则 vitest 跑不到它）。"""
    v = _validate("unit", "backend/test-e2e/a.spec.js")
    assert v and "tests/" in v[0]


def test_e2e_type_is_legal():
    assert _validate("e2e", "backend/test-e2e/a.spec.js") == []
    v = _validate("smoke", "backend/tests/a.test.js")
    assert any("type 非法" in x for x in v)


def test_normalize_prepends_backend():
    assert normalize_test_path("test-e2e/a.spec.js") == "backend/test-e2e/a.spec.js"
    assert normalize_test_path("backend/test-e2e/a.spec.js") == "backend/test-e2e/a.spec.js"


def test_ui_spec_declares_e2e_tests():
    """UI 探针规格里的 tests 应全部是 e2e，且路径落在 test-e2e/。"""
    req = _adapt(yaml.safe_load(UI_SPEC.read_text(encoding="utf-8")),
                 source=UI_SPEC).requirements[0]
    assert req.tests and all(t.type == "e2e" for t in req.tests)
    assert all(t.file_path and "test-e2e" in t.file_path for t in req.tests)


# ---- ③ 提示词 ----

def test_prompt_instructs_playwright():
    req = _adapt(yaml.safe_load(UI_SPEC.read_text(encoding="utf-8")),
                 source=UI_SPEC).requirements[0]
    b = LLMGenerator(_C(), "vitest")._requirement_brief(req)
    assert "Playwright E2E" in b
    assert "@playwright/test" in b
    assert "getByLabel" in b or "getByRole" in b
    assert "RED 阶段" in b, "必须提醒 E2E 在 RED 阶段应失败"
    for token in ("①", "②", "③", "④", "⑤", "⑥"):
        assert token in b, f"覆盖要求 {token} 缺失"
