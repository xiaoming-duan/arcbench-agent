"""声明变更核查：逐条断言"我声称做过的改动"确实存在于代码里。

动机：出现过一次 patch 锚点未匹配、无断言、静默失败的情况
（RunReport.to_dict 少了 5 个可观测字段，直到核对报告才发现）。
静默失败会污染结论，因此**每一条声称的改动都必须可被机器验证**。

用法：python3 tools/verify_changes.py      全部通过退出码 0，否则 1
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _has(rel: str, *needles: str) -> tuple[bool, str]:
    text = _src(rel)
    missing = [n for n in needles if n not in text]
    if missing:
        return False, f"{rel} 缺少: {missing}"
    return True, f"{rel} ✓ {len(needles)} 项"


def _check_models_to_dict() -> tuple[bool, str]:
    """RunReport.to_dict 必须输出全部声明字段（这就是上次静默失败处）。"""
    from factory.models import RequirementResult, RunReport

    result = RequirementResult(req_id="R", state="PASSED")
    result.test_rewrites = 1
    result.test_plan_files = ["backend/tests/a.test.js"]
    result.unauthorized_files = ["backend/tests/b.test.js"]
    result.weakening_violations = [{"code": "ASSERTION_DELETION", "path": "x"}]
    report = RunReport(project_name="p", requirements_total=1, results=[result])
    row = report.to_dict()["results"][0]
    required = {
        "req_id", "state", "attempts", "red_first_ok", "note", "test_rewrites",
        "test_plan_files", "test_plan_baselines", "unauthorized_files", "weakening_violations",
    }
    missing = sorted(required - set(row))
    if missing:
        return False, f"RunReport.results[] 缺字段: {missing}"
    if "cost" not in report.to_dict():
        return False, "RunReport 缺 cost 字段"
    return True, f"RunReport 字段完整（results[] {len(required)} 项 + cost）"


def _check_audit_verdicts() -> tuple[bool, str]:
    from factory import testaudit

    # 常量名带 V_ 前缀；这里断言的是"判定语义齐备"，不是某个字面量
    expected = {
        "V_IMPORTS": "IMPORTS_IMPLEMENTATION",
        "V_NO_IMPORT": "NO_IMPLEMENTATION_IMPORT",
        # 动态 import 拆成两种语义相反的情况：
        #   LITERAL    路径字面量，可解析 -> 算引用实现
        #   UNRESOLVED 路径不可解析       -> 不算引用（判 WEAK_TEST）
        "V_DYNAMIC_LITERAL": "DYNAMIC_IMPORT_LITERAL",
        "V_DYNAMIC_UNRESOLVED": "DYNAMIC_IMPORT_UNRESOLVED",
        "V_ALIAS": "ALIAS_UNRESOLVED",
        "V_MISSING": "MISSING_FILE",
        "V_E2E": "E2E_EXEMPT",
        "V_HELPER": "HELPER_EXEMPT",
    }
    missing = [n for n, v in expected.items() if getattr(testaudit, n, None) != v]
    if missing:
        return False, f"testaudit 判定缺失/不符: {missing}"
    for name in ("audit_imports", "is_helper_file", "looks_like_alias", "has_non_literal_dynamic_import"):
        if not callable(getattr(testaudit, name, None)):
            return False, f"testaudit.{name} 不存在"
    weak, exempt = testaudit.WEAK_VERDICTS, testaudit.EXEMPT_VERDICTS
    if testaudit.V_NO_IMPORT not in weak or testaudit.V_E2E not in exempt:
        return False, "WEAK_VERDICTS / EXEMPT_VERDICTS 分类不符"
    return True, f"audit_imports + {len(expected)} 类判定 + 弱/豁免分类"


def _check_config_switches() -> tuple[bool, str]:
    from factory.config import FactoryConfig

    cfg = FactoryConfig()
    needed = ["model_max_tokens", "model_timeout_s", "max_test_rewrites", "weak_feedback_mode",
              "enforce_test_whitelist", "audit_in_red_gate", "implementation_root",
              "import_aliases", "backend_dir", "backend_test_dir"]
    missing = [n for n in needed if not hasattr(cfg, n)]
    if missing:
        return False, f"FactoryConfig 缺开关: {missing}"
    if cfg.model_max_tokens != 1500:
        return False, f"model_max_tokens 期望 1500，实际 {cfg.model_max_tokens}"
    return True, f"{len(needed)} 个开关齐备"


def _check_probe_set() -> tuple[bool, str]:
    from factory.adapter import _adapt
    from factory.workspace import load_requirements_raw

    src = ROOT / "requirements_probe" / "requirements.yaml"
    rs = _adapt(load_requirements_raw(src), source=src)
    ids = [r.req_id for r in rs.requirements]
    if len(ids) != 12:
        return False, f"探针集期望 12 个需求，实际 {len(ids)}"
    deps = {r.req_id: list(r.dependencies) for r in rs.requirements}
    roots = sorted(k for k, v in deps.items() if not v)
    if roots != ["REQ-1", "REQ-5", "REQ-9"]:
        return False, f"根节点期望 REQ-1/5/9，实际 {roots}"
    pos = {r: i for i, r in enumerate(ids)}
    bad = [(r, d) for r, ds in deps.items() for d in ds if pos[d] > pos[r]]
    if bad:
        return False, f"拓扑序违反依赖: {bad}"
    if deps["REQ-12"] != ["REQ-3", "REQ-11"]:
        return False, f"REQ-12 依赖异常: {deps['REQ-12']}"
    return True, f"12 需求 / 根 {roots} / 拓扑序合法"


CHECKS: list[tuple[str, object]] = [
    # ---- 模型接入 ----
    ("llm: 标准库 HTTP 回退后端", lambda: _has(
        "factory/llm.py", "BACKEND_HTTP", "_complete_http", "urllib.request")),
    ("llm: 服务端差异降级（temperature/response_format/max_tokens）", lambda: _has(
        "factory/llm.py", "服务端限制 temperature", "不支持 response_format",
        "改用 max_completion_tokens")),
    ("llm: 推理占满预算自适应放大", lambda: _has(
        "factory/llm.py", "MAX_TOKEN_CEILING", "正文为空（推理占满预算）")),
    ("llm: CallStats 成本记账", lambda: _has(
        "factory/llm.py", "class CallStats", "_record_usage", "gateway_retries", "json_retries")),
    # ---- 测试执行 ----
    ("testrunner: ANSI 清洗 + NO_COLOR", lambda: _has(
        "factory/testrunner.py", "_ANSI_RE", "_clean", "NO_COLOR")),
    ("testrunner: 结构化失败反馈（TAP 诊断块）", lambda: _has(
        "factory/testrunner.py", "缩进诊断块", "AssertionError")),
    ("testrunner: npm 缓存重定向", lambda: _has(
        "factory/testrunner.py", "FACTORY_NPM_CACHE")),
    # ---- 控制面 / 正确面 ----
    ("loop: 实现阶段测试文件护栏", lambda: _has(
        "factory/loop.py", "_guard_implementation_files")),
    ("loop: WEAK_TEST 阻断 + 配对回退", lambda: _has(
        "factory/loop.py", "回退到写测试阶段重写", "_weak_reason", "self.config.require_red_first")),
    ("loop: 计划门禁 + 持久化", lambda: _has(
        "factory/loop.py", "_make_test_plan", "_persist_test_plan", "validate_requirement_plan")),
    ("loop: A 路径白名单 + UNAUTHORIZED_TEST_FILE", lambda: _has(
        "factory/loop.py", "_enforce_test_whitelist", "UNAUTHORIZED_TEST_FILE", "_run_paths")),
    ("loop: 弱化守卫双信号合取", lambda: _has(
        "factory/loop.py", "_meaningful_files", "_enforce_no_weakening", "ASSERTION_DELETION",
        "单独跑失败 + import 实现")),
    ("loop: 逐文件门禁判定（C 接入）", lambda: _has(
        "factory/loop.py", "_is_weak", "audit_in_red_gate")),
    ("loop: 拒绝反馈闭环（写测试 + 重写两条路径共用）", lambda: _has(
        "factory/loop.py", "_rejection_feedback", "被白名单/基线守卫**拒绝**",
        "只允许写以下**计划内**文件", "allowed_paths=allowed_paths")),
    # ---- 生成器 ----
    ("generator: plan_tests 三处实现", lambda: _has(
        "factory/generator.py", "def plan_tests", "_PLAN_SYSTEM", "plan_feedback")),
    ("generator: 实现阶段禁止改测试", lambda: _has(
        "factory/generator.py", "绝对不要新增、删除或改写")),
    # ---- 计划与审计 ----
    ("testplan: 校验规则 + 基线", lambda: _has(
        "factory/testplan.py", "validate_requirement_plan", "record_baseline", "check_baseline")),
    ("models: RunReport.to_dict 字段完整", _check_models_to_dict),
    ("testaudit: audit_imports + 判定齐备", _check_audit_verdicts),
    ("config: 开关齐备", _check_config_switches),
    ("pipeline: weak==0 纳入 ok + cost 写入", lambda: _has(
        "factory/pipeline.py", "weak == 0", "report.cost = model_client.stats.to_dict()")),
    # ---- 交付物 ----
    ("schema: test_plan.schema.yaml", lambda: _has(
        "schemas/test_plan.schema.yaml", "x-validation-rules", "UNAUTHORIZED_TEST_FILE")),
    ("架构文档: 第 17 章过程发现", lambda: _has(
        "软件工厂-项目架构文档.md",
        "光有白名单只能阻断，回传理由才能纠正", "17.5 弱化守卫的判定标准")),
    ("探针集: 12 需求 + 依赖 DAG", _check_probe_set),
]


def main() -> int:
    failures: list[str] = []
    print(f"{'状态':<6}{'检查项':<46}详情")
    print("-" * 100)
    for name, fn in CHECKS:
        try:
            ok, detail = fn()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"{'✅' if ok else '❌':<6}{name:<46}{detail}")
        if not ok:
            failures.append(f"{name}: {detail}")
    print("-" * 100)
    if failures:
        print(f"\n❌ {len(failures)}/{len(CHECKS)} 项未通过：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"\n✅ 全部 {len(CHECKS)} 项声称的改动均已核实存在")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
