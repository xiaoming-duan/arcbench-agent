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


# ---------------------------------------------------------------------------
# ④ 模板的就绪探测面（平台实测：did not become ready within 120 seconds）
# ---------------------------------------------------------------------------

APP_JS = ROOT / "template" / "backend" / "src" / "app.js"
INDEX_JS = ROOT / "template" / "backend" / "src" / "index.js"


def test_app_exposes_common_readiness_paths():
    """★常见就绪探测路径必须都返回 200。

    实测（平台 2026-10-02）：`template application server did not become
    ready within 120 seconds`。我们**无法预知探测用的是哪条路径**，
    每少一条 200 就可能换来一次 120 秒超时，而且看不出是路径不对。
    原先 `/health` 与 `/healthz` 都是 **404**。
    """
    src = APP_JS.read_text(encoding="utf-8")
    for path in ("/api/health", "/health", "/healthz"):
        assert f"'{path}'" in src, f"模板缺少就绪路径 {path}"


def test_missing_dist_does_not_return_503():
    """★frontend/dist 缺失时 `/` 不得回 503。

    「就绪」与「正确」是两件事：探测通常只认 2xx，一旦 `/` 恒回 503，
    探测永远不就绪 -> 120 秒超时，而真正原因（前端没构建）被完全掩盖。
    改成 200 后，问题会暴露在**基准测试**里 —— 那才是该暴露它的地方。
    """
    src = APP_JS.read_text(encoding="utf-8")
    assert ".status(503)" not in src, \
        "无 dist 时 `/` 又回到了 503 —— 会让就绪探测永远超时"
    assert ".status(200)" in src


def test_server_binds_all_interfaces():
    """`app.listen(port)` 不得绑定 127.0.0.1 —— 探测可能在别的命名空间。

    日志里那句 "listening at http://127.0.0.1:PORT" 只是文案；
    真正要紧的是 listen 没有 host 参数（绑定全部网卡）。
    """
    src = INDEX_JS.read_text(encoding="utf-8")
    assert "app.listen(port, () => {" in src, "listen 签名变了，确认是否误绑了 host"
