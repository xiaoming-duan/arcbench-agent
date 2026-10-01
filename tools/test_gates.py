"""门禁行为的断言测试：不调用模型，用假生成器 + 真实 TddLoop。

=====================  为什么需要它  =====================
D3（初次写测试被白名单拒绝后没有回传理由）的修复**当初只靠肉眼验证**，
紧接着就引入了 D4（把字符串列表传给期望字典列表的函数）。
这说明「改完看一眼」不足以守住门禁行为——必须有断言。

本文件覆盖：
  T1  D3 核心：初次写测试被拒 -> 带反馈重试 -> 成功（且反馈内容正确）
  T2  D3 耗尽：始终写不对 -> 阻断，且理由明确
  T3  D4 回归：回传的 feedback 必须是字符串且含被拒路径（不是把 str 当 dict 用）
  T4  重写路径：WEAK_TEST 触发的重写同样带上白名单与拒绝理由
  T5  弱化守卫：对「有意义」的文件拒绝删断言；对空转文件允许整体重写
  T6  运行集合：白名单开启时只跑计划内路径

用法：python3 tools/test_gates.py      全部通过退出码 0
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.config import FactoryConfig  # noqa: E402
from factory.loop import TddLoop  # noqa: E402
from factory.models import (  # noqa: E402
    DesignPlan,
    GeneratedFile,
    InterfaceSpec,
    Requirement,
    TestOutcome,
    TestSpec,
)
from factory.models import GeneratedFile  # noqa: E402
from factory.testplan import RequirementTestPlan, TestFileSpec  # noqa: E402
from factory.testrunner import NodeTestRunner  # noqa: E402

logging.basicConfig(level=logging.CRITICAL)

PLANNED = "backend/tests/planned.test.js"
UNAUTHORIZED = "backend/tests/rogue.test.js"

FAILING_TEST = (
    "const t=require('node:test'),a=require('node:assert');\n"
    "const {value}=require('../src/impl');\n"
    "t('t',()=>{a.equal(value(),999)});\n"
)
PASSING_TEST = (
    "const t=require('node:test'),a=require('node:assert');\n"
    "const {value}=require('../src/impl');\n"
    "t('t',()=>{a.equal(value(),1)});\n"
)
VACUOUS_TEST = "const t=require('node:test'),a=require('node:assert');\nt('t',()=>{a.equal(1,1)});\n"


class NullStore:
    """TddLoop 需要 store，但这些测试只关心门禁行为。"""

    def __getattr__(self, _name):  # noqa: ANN204
        return lambda *a, **k: None


class FakeGenerator:
    """可编程生成器：记录每次调用的入参，按脚本返回文件。"""

    name = "fake"

    def __init__(self, *, write_script, weak_rewrite_script=None, design=None, plan=None):
        self.write_script = list(write_script)
        self.weak_rewrite_script = list(weak_rewrite_script or [])
        self._design = design
        self._plan = plan
        self.write_calls: list[dict] = []

    def design(self, requirement):  # noqa: ANN001
        return self._design

    def plan_tests(self, requirement, plan, plan_feedback=""):  # noqa: ANN001
        return self._plan

    def write_tests(self, requirement, plan, weak_feedback="", allowed_paths=()):  # noqa: ANN001
        self.write_calls.append(
            {"weak_feedback": weak_feedback, "allowed_paths": tuple(allowed_paths)}
        )
        script = self.write_script if len(self.write_calls) == 1 else (
            self.weak_rewrite_script or self.write_script
        )
        content = script.pop(0) if script else None
        if content is None:
            raise AssertionError("FakeGenerator 脚本耗尽")
        path = UNAUTHORIZED if content is UNAUTHORIZED else PLANNED
        return [GeneratedFile(path=path, content=("x" if content is UNAUTHORIZED else content))]

    def implement(self, requirement, plan, failures, test_context: str = ""):  # noqa: ANN001
        return []


def make_workspace() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="gate-test-"))
    (ws / "backend/src").mkdir(parents=True)
    (ws / "backend/tests").mkdir(parents=True)
    (ws / "backend/src/impl.js").write_text("module.exports = { value: () => 1 };", encoding="utf-8")
    return ws


def build_loop(ws: Path, gen, **cfg) -> TddLoop:
    # 默认 max_repairs=0（快），但允许调用方显式覆盖 —— 早前这里硬编码 0，
    # 把调用方传的 max_repairs 静默吞掉了，导致"理由回传"这一环测不到。
    max_repairs = cfg.pop("max_repairs", 0)
    config = FactoryConfig.from_env(**cfg)
    config.max_repairs = max_repairs
    return TddLoop(
        store=NullStore(),  # type: ignore[arg-type]
        generator=gen,  # type: ignore[arg-type]
        runner=NodeTestRunner(ws, timeout_s=60),
        config=config,
        output_dir=ws,
    )


def sample_requirement() -> Requirement:
    return Requirement(req_id="REQ-1", name="demo")


def sample_design() -> DesignPlan:
    return DesignPlan(
        req_id="REQ-1",
        summary="s",
        interfaces=(
            InterfaceSpec(interface_id="REQ-1.SVC", req_ids=("REQ-1",), type="db", content="value()"),
        ),
        tests=(
            TestSpec(test_id="REQ-1.TEST.t", req_id="REQ-1", type="unit", intent="值等于 1"),
        ),
    )


def sample_plan() -> RequirementTestPlan:
    return RequirementTestPlan(
        req_id="REQ-1",
        test_files=(TestFileSpec(path=PLANNED, type="unit", covers=()),),
        scenarios=(),
    )


RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'}  {name}" + (f"   {detail}" if detail else ""))


# ---------------------------------------------------------------------------


def t1_initial_reject_then_retry() -> None:
    ws = make_workspace()
    gen = FakeGenerator(
        write_script=[UNAUTHORIZED, FAILING_TEST], design=sample_design(), plan=sample_plan()
    )
    loop = build_loop(ws, gen)
    result = loop.run(sample_requirement())

    calls = gen.write_calls
    check("T1a 被拒后确实重试（write_tests 调用 2 次）", len(calls) == 2, f"实际 {len(calls)}")
    check(
        "T1b 重试时回传了拒绝理由（非空字符串）",
        len(calls) == 2 and isinstance(calls[1]["weak_feedback"], str)
        # 计划内文件一个都没提交、只提交了别的文件名 -> 现在归为 TEST_FILE_DRIFT
        # （白名单的细分，见 t29）。T1b 只保证「理由回传且指名被拒路径」。
        and any(code in calls[1]["weak_feedback"]
                for code in ("UNAUTHORIZED_TEST_FILE", "TEST_FILE_DRIFT"))
        and UNAUTHORIZED in calls[1]["weak_feedback"],
        f"feedback={calls[1]['weak_feedback'][:60]!r}" if len(calls) == 2 else "",
    )
    check(
        "T1c 两次调用都收到白名单",
        all(c["allowed_paths"] == (PLANNED,) for c in calls),
    )
    check("T1d 计划内文件已生成，未因缺失而阻断", (ws / PLANNED).is_file())
    check(
        "T1e 计划外文件未落盘",
        not (ws / UNAUTHORIZED).is_file(),
    )
    check("T1f 被拒路径记入 unauthorized_files", UNAUTHORIZED in result.unauthorized_files)
    shutil.rmtree(ws, ignore_errors=True)


def t2_reject_exhaustion() -> None:
    ws = make_workspace()
    gen = FakeGenerator(
        write_script=[UNAUTHORIZED] * 5, design=sample_design(), plan=sample_plan()
    )
    loop = build_loop(ws, gen)
    result = loop.run(sample_requirement())

    # 从配置推导，避免提示词/预算调整后断言失效（本轮 max_test_rewrites 2 -> 4）
    expected_calls = loop.config.max_test_rewrites + 1
    check("T2a 始终写不对时判定 FAILED", result.state == "FAILED", result.note[:70])
    check(
        "T2b 失败理由说明尝试次数",
        f"{expected_calls} 次尝试后仍缺失" in result.note,
        result.note[:90],
    )
    check(
        f"T2c 重试次数 = max_test_rewrites+1 = {expected_calls}",
        len(gen.write_calls) == expected_calls,
        f"实际 {len(gen.write_calls)}",
    )
    shutil.rmtree(ws, ignore_errors=True)


def t3_feedback_type_regression() -> None:
    """D4 回归：回传的 feedback 必须是字符串，且不能被当 dict 用。"""
    ws = make_workspace()
    gen = FakeGenerator(
        write_script=[UNAUTHORIZED, FAILING_TEST], design=sample_design(), plan=sample_plan()
    )
    loop = build_loop(ws, gen)
    result = loop.run(sample_requirement())

    fb = gen.write_calls[1]["weak_feedback"] if len(gen.write_calls) > 1 else ""
    check("T3a feedback 是 str（D4 的类型错误不再出现）", isinstance(fb, str), type(fb).__name__)
    check("T3b feedback 含被拒文件路径", UNAUTHORIZED in fb)
    check("T3c feedback 含计划内路径清单", PLANNED in fb)
    check("T3d 未抛异常（result 正常返回）", result.state in {"PASSED", "FAILED"})
    shutil.rmtree(ws, ignore_errors=True)


def t4_rewrite_path_gets_whitelist() -> None:
    """WEAK_TEST 触发的重写同样要带白名单与拒绝理由。"""
    ws = make_workspace()
    gen = FakeGenerator(
        write_script=[VACUOUS_TEST, FAILING_TEST],
        design=sample_design(),
        plan=sample_plan(),
    )
    loop = build_loop(ws, gen)
    loop.run(sample_requirement())
    check("T4a 空转测试触发重写（write_tests 调用 2 次）", len(gen.write_calls) == 2)
    check(
        "T4b 重写调用带白名单",
        len(gen.write_calls) == 2 and gen.write_calls[1]["allowed_paths"] == (PLANNED,),
    )
    shutil.rmtree(ws, ignore_errors=True)


def t5_meaningful_guard() -> None:
    """弱化守卫：有意义的文件不许删断言；空转文件允许整体重写。"""
    ws = make_workspace()
    (ws / PLANNED).write_text(FAILING_TEST, encoding="utf-8")
    loop = build_loop(ws, FakeGenerator(write_script=[], design=sample_design(), plan=sample_plan()))

    class Audit:
        files = [
            type("F", (), {"path": PLANNED, "verdict": "IMPORTS_IMPLEMENTATION"})(),
            type("F", (), {"path": "backend/tests/vac.test.js", "verdict": "NO_IMPLEMENTATION_IMPORT"})(),
        ]

    meaningful = loop._meaningful_files(Audit())
    check("T5a 单独跑失败 + import 实现 => 有意义", PLANNED in meaningful)
    check(
        "T5b 空转文件（无 import）不在有意义集合",
        "backend/tests/vac.test.js" not in meaningful,
    )

    from factory.models import RequirementResult

    res = RequirementResult(req_id="REQ-1", state="PENDING")
    weakened = [GeneratedFile(path=PLANNED, content="const t=require('node:test');\nt('t',()=>{});")]
    kept = loop._enforce_no_weakening(sample_requirement(), weakened, meaningful, res)
    check("T5c 删断言被拒", kept == [] and res.weakening_violations, str(res.weakening_violations[:1]))

    vacuous_rewrite = [
        GeneratedFile(path="backend/tests/vac.test.js", content="// 完全重写")
    ]
    kept2 = loop._enforce_no_weakening(sample_requirement(), vacuous_rewrite, meaningful, res)
    check("T5d 空转文件允许整体重写", len(kept2) == 1)
    shutil.rmtree(ws, ignore_errors=True)


def t6_run_paths_whitelist() -> None:
    ws = make_workspace()
    loop = build_loop(ws, FakeGenerator(write_script=[], design=sample_design(), plan=sample_plan()))
    written = [
        GeneratedFile(path=PLANNED, content="a"),
        GeneratedFile(path=UNAUTHORIZED, content="b"),
    ]
    on = loop._run_paths([PLANNED], written)
    check("T6a 白名单开启：只跑计划内路径", on == ["tests/planned.test.js"], str(on))

    loop2 = build_loop(
        ws, FakeGenerator(write_script=[], design=sample_design(), plan=sample_plan()),
        enforce_test_whitelist=False, audit_in_red_gate=False,
    )
    off = loop2._run_paths([PLANNED], written)
    check("T6b 对照臂：计划外文件也进入运行集合", "tests/rogue.test.js" in off, str(off))
    shutil.rmtree(ws, ignore_errors=True)


UPSTREAM_FILE = "backend/src/services/summary.js"
UPSTREAM_SRC = "function summarizeItems(i=[]){return {total:i.length};}\nmodule.exports={summarizeItems};\n"
DOWNSTREAM_FILE = "backend/src/services/report.js"
DOWNSTREAM_TEST = "backend/tests/req12.test.js"

DOWNSTREAM_TEST_SRC = (
    "const t=require('node:test'),a=require('node:assert');\n"
    "const {buildReport}=require('../src/services/report');\n"
    "t('t',()=>{const r=buildReport([]);a.deepStrictEqual(Object.keys(r).sort(),['movements','summary']);});\n"
)
# 不调用上游：参数注入式（复现 chain5 的真实病理）
DOWNSTREAM_IMPL_NO_DEP = (
    "function buildReport(items=[]){return {summary:{},movements:[]};}\n"
    "module.exports={buildReport};\n"
)
# 真实调用上游
DOWNSTREAM_IMPL_WITH_DEP = (
    "const {summarizeItems}=require('./summary');\n"
    "function buildReport(items=[]){return {summary:summarizeItems(items),movements:[]};}\n"
    "module.exports={buildReport};\n"
)


class DependencyGenerator:
    """下游需求：测试先失败、实现后通过；实现内容由参数决定是否调用上游。"""

    name = "fake-dep"

    def __init__(self, impl_src: str, *, indirect: tuple = ()):
        self.impl_src = impl_src
        self.indirect = indirect
        self.write_calls: list[dict] = []

    def design(self, requirement):  # noqa: ANN001
        return DesignPlan(
            req_id=requirement.req_id, summary="s",
            interfaces=(InterfaceSpec(interface_id="I", req_ids=(requirement.req_id,),
                                      type="db", content="buildReport()"),),
            tests=(TestSpec(test_id="T", req_id=requirement.req_id, type="unit", intent="x"),),
        )

    def plan_tests(self, requirement, plan, plan_feedback=""):  # noqa: ANN001
        return RequirementTestPlan(
            req_id=requirement.req_id,
            test_files=(TestFileSpec(path=DOWNSTREAM_TEST, type="unit", covers=()),),
            scenarios=(),
            indirect_dependencies=self.indirect,
        )

    def write_tests(self, requirement, plan, weak_feedback="", allowed_paths=()):  # noqa: ANN001
        self.write_calls.append({"weak_feedback": weak_feedback})
        return [GeneratedFile(path=DOWNSTREAM_TEST, content=DOWNSTREAM_TEST_SRC)]

    def implement(self, requirement, plan, failures, test_context: str = ""):  # noqa: ANN001
        self.impl_failures = list(failures)
        return [GeneratedFile(path=DOWNSTREAM_FILE, content=self.impl_src)]


def dep_workspace(with_upstream_on_disk: bool = True) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="dep-gate-"))
    (ws / "backend/src/services").mkdir(parents=True)
    (ws / "backend/tests").mkdir(parents=True)
    if with_upstream_on_disk:
        (ws / UPSTREAM_FILE).write_text(UPSTREAM_SRC, encoding="utf-8")
    return ws


def run_downstream(ws: Path, gen: DependencyGenerator, *, upstream_produced=True, **cfg):
    """模拟「上游 REQ-3 已跑完，现在跑下游 REQ-12」。"""
    loop = build_loop(ws, gen, **cfg)  # type: ignore[arg-type]
    if upstream_produced:
        loop._impl_files["REQ-3"] = [UPSTREAM_FILE]
    else:
        loop._impl_files["REQ-3"] = []
    req = Requirement(req_id="REQ-12", name="报表", dependencies=("REQ-3",))
    return loop.run(req), loop


def t7_dependency_not_used_blocks() -> None:
    ws = dep_workspace()
    gen = DependencyGenerator(DOWNSTREAM_IMPL_NO_DEP)
    # max_repairs=1：给一次重写机会，才能观察到"拒绝理由回传"这一环节
    result, loop = run_downstream(ws, gen, max_repairs=1)
    check("T7a 声明依赖但未调用上游 -> FAILED", result.state == "FAILED", result.note[:80])
    check("T7b 记为 DEPENDENCY_NOT_USED",
          result.dependency_violations
          and result.dependency_violations[0]["verdict"] == "DEPENDENCY_NOT_USED",
          str([v["verdict"] for v in result.dependency_violations]))
    check("T7c 拒绝理由回传给了生成器（复用 A 的机制）",
          "真实调用" in (getattr(gen, "impl_failures", [""])[0] if getattr(gen, "impl_failures", None) else ""),
          str(getattr(gen, "impl_failures", [""])[0])[:70])
    shutil.rmtree(ws, ignore_errors=True)


def t8_dependency_used_passes() -> None:
    ws = dep_workspace()
    gen = DependencyGenerator(DOWNSTREAM_IMPL_WITH_DEP)
    result, _ = run_downstream(ws, gen)
    check("T8 真实调用上游 -> PASSED 且无依赖违规",
          result.state == "PASSED" and not result.dependency_violations,
          f"{result.state} violations={len(result.dependency_violations)}")
    shutil.rmtree(ws, ignore_errors=True)


def t9_indirect_declaration_skips() -> None:
    ws = dep_workspace()
    gen = DependencyGenerator(DOWNSTREAM_IMPL_NO_DEP,
                              indirect=(("REQ-3", "由调用方注入 summarizeItems"),))
    result, _ = run_downstream(ws, gen)
    check("T9a 声明间接依赖后不再阻断 -> PASSED", result.state == "PASSED", result.note[:70])
    check("T9b 记入 dependency_uncertain（不判 FAILED）",
          result.dependency_uncertain
          and result.dependency_uncertain[0]["verdict"] == "SKIPPED_INDIRECT",
          str([u["verdict"] for u in result.dependency_uncertain]))
    check("T9c 未记入 violations", not result.dependency_violations)
    shutil.rmtree(ws, ignore_errors=True)


def t10_control_arm_off() -> None:
    ws = dep_workspace()
    gen = DependencyGenerator(DOWNSTREAM_IMPL_NO_DEP)
    result, _ = run_downstream(ws, gen, enforce_dependency_usage=False)
    check("T10 关闭依赖门禁（control 臂）-> 假阳性通过", result.state == "PASSED",
          f"{result.state}（这正是 chain5 里 REQ-12 的假阳性）")
    shutil.rmtree(ws, ignore_errors=True)


def t11_upstream_missing_blocks() -> None:
    ws = dep_workspace(with_upstream_on_disk=False)
    # 用参数注入式实现：测试能通过，依赖门禁才会被触达（这正是 chain5 里 REQ-12 的形态）
    gen = DependencyGenerator(DOWNSTREAM_IMPL_NO_DEP)
    result, _ = run_downstream(ws, gen, upstream_produced=False)
    check("T11 上游未产出任何实现 -> UPSTREAM_MISSING 阻断",
          result.state == "FAILED"
          and result.dependency_violations
          and result.dependency_violations[0]["verdict"] == "UPSTREAM_MISSING",
          str([v["verdict"] for v in result.dependency_violations]))
    shutil.rmtree(ws, ignore_errors=True)


# --- 方案 2-A / 2-B / 回归检查 -------------------------------------------------

MOCK_TEST_SRC = (
    "const vi = { mock: (...a) => {} };\n"          # 模拟 vitest 提供的 vi
    "vi.mock('../src/services/summary');\n"
    "const t=require('node:test'),a=require('node:assert');\n"
    "const {buildReport}=require('../src/services/report');\n"
    "t('t',()=>{const r=buildReport([{quantity:5}]);"
    "a.deepStrictEqual(Object.keys(r).sort(),['movements','summary']);});\n"
)
PLAIN_TEST_SRC = (
    "const t=require('node:test'),a=require('node:assert');\n"
    "const {buildReport}=require('../src/services/report');\n"
    "t('t',()=>{const r=buildReport([{quantity:5}]);"
    "a.deepStrictEqual(Object.keys(r).sort(),['movements','summary']);});\n"
)
# 通过测试但不调用上游（参数注入）
IMPL_OK_NO_DEP = (
    "function buildReport(items=[],movements=[],summarizeItems){\n"
    "  return {summary: typeof summarizeItems==='function'?summarizeItems(items):{},movements};\n"
    "}\nmodule.exports={buildReport};\n"
)
# 通过测试、真实调用上游、无守卫
IMPL_OK_REAL = (
    "const {summarizeItems}=require('./summary');\n"
    "function buildReport(items=[],movements=[]){\n"
    "  return {summary:summarizeItems(items),movements};\n"
    "}\nmodule.exports={buildReport};\n"
)
# 通过测试、且带形参守卫（注入旁路）
IMPL_OK_BYPASS = (
    "const {summarizeItems:upstream}=require('./summary');\n"
    "function buildReport(items=[],movements=[],summarizeItems){\n"
    "  const summary = typeof summarizeItems==='function' ? summarizeItems(items) : upstream(items);\n"
    "  return {summary,movements};\n"
    "}\nmodule.exports={buildReport};\n"
)
# 测试会失败（返回值缺 summary）
IMPL_BAD = "function buildReport(){return {movements:[]};}\nmodule.exports={buildReport};\n"


class ScriptedGenerator:
    """按脚本依次返回实现；测试文件与计划可配置。"""

    name = "scripted"

    def __init__(self, impls, *, test_src=PLAIN_TEST_SRC, mocked=()):
        self.impls = list(impls)
        self.test_src = test_src
        self.mocked = mocked
        self.implement_calls = 0
        self.seen_context: list[str] = []

    def design(self, requirement):  # noqa: ANN001
        return DesignPlan(
            req_id=requirement.req_id, summary="s",
            interfaces=(InterfaceSpec(interface_id="I", req_ids=(requirement.req_id,),
                                      type="db", content="buildReport()"),),
            tests=(TestSpec(test_id="T", req_id=requirement.req_id, type="unit", intent="x"),),
        )

    def plan_tests(self, requirement, plan, plan_feedback=""):  # noqa: ANN001
        return RequirementTestPlan(
            req_id=requirement.req_id,
            test_files=(TestFileSpec(path=DOWNSTREAM_TEST, type="unit", covers=()),),
            scenarios=(), mocked_dependencies=self.mocked,
        )

    def write_tests(self, requirement, plan, weak_feedback="", allowed_paths=()):  # noqa: ANN001
        return [GeneratedFile(path=DOWNSTREAM_TEST, content=self.test_src)]

    def implement(self, requirement, plan, failures, test_context: str = ""):  # noqa: ANN001
        self.seen_context.append(test_context)
        src = self.impls[min(self.implement_calls, len(self.impls) - 1)]
        self.implement_calls += 1
        return [GeneratedFile(path=DOWNSTREAM_FILE, content=src)]


def run_scripted(impls, *, test_src=PLAIN_TEST_SRC, mocked=(), max_repairs=0, **cfg):
    ws = dep_workspace()
    loop = build_loop(ws, ScriptedGenerator(impls, test_src=test_src, mocked=mocked),
                      max_repairs=max_repairs, **cfg)  # type: ignore[arg-type]
    loop._impl_files["REQ-3"] = [UPSTREAM_FILE]
    req = Requirement(req_id="REQ-12", name="报表", dependencies=("REQ-3",))
    result = loop.run(req)
    shutil.rmtree(ws, ignore_errors=True)
    return result


def run_scripted_with_loop(impls, *, test_src=PLAIN_TEST_SRC, mocked=(), max_repairs=0,
                           inner=None, **cfg):
    """同 run_scripted，但把 loop 与生成器一并返回（用于观察重写调用）。"""
    ws = dep_workspace()
    gen = ScriptedGenerator(impls, test_src=test_src, mocked=mocked)
    if inner is not None:
        gen.inner = inner
    loop = build_loop(ws, gen, max_repairs=max_repairs, **cfg)  # type: ignore[arg-type]
    loop._impl_files["REQ-3"] = [UPSTREAM_FILE]
    req = Requirement(req_id="REQ-12", name="报表", dependencies=("REQ-3",))
    result = loop.run(req)
    shutil.rmtree(ws, ignore_errors=True)
    return result, gen


def t24_feedback_includes_stderr() -> None:
    """vitest 把错误写到 stderr、摘要写到 stdout。

    旧写法 `stdout or stderr` 在 stdout 非空时永远看不到 stderr ->
    模型只收到「0 个测试被收集」，从未知道 ReferenceError 在哪一行。
    实测这条缺失让 REQ-7 连续两轮无法修复。断言两流都被带出。
    """
    from factory.models import TestOutcome as TO

    loop = build_loop(dep_workspace(), FakeGenerator(
        write_script=[], design=sample_design(), plan=sample_plan()))
    outcome = TO(
        passed=False, command="vitest", exit_code=1, total=0, failed=0,
        stdout=" RUN  v4.1.8\n ❯ tests/req7.test.js (0 test)\n Tests  no tests",
        stderr="⎯ Failed Suites 1 ⎯\nReferenceError: Cannot access '__vi_import_0__'"
               " before initialization\n ❯ tests/req7.test.js:5:14",
    )
    reason = loop._broken_test_reason(outcome)
    fb = loop._broken_test_feedback(outcome)
    check("T24a 理由含 stderr 里的 ReferenceError（旧写法会丢）",
          "ReferenceError" in reason and "before initialization" in reason,
          reason[:80].replace("\n", " "))
    check("T24b 理由同时保留 stdout 摘要作为上下文",
          "no tests" in reason or "0 test" in reason)
    check("T24c 反馈也含 stderr", "ReferenceError" in fb and "req7.test.js:5:14" in fb)


def t25_vitest_prompt_forbids_hoisted_toplevel_ref() -> None:
    """测试生成提示词必须显式禁止 vi.hoisted 内引用顶层 import。"""
    from factory.generator import LLMGenerator

    class _C:
        model = "x"
        def is_available(self):  # noqa: ANN201
            return True

    gen = LLMGenerator(_C(), "vitest")  # type: ignore[arg-type]
    note = gen._dialect_note()
    check("T25a vitest 提示词含 vi.hoisted 硬约束", "vi.hoisted" in note)
    check("T25b 说明成因（回调先于模块加载执行）与后果（收集阶段失败、0 个测试）",
          "之前" in note and "收集阶段" in note and "0 个测试" in note)
    check("T25c 给出正确写法（回调内部 require 或 vi.importActual）",
          "require(" in note and "vi.importActual" in note)
    node_gen = LLMGenerator(_C(), "node")  # type: ignore[arg-type]
    check("T25d node 方言不受影响（不误加 vitest 约束）",
          "vi.hoisted" not in node_gen._dialect_note())


def t26_root_container_is_not_designed() -> None:
    """容器节点（平台 ROOT）必须被识别出来并跳过设计。

    实测病理：平台把「整个平台」作为 ROOT 塞进需求树，工厂照常对它调用 design()，
    等于让模型一次性设计整个平台 -> prompt/token 爆炸 -> 每次调用卡满超时，
    重试耗尽后整条 ROOT 失败。

    注意两种层级表达都要认：父节点自带 children，或子节点声明 parent_id。
    只认前者会在平台只用 parent_id 时漏判。
    """
    from factory.pipeline import build_children_map, container_ids_of

    # 表达一：父节点自带 children
    by_children = [
        Requirement(req_id="ROOT", name="平台根需求", children_ids=("REQ-1", "REQ-2")),
        Requirement(req_id="REQ-1", name="叶子一"),
        Requirement(req_id="REQ-2", name="叶子二", dependencies=("REQ-1",)),
    ]
    check("T26a 父节点自带 children 时识别为容器",
          container_ids_of(by_children) == {"ROOT"}, str(container_ids_of(by_children)))

    # 表达二：只有子节点声明 parent_id（ROOT 自己没有 children 字段）
    by_parent = [
        Requirement(req_id="ROOT", name="平台根需求"),
        Requirement(req_id="REQ-1", name="叶子一", parent_id="ROOT"),
        Requirement(req_id="REQ-2", name="叶子二", parent_id="ROOT"),
    ]
    check("T26b 仅子节点声明 parent_id 时也能识别出容器",
          container_ids_of(by_parent) == {"ROOT"}, str(container_ids_of(by_parent)))

    # 扁平集不得误判
    flat = [Requirement(req_id="REQ-1", name="a"), Requirement(req_id="REQ-2", name="b")]
    check("T26c 扁平需求集不产生容器（不误跳过正常需求）",
          container_ids_of(flat) == set(), str(container_ids_of(flat)))
    check("T26d 叶子需求不会被当成容器",
          "REQ-2" not in container_ids_of(by_children), str(container_ids_of(by_children)))

    # 依赖边与父子关系必须互不干扰：REQ-2 depends_on REQ-1 不应让 REQ-1 变成容器
    check("T26e 依赖（depends_on）不被误认成父子关系",
          build_children_map(flat) == {}, str(build_children_map(flat)))


def t27_json_parse_hardening_and_adaptive_retry() -> None:
    """JSON 解析必须能判定「是否被截断」，且重试必须改变 prompt 而不是原样重发。

    实测病理（平台日志）：模型输出是**以 { 开头、结构正确**的设计 JSON，但被
    max_tokens 截断在 interface content 的半个字符串里。而旧日志只打前 2000 字符，
    截取窗口**必然**停在半个字符串里——因此无法区分「模型输出被截断」与
    「日志窗口截断」。判据必须看整体闭合情况，头尾都要给。

    同时旧重试原样重发 prompt：输出被截断时必然再次被截断，两次必然全废
    （平台上正是「第 1/2 次 -> 重试 -> 第 2 次」连着失败）。
    """
    from factory.llm import _JSON_RETRY_NOTE, _extract_json, _json_truncation_note

    mid_string = '{"summary":"x","interfaces":[{"content":"POST /api/auth/reg'
    check("T27a 结尾停在半个字符串 -> 判为截断",
          "字符串字面量未闭合" in _json_truncation_note(mid_string))
    check("T27b 缺闭合括号 -> 判为截断",
          "未闭合" in _json_truncation_note('{"a":1'))
    check("T27c 括号与字符串都闭合 -> 不误判为截断",
          "非截断" in _json_truncation_note('{"a":1,}'))

    for label, raw in [
        ("纯 JSON", '{"a":1}'),
        ("markdown 围栏", '```json\n{"b":2}\n```'),
        ("前后解释文字", '好的：\n{"c":3}\n以上'),
        ("顶层数组", '[{"d":4}]'),
    ]:
        try:
            ok = bool(_extract_json(raw))
        except Exception:  # noqa: BLE001
            ok = False
        check(f"T27d 宽容提取：{label}", ok)

    try:
        _extract_json(mid_string)
        carries = False
    except ValueError as exc:
        carries = "原始输出前 500 字符" in str(exc) and "疑似被截断" in str(exc)
    check("T27e 失败时异常携带原始输出并说明截断", carries)

    from factory.llm import CallStats, ModelCallError, ModelClient

    captured: list[str] = []
    client = object.__new__(ModelClient)
    client.stats = CallStats()

    def fake_complete(*, system, user, json_mode=False):  # noqa: ANN001, ANN202
        captured.append(user)
        return mid_string

    client.complete = fake_complete  # type: ignore[method-assign]
    try:
        client.complete_json(system="S", user="原始指令", attempts=2)
    except ModelCallError:
        pass
    check("T27f 首次尝试用原始 prompt", len(captured) == 2 and captured[0] == "原始指令")
    check("T27g 第二次尝试追加精简指令（不是原样重发）",
          len(captured) == 2 and _JSON_RETRY_NOTE.strip()[:16] in captured[1])


def t28_verdict_judgment_is_unified() -> None:
    """「什么算引用了实现」必须只有一处定义，门禁与守卫共用。

    历史坑：门禁走 `verdict in WEAK_VERDICTS`，守卫写死 `verdict != V_IMPORTS`。
    今天两者恰好互补，但一旦新增 verdict 就会**静默分叉**：一个放行、一个阻断，
    而且不报错。本测试锁三件事：
      1) 判据只有一处（is_meaningful_import / is_weak_verdict）
      2) 动态 import 按可解析性分成语义相反的两种
      3) 真实引用了实现的文件，不因"另有无关动态 import"被误杀
    """
    import tempfile
    from pathlib import Path as _Path

    from factory.testaudit import (
        EXEMPT_VERDICTS,
        MEANINGFUL_VERDICTS,
        V_DYNAMIC_LITERAL,
        V_DYNAMIC_UNRESOLVED,
        V_IMPORTS,
        WEAK_VERDICTS,
        audit_imports,
        is_meaningful_import,
        is_weak_verdict,
    )

    check("T28a 字面量动态 import 算引用实现",
          is_meaningful_import(V_DYNAMIC_LITERAL))
    check("T28b 不可解析动态 import 判弱（不算引用实现）",
          is_weak_verdict(V_DYNAMIC_UNRESOLVED) and not is_meaningful_import(V_DYNAMIC_UNRESOLVED))
    check("T28c 判据自洽：任何 verdict 不会同时既强又弱",
          not (MEANINGFUL_VERDICTS & WEAK_VERDICTS))
    check("T28d 强/弱/豁免三集互补，无重叠",
          not (MEANINGFUL_VERDICTS & EXEMPT_VERDICTS)
          and not (WEAK_VERDICTS & EXEMPT_VERDICTS)
          and len(MEANINGFUL_VERDICTS) + len(WEAK_VERDICTS) + len(EXEMPT_VERDICTS) == 8)

    ws = _Path(tempfile.mkdtemp(prefix="verdict-check-"))
    try:
        (ws / "backend/src").mkdir(parents=True)
        (ws / "backend/tests").mkdir(parents=True)
        (ws / "backend/src/svc.js").write_text("module.exports={f:()=>1};\n", encoding="utf-8")
        cases = {
            # 真实引用了实现，但另有无关的不可解析动态 import -> 必须仍判「算引用」
            "real_plus_dynamic": "const s=require('../src/svc.js');\nconst l=await import(dyn);\n",
            # 只有不可解析动态 import -> 判弱
            "dynamic_only": "const m = await import(someVar);\n",
            # 只有字面量动态 import 指向实现 -> 算引用（新 verdict）
            "literal_only": "const m = await import('../src/svc.js');\n",
        }
        for name, src in cases.items():
            (ws / "backend/tests" / f"{name}.test.js").write_text(src, encoding="utf-8")

        def verdict_of(name: str) -> str:
            audit = audit_imports(ws, [f"backend/tests/{name}.test.js"],
                                  implementation_root="backend/src")
            return audit.files[0].verdict

        v_real = verdict_of("real_plus_dynamic")
        v_dyn = verdict_of("dynamic_only")
        v_lit = verdict_of("literal_only")

        check("T28e 真实引用 + 无关动态 import -> 仍算引用（修复整文件短路误杀）",
              v_real == V_IMPORTS and is_meaningful_import(v_real), v_real)
        check("T28f 仅不可解析动态 import -> DYNAMIC_IMPORT_UNRESOLVED",
              v_dyn == V_DYNAMIC_UNRESOLVED, v_dyn)
        check("T28g 仅字面量动态 import 指实现 -> DYNAMIC_IMPORT_LITERAL",
              v_lit == V_DYNAMIC_LITERAL, v_lit)

        # 门禁判据（weak_files）与守卫判据（is_meaningful_import）必须处处互补
        for name, expect_weak in (("real_plus_dynamic", False),
                                  ("dynamic_only", True),
                                  ("literal_only", False)):
            audit = audit_imports(ws, [f"backend/tests/{name}.test.js"],
                                  implementation_root="backend/src")
            file = audit.files[0]
            gate_weak = bool(audit.weak_files)
            guard_meaningful = is_meaningful_import(file.verdict)
            check(f"T28h 门禁与守卫对 {name} 一致（weak={gate_weak}）",
                  gate_weak == expect_weak and gate_weak != guard_meaningful,
                  f"verdict={file.verdict} weak={gate_weak} meaningful={guard_meaningful}")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def t29_test_file_drift_and_broken_priority() -> None:
    """① ② ③ ④：区分弱化/重写、TEST_BROKEN 优先、主题漂移、收集诊断。

    实测病理（平台）：
      - 文件 IMPORTS_IMPLEMENTATION（确实 import 了实现）但 vitest 收集到 0 个测试
        （TEST_BROKEN）；此时它 28 -> 14 的断言缩减被 ASSERTION_DELETION 拒绝，
        正当的「删掉坏断言以修好文件」被误伤，重写预算耗尽在同一个坏文件上。
      - 计划的 req1-1-1.register.test.js 被换成 workbook.view.test.js：
        需求讲注册、测试测工作簿视图，后续断言检查全部失去意义。
    """
    from factory.models import GeneratedFile, RequirementResult

    loop = build_loop(make_workspace(), FakeGenerator(
        write_script=[], design=sample_design(), plan=sample_plan()))
    ws = loop.output_dir
    result = RequirementResult(req_id="REQ-1", state="DESIGNING")

    # 旧文件：20 个用例 / 20 个断言；新内容只有 2 个 -> 明显缩减
    old = "\n".join(f"test('c{i}', () => {{ expect(1).toBe(1); }});" for i in range(20))
    new = "\n".join(f"test('c{i}', () => {{ expect(1).toBe(1); }});" for i in range(2))
    (ws / PLANNED).parent.mkdir(parents=True, exist_ok=True)
    (ws / PLANNED).write_text(old, encoding="utf-8")
    rewrite = [GeneratedFile(path=PLANNED, content=new, mode="write")]
    meaningful = {PLANNED}

    # ② TEST_BROKEN 优先：文件跑不起来时，断言数不具可比性 -> 放行但记录
    kept_broken = loop._enforce_no_weakening(
        sample_requirement(), list(rewrite), meaningful, result,
        is_rewrite_phase=True, broken_test=True,
    )
    codes = [v["code"] for v in result.weakening_violations]
    check("T29a TEST_BROKEN 时不再因断言减少阻断重写",
          len(kept_broken) == 1, f"保留 {len(kept_broken)}/1")
    check("T29b 但记录为 ASSERTION_REDUCTION_ON_REWRITE（放行不等于无记录）",
          "ASSERTION_REDUCTION_ON_REWRITE" in codes, str(codes))

    # 非 TEST_BROKEN（正常弱化）仍必须拦截
    result2 = RequirementResult(req_id="REQ-1", state="DESIGNING")
    kept_normal = loop._enforce_no_weakening(
        sample_requirement(), list(rewrite), meaningful, result2,
        is_rewrite_phase=True, broken_test=False,
    )
    check("T29c 非 TEST_BROKEN 时断言减少仍被拦截（守卫未被架空）",
          kept_normal == [] and result2.weakening_violations
          and result2.weakening_violations[0]["code"] == "ASSERTION_DELETION",
          f"保留 {len(kept_normal)} 条, 违规 {result2.weakening_violations}")

    # ③ 主题漂移：计划内一个都没提交 + 提交了别的文件名 -> TEST_FILE_DRIFT
    drift_files = [GeneratedFile(path="backend/tests/workbook.view.test.js",
                                 content=new, mode="write")]
    accepted, violations = loop._enforce_test_whitelist(
        sample_requirement(), list(drift_files), [PLANNED])
    check("T29d 整体换文件名 -> 判 TEST_FILE_DRIFT",
          [v["code"] for v in violations] == ["TEST_FILE_DRIFT"], str(violations))
    check("T29e 漂移的文件不被接受", accepted == [], f"接受 {len(accepted)} 条")

    # 计划内 + 额外文件 -> 只是计划外文件，不算漂移
    extra = [GeneratedFile(path=PLANNED, content=new, mode="write"),
             GeneratedFile(path="backend/tests/extra.test.js", content=new, mode="write")]
    accepted2, violations2 = loop._enforce_test_whitelist(
        sample_requirement(), list(extra), [PLANNED])
    check("T29f 计划内仍在 + 多写一个 -> 记 UNAUTHORIZED 而非 DRIFT",
          [v["code"] for v in violations2] == ["UNAUTHORIZED_TEST_FILE"], str(violations2))
    check("T29g 计划内文件仍被接受", len(accepted2) == 1, f"接受 {len(accepted2)}")

    # ④ 收集诊断：必须包含退出码、stderr、源码前 10 行
    from factory.models import TestOutcome as TO
    bad = "const x = ;\n" * 12
    (ws / "backend/tests/broken.test.js").write_text(bad, encoding="utf-8")
    outcome = TO(passed=False, command="vitest", exit_code=1, total=0, failed=0,
                 stdout=" RUN  v4.1.8", stderr="SyntaxError: Unexpected token ';'")
    blob = loop._broken_test_diagnostics(["backend/tests/broken.test.js"], outcome)
    check("T29h TEST_BROKEN 诊断含退出码与收集数",
          "exit_code=1" in blob and "收集到 0 个测试" in blob, blob[:80])
    check("T29i 诊断含 stderr", "SyntaxError" in blob)
    check("T29j 诊断含源码前 10 行（带行号）",
          " 1|" in blob and "10|" in blob, blob[-140:])

    # 真实循环传入的是 **backend 相对**路径（tests/x.test.js，供运行器用），
    # 而文件落盘在 output_dir/backend/tests/x.test.js —— 两种形式都必须能读到源码，
    # 否则诊断会退化成「(读取失败: No such file)」，把最有用的证据变成误导。
    blob2 = loop._broken_test_diagnostics(["tests/broken.test.js"], outcome)
    check("T29k backend 相对路径也能读到源码（不是 '读取失败'）",
          "读取失败" not in blob2 and "const x = ;" in blob2, blob2[-160:])


def t30_esm_contract_and_static_syntax_check() -> None:
    """①③④⑥：ESM 约束、精简约束、require('vitest') 静态拦截、模板示例。

    实测病理（平台）：模型在截断压力下改用更短的 CommonJS 写法
    `require('vitest')` —— vitest 是纯 ESM 包，必然抛
    「Vitest cannot be imported in a CommonJS module using require()」。
    它其实不必跑一次 vitest 才知道：静态扫一眼就能拦，省掉一整轮 TEST_BROKEN。

    同时修 ⑤ 暴露出的**真实误判**：文件里写 `../../src/...`（多退一层）
    解析到项目根、不指向 backend/src，审计判 NO_IMPLEMENTATION_IMPORT 是**对的**，
    该改的是 prompt 里的路径写法，不是解析器。
    """
    from factory.generator import LLMGenerator
    from factory.models import GeneratedFile
    from factory.testrunner import VitestRunner

    class _C:
        model = "x"

        def is_available(self):  # noqa: ANN201
            return True

    vitest_note = LLMGenerator(_C(), "vitest")._test_file_contract()  # type: ignore[arg-type]
    node_note = LLMGenerator(_C(), "node")._test_file_contract()  # type: ignore[arg-type]

    check("T30a vitest 约定明确要求 ESM import",
          "import { describe, it, expect } from 'vitest'" in vitest_note)
    check("T30b vitest 约定明确禁止 require('vitest')",
          "require('vitest')" in vitest_note and "禁止" in vitest_note)
    check("T30c vitest 约定禁止 module.exports", "module.exports" in vitest_note)
    check("T30d 约定写明正确相对深度 ../src/ 并警告 ../../src/ 是错的",
          "../src/" in vitest_note and "../../src/" in vitest_note)
    check("T30e 精简约束：<=200 行 + 不重建 schema + 复用 fixture",
          "200 行" in vitest_note and "schema" in vitest_note and "fixture" in vitest_note)
    check("T30f node 方言不被误加 ESM 约束（node 用 require 是对的）",
          "require('node:test')" in node_note and "禁止 `require('vitest')`" not in node_note)

    ws = make_workspace()
    loop_v = build_loop(ws, FakeGenerator(write_script=[], design=sample_design(),
                                          plan=sample_plan()))
    loop_v.runner = VitestRunner(ws, timeout_s=60)
    bad = [GeneratedFile(path="backend/tests/a.test.js",
                         content="const { describe } = require('vitest');\n", mode="write")]
    good = [GeneratedFile(path="backend/tests/a.test.js",
                          content="import { describe } from 'vitest';\n", mode="write")]
    v_bad = loop_v._invalid_test_syntax(bad)
    check("T30g vitest 方言下拦截 require('vitest')",
          len(v_bad) == 1 and v_bad[0]["code"] == "INVALID_TEST_SYNTAX", str(v_bad))
    check("T30h 合法 ESM 写法不被拦截", loop_v._invalid_test_syntax(good) == [])
    loop_v.runner = NodeTestRunner(ws, timeout_s=60)
    check("T30i node 方言下 require 不被误拦（node 本就该用 require）",
          loop_v._invalid_test_syntax(bad) == [])
    shutil.rmtree(ws, ignore_errors=True)

    tpl = ROOT / "template" / "backend"
    example = tpl / "tests" / "_example.test.js"
    check("T30j 模板 ESM 示例文件存在", example.is_file(), str(example))
    if example.is_file():
        text = example.read_text(encoding="utf-8")
        check("T30k 示例使用 ESM import 且未用 require('vitest')",
              "import { describe, it, expect } from 'vitest'" in text
              and "require('vitest')" not in text)
    cfg_text = (tpl / "vitest.config.js").read_text(encoding="utf-8")
    check("T30l vitest.config.js 显式排除示例文件（避免被收集执行）",
          "tests/_example.test.js" in cfg_text)


def t31_code_version_anchor() -> None:
    """代码版本锚点：每次运行自带 HEAD + 脏文件数；脏 / 取不到 都标不可信。

    为什么值得断言：这个工作区已为「验证代码本身没被验证」付过三次代价 ——
    to_dict 缺字段导致 patch 静默失败、measure_source 别名断言失效、
    并发写入把一次测量改到一半。没有版本锚点，这三种事故在报告上都不留痕迹。

    关键语义：**unknown != clean**。取不到 git 信息时必须判不可信，
    否则「不知道干不干净」会被当成「干净」用。
    """
    import os
    import subprocess
    import tempfile

    from factory.models import RUN_REPORT_FIELDS, RunReport, validate_run_report
    from factory.version import UNKNOWN, describe, git_state, should_warn

    check("T31a code_version 已进入 RunReport 字段契约",
          "code_version" in RUN_REPORT_FIELDS)
    payload = RunReport(project_name="p", requirements_total=0).to_dict()
    check("T31b to_dict 输出 code_version", "code_version" in payload)
    check("T31c 含 code_version 的报告能通过 validate_run_report",
          isinstance(validate_run_report(payload), dict))

    # 非仓库：unknown != clean
    norepo = Path(tempfile.mkdtemp(prefix="ver-norepo-"))
    try:
        st = git_state(norepo)
        check("T31d 非 git 目录 -> head=unknown / dirty=-1 / 不可信",
              st["head"] == UNKNOWN and st["dirty_count"] == -1
              and st["trustworthy"] is False, str(st))
        check("T31e 描述文本明说不可信", "不可信" in describe(st), describe(st))
        check("T31e2 取不到 git 属环境常态 -> 不按 WARNING 刷屏（但仍不可信）",
              should_warn(st) is False and st["trustworthy"] is False, str(st))
    finally:
        shutil.rmtree(norepo, ignore_errors=True)

    # 干净仓库 vs 有未提交改动
    repo = Path(tempfile.mkdtemp(prefix="ver-clean-"))
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    })
    try:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
        (repo / "a.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, env=env)

        clean = git_state(repo)
        check("T31f 干净仓库 -> dirty=0 且可信",
              clean["dirty_count"] == 0 and clean["trustworthy"] is True, str(clean))
        check("T31g 干净时描述不含『可疑』", "可疑" not in describe(clean), describe(clean))

        (repo / "a.txt").write_text("y", encoding="utf-8")
        dirty = git_state(repo)
        check("T31h 有未提交改动 -> 不可信，且列出具体文件",
              dirty["dirty_count"] == 1 and dirty["trustworthy"] is False
              and dirty["dirty_files"], str(dirty))
        check("T31i 脏时描述点名『测量基础可疑』", "可疑" in describe(dirty), describe(dirty))
        check("T31j 脏工作区才按 WARNING 报（异常 vs 环境常态要分开）",
              should_warn(dirty) is True and should_warn(clean) is False)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def t32_missing_implementation_is_valid_red() -> None:
    """★ 架构性矛盾：反 WEAK_TEST 要求「必须 import 真实实现」，而 RED 阶段
    实现**还不存在** —— 静态 ESM 下这会让文件在收集阶段整体失败、报 0 个测试。

    实测（vitest 4.1.8）：
        Cannot find module '../src/services/summary.js' imported from .../tests/x.test.js
        Tests  no tests        <- total == 0
    模块存在、只是断言失败时才是  Tests 1 failed (1)（total == 1）。

    旧判据把 total==0 一律当 TEST_BROKEN，等于要求模型去修一个它修不了的问题：
    删掉 import 就不再引用实现（变 WEAK_TEST），留着 import 就永远收集失败。
    重写预算被烧光，而真凶不在测试里。

    实测对照：node 方言把「加载失败」计为 1 个失败测试（total==1），
    所以这个坑只在 vitest 上暴露。
    """
    from factory.models import TestOutcome as TO

    ws = make_workspace()
    loop = build_loop(ws, FakeGenerator(write_script=[], design=sample_design(),
                                        plan=sample_plan()))
    tests_dir = ws / "backend" / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    tf = tests_dir / "x.test.js"
    tf.write_text("import { f } from '../src/svc.js';\n", encoding="utf-8")

    def outcome(stderr: str, total: int = 0, passed: bool = False) -> TO:
        return TO(passed=passed, command="vitest", exit_code=1, total=total,
                  failed=0, stdout="", stderr=stderr)

    # ★夹具必须是**平台原文**：importing 路径两侧带单引号。
    #   初版夹具是我手写的「无引号版」，于是正则里漏掉 ['"]? 也照样全绿，
    #   而修复在平台上一條都匹配不上、静默失效（2026-09-30 14:15:55 的日志）。
    missing_impl = (
        "Error: Cannot find module '../src/services/workbookService.js' "
        f"imported from '{tf}'\n\n Test Files  1 failed (1)\n      Tests  no tests"
    )
    o = outcome(missing_impl)
    check("T32a 缺实现模块的收集失败 -> 不算 TEST_BROKEN（有效 RED）",
          loop._is_uncollectable(o) is False, str(loop._expected_red_reason(o)))
    # 无引号变体（本地实测的 Rolldown 措辞）也必须认——两种都出现过
    unquoted = ("Error: Cannot find module '../src/services/summary.js' "
                f"imported from {tf}\n      Tests  no tests")
    check("T32a2 无引号变体同样识别（两种措辞都要认）",
          loop._is_uncollectable(outcome(unquoted)) is False)
    check("T32b 且给出可读理由（点明是 RED 常态）",
          "尚未存在" in (loop._expected_red_reason(o) or "")
          or "RED 阶段" in (loop._expected_red_reason(o) or ""),
          str(loop._expected_red_reason(o)))

    syntax = "RolldownError: Parse failure: Parse failed with 1 error:\nUnexpected token\n3: const x = ;"
    check("T32c 真语法错误仍判 TEST_BROKEN",
          loop._is_uncollectable(outcome(syntax)) is True)

    hoisted = "ReferenceError: Cannot access '__vi_import_0__' before initialization"
    check("T32d vi.hoisted 顶层引用仍判 TEST_BROKEN（旧病理不能放走）",
          loop._is_uncollectable(outcome(hoisted)) is True)

    bare = "Error: Cannot find module 'vitest' imported from " + str(tf)
    check("T32e 缺的是裸包名（vitest）-> 不算有效 RED，仍判 TEST_BROKEN",
          loop._is_uncollectable(outcome(bare)) is True)

    helper = ("Error: Cannot find module '../helpers/util.js' "
              f"imported from {tf}")
    check("T32f 缺的是实现根之外的模块 -> 不算有效 RED（那是测试自己的依赖）",
          loop._is_uncollectable(outcome(helper)) is True)

    check("T32g 模块存在且断言失败（total=1）本来就放行",
          loop._is_uncollectable(outcome("", total=1)) is False)

    # Vite 的另一种措辞也要认
    vite = ('Failed to resolve import "../src/svc.js" from "tests/x.test.js". '
            "Does the file exist?")
    check("T32h Vite 措辞（Failed to resolve import）同样识别",
          loop._is_uncollectable(outcome(vite)) is False)
    shutil.rmtree(ws, ignore_errors=True)


def t33_exit_code_contract() -> None:
    """平台退出码契约：**完成即 0**（README: "exit with code 0 when finished"）。

    旧行为：未全通过就 `return 2`。平台把它当硬错误
    （"returned non-zero exit status 2"）——于是 agent 明明跑完了、
    traceability 与 runner-events 也如实上报了，平台看到的却是「进程失败」。
    一个退出码把真实产出盖掉，正是「报告与事实不符」这一类缺陷。

    但必须同时守住另一头：**真崩溃仍要非 0**，否则会把崩溃伪装成完成。
    所以本测试两头都断言。
    """
    import subprocess

    env = dict(os.environ)
    env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'arcbench-agent-runtime' / 'src'}"
    ws = Path(tempfile.mkdtemp(prefix="exitcode-"))
    try:
        # 会跑完但失败的需求集：实现无法满足断言 -> report.ok=False
        reqs = ws / "reqs"
        (reqs / "fixtures/REQ-1/tests/backend/tests").mkdir(parents=True)
        (reqs / "fixtures/REQ-1/impl/backend/src").mkdir(parents=True)
        (reqs / "requirements.yaml").write_text(
            "schema_version: \"1.0\"\n"
            "project: {id: e, name: exit}\n"
            "requirements:\n"
            "  - id: REQ-1\n"
            "    name: 必然失败\n"
            "    tests:\n"
            "      - id: REQ-1.TEST.x\n"
            "        type: unit\n"
            "        file_path: backend/tests/x.test.js\n"
            "        intent: 断言实现返回 999（实现返回 1）\n",
            encoding="utf-8")
        (reqs / "fixtures/REQ-1/tests/backend/tests/x.test.js").write_text(
            "const test = require('node:test');\n"
            "const assert = require('node:assert');\n"
            "const svc = require('../src/svc.js');\n"
            "test('x', () => { assert.equal(svc.value(), 999); });\n",
            encoding="utf-8")
        (reqs / "fixtures/REQ-1/impl/backend/src/svc.js").write_text(
            "module.exports = { value: () => 1 };\n", encoding="utf-8")

        def run(req_dir, out, extra=None):
            e = dict(env)
            e.update(extra or {})
            proc = subprocess.run(
                [sys.executable, str(ROOT / "main.py"), str(req_dir),
                 "--output-dir", str(out), "--generator", "stub",
                 "--test-dialect", "node", "--install-deps", "never"],
                cwd=str(ROOT), env=e, capture_output=True, text=True, timeout=300)
            return proc.returncode

        out_fail = ws / "out-fail"
        check("T33a 跑完但失败 -> 默认退出码 0（平台契约：完成即 0）",
              run(reqs, out_fail) == 0)
        report = json.loads((out_fail / ".arc/factory-report.json").read_text(encoding="utf-8"))
        check("T33b 退出码 0 不能掩盖失败：报告里 ok=False 且节点为 FAILED",
              report.get("ok") is False
              and {r["req_id"]: r["state"] for r in report["results"]} == {"REQ-1": "FAILED"},
              str(report.get("ok")))
        check("T33c FACTORY_STRICT_EXIT=1 时恢复「通过与否」语义（本地/CI 用）",
              run(reqs, ws / "out-fail-strict", {"FACTORY_STRICT_EXIT": "1"}) == 2)

        check("T33d 全部通过 -> 0（两种模式）",
              run(ROOT / "requirements_sample", ws / "out-ok") == 0
              and run(ROOT / "requirements_sample", ws / "out-ok2",
                      {"FACTORY_STRICT_EXIT": "1"}) == 0)

        # 真崩溃：需求文件本身无法解析 -> pipeline 抛异常 -> 必须非 0
        # （注意「没有 fixture」并不算崩溃：那会走到「计划门禁未通过」并正常结束，
        #   退出码应为 0。T33e 初版就错在这里，被断言当场抓住。）
        crash = ws / "crash"
        crash.mkdir()
        (crash / "requirements.yaml").write_text(
            'schema_version: "1.0"\nrequirements: [this is: not valid yaml\n',
            encoding="utf-8")
        out_crash = ws / "out-crash"
        check("T33e 真崩溃仍非 0（1），不能被伪装成完成",
              run(crash, out_crash) == 1
              and run(crash, ws / "out-crash2", {"FACTORY_STRICT_EXIT": "1"}) == 1)
        events = [json.loads(ln) for ln in
                  (out_crash / ".arc/runner-events.jsonl").read_text(encoding="utf-8").splitlines()
                  if ln.strip()]
        check("T33f 崩溃仍上报 mark_run_failed（后端不会看到静默退出）",
              "failed" in [e.get("state") for e in events if e.get("type") == "runner_state"],
              str([e.get("state") for e in events if e.get("type") == "runner_state"]))
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def t34_sdk_backend_parity_and_summary_arithmetic() -> None:
    """① SDK 后端必须与 HTTP 后端同等记账与校验；② 摘要账目必须对得上。

    实测（平台 2026-09-30 14:42）：
      「成本记账: 调用 0 次 / token 0 … / prompt 69276 字符（单次最大 11804）」
    prompt 体积在 complete() 入口累加，而 calls 只在 HTTP 后端自增 ——
    平台上 openai 是装好的（requirements.txt 里有），所以走的是 SDK 路径，
    于是**整套记账与截断诊断全部缺席**：
      - 成本恒为 0
      - finish_reason=length 的截断永不告警
      - 空正文不报错、传输错误不重试、协议适配也没有

    同一份日志还暴露第二个账目问题：共 42 个需求，尝试 3 + 上游失败 21 = 24，
    剩下 18 个是「只分解不设计」的容器节点，此前在摘要里完全不出现。
    """
    from factory.llm import MAX_TOKEN_CEILING, CallStats, ModelCallError, ModelClient
    from factory.models import RUN_REPORT_FIELDS, RunReport

    class _Resp:
        def __init__(self, content, finish="stop", usage=(11, 7, 18), reasoning=None):
            self._d = {
                "choices": [{"finish_reason": finish,
                             "message": {"content": content, "reasoning_content": reasoning}}],
                "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1],
                          "total_tokens": usage[2]},
            }

        def model_dump(self):  # noqa: ANN201
            return self._d

    class _Completions:
        def __init__(self, script):
            self.script = list(script)
            self.calls = []

        def create(self, **payload):  # noqa: ANN003
            self.calls.append(payload)
            item = self.script.pop(0) if self.script else _Resp("")
            if isinstance(item, Exception):
                raise item
            return item

    def client(script):
        c = object.__new__(ModelClient)
        c.stats = CallStats()
        c.api_key, c.model, c.base_url = "k", "m", None
        c.temperature, c.max_tokens, c.timeout_s = 0.2, 1500, 30
        comp = _Completions(script)
        c._sdk = type("S", (), {"chat": type("C", (), {"completions": comp})()})()
        return c

    c = client([_Resp('{"ok":1}')])
    out = c._complete_sdk(system="s", user="u", json_mode=True)
    check("T34a SDK 后端会记调用次数（此前恒为 0）",
          out == '{"ok":1}' and c.stats.calls == 1, f"calls={c.stats.calls}")
    check("T34b SDK 后端会记 token（此前恒为 0）",
          c.stats.total_tokens == 18 and c.stats.prompt_tokens == 11,
          f"tokens={c.stats.total_tokens}")

    c = client([_Resp("")])
    try:
        c._complete_sdk(system="s", user="u", json_mode=False)
        raised = ""
    except ModelCallError as exc:
        raised = str(exc)
    check("T34c SDK 后端空正文会报错（并触发预算升级后停在上限）",
          "空内容" in raised and c.max_tokens == MAX_TOKEN_CEILING,
          f"{raised[:50]} max_tokens={c.max_tokens}")

    class _Boom(Exception):
        status_code = 503

    c = client([_Boom("upstream 503"), _Resp('{"ok":1}')])
    c._complete_sdk(system="s", user="u", json_mode=False)
    check("T34d SDK 后端可重试错误会重试并计入 gateway_retries（此前一次都不重试）",
          c.stats.gateway_retries == 1 and c.stats.calls == 1,
          f"retries={c.stats.gateway_retries} calls={c.stats.calls}")

    c = client([Exception("unsupported parameter: max_tokens"), _Resp('{"ok":1}')])
    c._complete_sdk(system="s", user="u", json_mode=False)
    check("T34e SDK 后端共享协议适配（max_tokens -> max_completion_tokens）",
          c.stats.adaptation_retries == 1
          and "max_completion_tokens" in c._sdk.chat.completions.calls[1],
          f"adapt={c.stats.adaptation_retries}")

    # ② 摘要账目
    check("T34f RunReport 契约含 decomposed", "decomposed" in RUN_REPORT_FIELDS)
    payload = RunReport(project_name="p", requirements_total=42, decomposed=18).to_dict()
    attempted, up = 3, 21
    check("T34g 账目可对上：尝试 + 上游失败 + 分解节点 = 总数",
          attempted + up + payload["decomposed"] == payload["requirements_total"],
          f"{attempted}+{up}+{payload['decomposed']} vs {payload['requirements_total']}")


def t35_transient_model_failure_does_not_kill_requirement() -> None:
    """模型/传输层瞬时故障不应**直接判需求失败**。

    实测（平台 2026-09-30 14:46）：
        ERROR factory.loop | 实现失败: openai SDK 调用失败: Request timed out.
    一个已经过了设计、写测试、RED 三道关的需求被一次超时当场判 FAILED；
    它又是上游，最终 21 个下游被跳过 —— 一次超时毁掉半轮。

    修法：记为一次失败的尝试，把错误**回传**给下一次调用（提示词因此不同，
    不是原样重发），并继续消耗既有修复预算。非瞬时失败仍快速判失败。
    """
    from factory.models import GeneratedFile

    class FlakyGen(FakeGenerator):
        def __init__(self, *, fail_impl=0, fail_design=0, fail_write=0, **kw):
            super().__init__(**kw)
            self.fail_impl, self.fail_design, self.fail_write = fail_impl, fail_design, fail_write
            self.design_calls = 0
            self.write_attempts_count = 0
            self.impl_failures: list[list] = []

        def design(self, requirement):  # noqa: ANN001
            self.design_calls += 1
            if self.fail_design > 0:
                self.fail_design -= 1
                raise RuntimeError("openai SDK 调用失败: Request timed out.")
            return super().design(requirement)

        def write_tests(self, *a, **kw):  # noqa: ANN002, ANN003
            # 注意：要在这里计数，而不是靠 write_calls —— 失败的那次调用
            # 根本没走到 super()，用 write_calls 数会把「重试」看成「没重试」。
            self.write_attempts_count += 1
            if self.fail_write > 0:
                self.fail_write -= 1
                raise RuntimeError("openai SDK 调用失败: Request timed out.")
            return super().write_tests(*a, **kw)

        def implement(self, requirement, plan, failures, test_context=""):  # noqa: ANN001
            self.impl_failures.append(list(failures))
            if self.fail_impl > 0:
                self.fail_impl -= 1
                raise RuntimeError("openai SDK 调用失败: Request timed out.")
            return [GeneratedFile(path="backend/src/impl.js",
                                  content="module.exports = { value: () => 1 };")]

    def mk(**kw):
        return FlakyGen(write_script=[FAILING_TEST], design=sample_design(),
                        plan=sample_plan(), **kw)

    # ① 实现阶段瞬时失败 -> 重试，且第二次的 failures 带上了错误（提示词不同）
    ws = make_workspace()
    gen = mk(fail_impl=1)
    build_loop(ws, gen, max_repairs=1).run(sample_requirement())
    check("T35a 实现调用瞬时失败后会重试（不是当场判死）",
          len(gen.impl_failures) >= 2, f"implement 调用 {len(gen.impl_failures)} 次")
    check("T35b 重试时的 failures 回传了失败原因（提示词因此不同，非原样重发）",
          any("timed out" in f for f in (gen.impl_failures[1] if len(gen.impl_failures) > 1 else [])),
          str(gen.impl_failures[1] if len(gen.impl_failures) > 1 else [])[:80])
    shutil.rmtree(ws, ignore_errors=True)

    # ② 一直失败 -> 仍受既有修复预算约束（有界，不无限重试）
    ws = make_workspace()
    gen = mk(fail_impl=99)
    res = build_loop(ws, gen, max_repairs=1).run(sample_requirement())
    check("T35c 持续失败仍受 max_repairs 约束（有界）",
          res.state == "FAILED" and len(gen.impl_failures) <= 3,
          f"state={res.state} 调用 {len(gen.impl_failures)} 次")
    check("T35d 失败理由说明尝试次数与模型侧原因",
          "次尝试" in (res.note or "") and "timed out" in (res.note or ""),
          (res.note or "")[:80])
    shutil.rmtree(ws, ignore_errors=True)

    # ③ 设计阶段瞬时失败 -> 重试一次（设计是第一关，失败即全盘皆输）
    ws = make_workspace()
    gen = mk(fail_design=1)
    build_loop(ws, gen).run(sample_requirement())
    check("T35e 设计瞬时失败会重试一次", gen.design_calls == 2, f"design 调用 {gen.design_calls} 次")
    shutil.rmtree(ws, ignore_errors=True)

    # ④ 非瞬时失败 -> 不重试（重试无意义）
    ws = make_workspace()
    gen = FlakyGen(write_script=[FAILING_TEST], design=sample_design(),
                   plan=sample_plan(), fail_design=99)
    gen.fail_design = 0
    gen.design = lambda requirement: (_ for _ in ()).throw(RuntimeError("需求格式非法，无法解析"))
    res = build_loop(ws, gen).run(sample_requirement())
    check("T35f 非瞬时失败不重试，直接判失败",
          res.state == "FAILED" and "设计失败" in (res.note or ""), (res.note or "")[:60])
    shutil.rmtree(ws, ignore_errors=True)

    # ⑤ 写测试阶段瞬时失败 -> 走上**本来就有**的带反馈重试循环
    ws = make_workspace()
    gen = mk(fail_write=1)
    res = build_loop(ws, gen).run(sample_requirement())
    # 断言「尝试了 2 次」且「第 2 次真的写成了」——证明需求继续往下走，
    # 而不是在第一次模型抖动时就被判死。（最终 state 仍是 FAILED：
    # 这个夹具的测试断言 value()==999，本来就永远不可能通过。）
    check("T35g 写测试瞬时失败会重试并继续（复用既有重试循环，不再绕过它）",
          gen.write_attempts_count >= 2 and len(gen.write_calls) >= 1,
          f"尝试 {gen.write_attempts_count} 次, 成功写入 {len(gen.write_calls)} 次")
    shutil.rmtree(ws, ignore_errors=True)


def t37_quota_exhaustion_is_terminal_and_fails_fast() -> None:
    """配额/余额耗尽 = 终局，不是瞬时：不重试，且整轮提前收摊。

    实测（平台 2026-09-30 18:26）：
        429 {'code': 'Free quota exhausted and balance too low, please recharge
             compute credits.', 'type': 'insufficient_quota'}
    它带着 **429**，而我上一轮的瞬时判据里恰好有 "429" 这个词 —— 于是配额耗尽
    被同时判为「可重试」和「瞬时」，一次运行白白「网关重试 11 次（成功 0）」，
    外加每个需求 3 次循环级重试。

    关键区分：同是 429，
        429 rate_limit_exceeded -> 瞬时，退避重试有意义
        429 insufficient_quota  -> 终局，重试纯属烧时间
    """
    from factory.llm import (
        _is_quota_exhausted,
        _is_retryable_sdk_error,
        _is_timeout_error,
    )
    from factory.loop import _is_transient_model_error

    real = ("openai SDK 调用失败: Error code: 429 - {'error': {'code': "
            "'Free quota exhausted and balance too low, please recharge compute credits.', "
            "'type': 'insufficient_quota'}}")

    check("T37a 能识别配额耗尽（insufficient_quota / recharge / compute credits）",
          _is_quota_exhausted(real) and _is_quota_exhausted("insufficient_quota"))
    check("T37b 限流不算配额耗尽（429 rate limit 仍应可重试）",
          not _is_quota_exhausted("429 rate limit exceeded, please retry later"))

    check("T37c 配额耗尽的真实报错**不**判为瞬时（此前被 429 误判）",
          _is_transient_model_error(Exception(real)) is False, "被误判为瞬时")
    check("T37d 限流仍判为瞬时（不能把该重试的也一刀切掉）",
          _is_transient_model_error(Exception("429 rate limit exceeded")) is True)
    check("T37e 超时仍判为瞬时",
          _is_transient_model_error(Exception("Request timed out.")) is True)
    check("T37f 配额文本即使被别的层重新包装也认得出",
          _is_transient_model_error(Exception(f"包装层: {real}")) is False)

    # 时间/重试代价的区分仍然成立
    check("T37g 超时判据独立于配额判据",
          _is_timeout_error(Exception("Request timed out"))
          and not _is_timeout_error(Exception(real)))
    class _429(Exception):
        status_code = 429
    check("T37h 429 在传输层仍算可重试（限流场景），配额由上层先行拦截",
          _is_retryable_sdk_error(_429("429")) is True)

    # 配额异常必须能穿透 loop 的异常处理（不能被吞进「回传重试」）
    import inspect

    from factory.loop import TddLoop

    for name in ("run",):
        src = inspect.getsource(getattr(TddLoop, name))
        check(f"T37i {name}() 对 ModelQuotaExhaustedError 显式上抛",
              src.count("except ModelQuotaExhaustedError:") >= 3,
              f"出现 {src.count('except ModelQuotaExhaustedError:')} 次")

    from factory import pipeline

    psrc = inspect.getsource(pipeline.run_factory)
    check("T37j pipeline 在配额耗尽时 break（不再逐个烧重试预算）",
          "ModelQuotaExhaustedError" in psrc and "break" in psrc)
    check("T37k 配额中止时绝不判 ok",
          "not quota_exhausted" in psrc)

    # ---- 端到端：配额耗尽必须让整轮提前收摊（不是逐个需求烧重试预算）----
    import tempfile as _tf

    from arcbench_agent_runtime import AgentRuntime

    from factory import pipeline as _P
    from factory.config import FactoryConfig as _FC
    from factory.llm import ModelQuotaExhaustedError as _Quota

    class _QuotaGen:
        name = "quota"
        calls = 0

        def design(self, requirement):  # noqa: ANN001
            _QuotaGen.calls += 1
            raise _Quota("insufficient_quota: Free quota exhausted, please recharge")

        def plan_tests(self, *a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("配额已耗尽，不该走到计划阶段")

        write_tests = implement = plan_tests

    # 修（本工作流代为补上）：`_Path` 此前只在 t30 的作用域内局部导入，
    # T37 直接引用 -> NameError，整个 test_gates 套件报红。
    # 按该文件既有风格（t30 的 `from pathlib import Path as _Path`）补局部导入。
    from pathlib import Path as _Path

    ws = Path(_tf.mkdtemp(prefix="quota-abort-"))
    reqs = ws / "reqs"
    reqs.mkdir(parents=True)
    (reqs / "requirements.yaml").write_text(
        "schema_version: \"1.0\"\nproject: {id: q, name: quota}\nrequirements:\n"
        + "".join(f"  - id: REQ-{i}\n    name: r{i}\n" for i in (1, 2, 3)),
        encoding="utf-8")
    runtime = AgentRuntime.from_env(project_dir=str(ws / "out"))
    cfg = _FC()
    cfg.install_deps = "never"
    original = _P.build_generator
    _P.build_generator = lambda *a, **k: _QuotaGen()  # type: ignore[assignment]
    try:
        report = _P.run_factory(runtime, reqs, ws / "out", config=cfg,
                                template_dir=ROOT / "template")
    finally:
        _P.build_generator = original  # type: ignore[assignment]

    check("T37l 配额耗尽时整轮提前收摊（只尝试了 1 个需求）",
          _QuotaGen.calls == 1, f"design 被调用 {_QuotaGen.calls} 次")
    check("T37m 提前中止时 report.ok=False 且 error 指向配额",
          report.ok is False and "Quota" in (report.error or ""),
          f"ok={report.ok} error={(report.error or '')[:60]}")
    shutil.rmtree(ws, ignore_errors=True)



def t38_implementation_module_graph_audit() -> None:
    """实现引用了不存在的相对模块 -> 必须拦下并回传（平台会 `npm start` 验证）。

    实测（平台 2026-10-01 01:50）：
        Error: Cannot find module './routes/branchRoutes'
        requireStack: ['/workspace/template/backend/src/app.js', .../src/index.js]
    模型在 app.js 的「// route modules imports」锚点后插入了
    require('./routes/branchRoutes')，却**从未创建**该文件。

    我们的门禁此前一路绿灯，原因很具体：**测试 import 的是服务层**，
    不会因为 app.js 多一行 require 而失败；而平台会 `npm start`，启动即崩。
    所以这个审计必须**独立于测试**。
    """
    from factory.models import GeneratedFile

    TEST_JS = (
        "const t=require('node:test'),a=require('node:assert');\n"
        "const {value}=require('../src/widget');\n"
        "t('w',()=>{a.equal(value(),1)});\n"
    )
    APP_BROKEN = "const express=require('express');\nrequire('./routes/branchRoutes');\n"
    APP_FIXED = "const express=require('express');\n"
    WIDGET = "module.exports = { value: () => 1 };\n"

    class ScriptedGen(FakeGenerator):
        """按脚本返回实现文件集；记录每次收到的 failures。"""

        def __init__(self, sets, **kw):
            super().__init__(**kw)
            self.sets = list(sets)
            self.impl_failures: list[list] = []

        def implement(self, requirement, plan, failures, test_context=""):  # noqa: ANN001
            self.impl_failures.append(list(failures))
            spec = self.sets.pop(0) if self.sets else []
            return [GeneratedFile(path=path, content=content, mode="write")
                    for path, content in spec]

    # ① 一直写坏：必须在预算内拦下，且理由点名缺失模块
    ws = make_workspace()
    gen = ScriptedGen([[("backend/src/app.js", APP_BROKEN), ("backend/src/widget.js", WIDGET)]],
                      write_script=[TEST_JS], design=sample_design(), plan=sample_plan())
    res = build_loop(ws, gen, max_repairs=2).run(sample_requirement())
    check("T38a 引用不存在的模块 -> 最终判失败（不再一路绿灯）",
          res.state == "FAILED", f"state={res.state}")
    check("T38b 失败理由点名缺失的模块",
          "不存在的模块" in (res.note or "") and "branchRoutes" in (res.note or ""),
          (res.note or "")[:80])
    check("T38c 重试时的反馈带上了缺失模块清单（提示词因此不同）",
          any("branchRoutes" in " ".join(fs) for fs in gen.impl_failures[1:]),
          str(gen.impl_failures[1:])[:90])
    shutil.rmtree(ws, ignore_errors=True)

    # ② 第二次补齐缺失文件 -> 审计放行、需求正常通过
    ws = make_workspace()
    gen = ScriptedGen(
        [
            [("backend/src/app.js", APP_BROKEN), ("backend/src/widget.js", WIDGET)],
            [("backend/src/app.js", APP_FIXED), ("backend/src/widget.js", WIDGET),
             ("backend/src/routes/branchRoutes.js", "module.exports = {};\n")],
        ],
        write_script=[TEST_JS], design=sample_design(), plan=sample_plan())
    res2 = build_loop(ws, gen, max_repairs=2).run(sample_requirement())
    check("T38d 补齐缺失模块后放行并通过",
          res2.state == "PASSED", f"state={res2.state} note={(res2.note or '')[:60]}")
    check("T38e 审计确实拦过第一次（否则②不能证明它在工作）",
          len(gen.impl_failures) >= 2 and any("branchRoutes" in " ".join(fs)
                                              for fs in gen.impl_failures[1:]),
          f"implement 调用 {len(gen.impl_failures)} 次")
    shutil.rmtree(ws, ignore_errors=True)

    # ③ 模板自身必须干净：模板 app.js 不得引用不存在的模块
    from factory.testaudit import audit_relative_imports

    tpl = ROOT / "template" / "backend" / "src"
    tpl_broken = audit_relative_imports(ROOT / "template", sorted(tpl.rglob("*.js")))
    check("T38f 出厂模板的实现模块图是完整的（否则应用永远起不来）",
          tpl_broken == [], str(tpl_broken[:3]))


def t23_uncollectable_test_rolls_back_to_test_stage() -> None:
    """测试文件跑不起来（0 个测试被收集）-> 判 TEST_BROKEN 并回退到写测试阶段，
    而不是把 4 轮实现预算浪费在一个坏掉的测试文件上。

    实测病理（REQ-7）：测试在 `vi.hoisted` 里引用顶层 import ->
    收集阶段 ReferenceError -> 0 个测试 -> 旧行为当成有效 RED，
    4 轮实现修复全部白费。
    """
    from factory.models import TestOutcome as TO

    loop = build_loop(dep_workspace(), FakeGenerator(
        write_script=[], design=sample_design(), plan=sample_plan()))

    collected_fail = TO(passed=False, command="vitest", exit_code=1, total=0, failed=0)
    assertion_fail = TO(passed=False, command="vitest", exit_code=1, total=1, failed=1,
                        failures=["Expected 20, received 12"])
    passed_run = TO(passed=True, command="vitest", exit_code=0, total=1, failed=0)

    check("T23a 0 个测试被收集 -> 判为不可执行", loop._is_uncollectable(collected_fail))
    check("T23b 断言失败不算不可执行（这是有效的 RED 证据）",
          not loop._is_uncollectable(assertion_fail))
    check("T23c 通过也不算", not loop._is_uncollectable(passed_run))

    reason = loop._broken_test_reason(collected_fail)
    check("T23d 理由说明「这是测试自身的问题，改实现无法修复」",
          "测试自身的问题" in reason and "改实现无法修复" in reason, reason[:60])

    fb = loop._broken_test_feedback(collected_fail)
    check("T23e 反馈给出可执行的修法（vi.hoisted 不能引用顶层 import）",
          "vi.hoisted" in fb and "require(" in fb and "收集" in fb)


def t36_dependency_audit_runs_even_when_tests_fail() -> None:
    """依赖审计必须**无条件运行**，不放在 `if outcome.passed` 内。

    实测缺陷（closure6 直接证据）：
      审计原先位于 `if outcome.passed:` 之内 -> 测试不过就永不审计 ->
      报告里 `dependency_violations = 0` 看起来像「依赖用对了」，
      **实际是「依赖根本没被评估」**。
      REQ-11 的 movementsService.js 有 0 条 import、与上游从未连接，
      本应被判 DEPENDENCY_NOT_USED —— 只要门禁运行。

    而「测试过不去」恰恰最可能有依赖问题：跨模块集成断裂时，
    下游测试正是因为接不上上游而失败。

    这条断言守的是**位置**（编号本应为 T26，但该号已被并发工作流占用，故改为 T36）：审计调用必须在 `if outcome.passed:` 之前。
    """
    import inspect
    source = inspect.getsource(TddLoop.run)

    # 找到实现循环里最后一次 runner.run 之后的区域
    idx_run = source.rfind("outcome = self.runner.run(test_paths)")
    assert idx_run != -1, "找不到 outcome = self.runner.run(test_paths)"
    tail = source[idx_run:]

    # 该区域内「测试通过分支」的相对位置。
    # 必须匹配**代码行**（12 空格缩进）而不是裸子串 ——
    # 初版用 tail.find("if outcome.passed:")，结果命中了注释里引用的同一串文字，
    # 于是断言在修复已生效的情况下仍报红。断言必须认代码，不认注释。
    marker = "\n            if outcome.passed:\n"
    idx_if = tail.find(marker)
    assert idx_if != -1, "找不到其后的 if outcome.passed: 代码行"

    region_before_if = tail[:idx_if]
    for name in ("dep_audit = self._audit_dependencies(",
                 "mock_audit = self._audit_mocks(",
                 "bypass_audit = self._audit_bypass("):
        check(f"T36 审计在 if outcome.passed 之前: {name.split(' =')[0]}",
              name in region_before_if,
              "在 if 内 -> 测试不过就永不审计" if name not in region_before_if else "")

    check("T36d 违规记录在循环外无条件执行",
          "result.dependency_violations = [u.to_dict() for u in dep_audit.violations]"
          in source)

    # combined_ok 不得短路：必须是三个 ok 的由 and 连接
    gate = source[source.find("dep_ok_raw = "):]
    gate = gate[:gate.find("if outcome.passed and dep_ok")]
    check("T36e combined_ok 非短路（三个 ok 全部参与 and 链）",
          "dep_ok = dep_ok and mock_ok and bypass_ok" in gate,
          "若写成嵌套 if 短路，被跳过的门禁结果不会被使用")


def t22_test_source_is_given_to_implement() -> None:
    """实现阶段必须拿到测试源码 —— 否则接口契约与测试签名冲突时模型只能猜。

    实测病理（REQ-7）：接口契约写 updateQuantity(sku, quantity)，测试却按
    仓储注入写成 updateQuantity(repository, sku, quantity)，两契约矛盾而提示词
    不给测试源码 -> 4 轮重写都没收敛。
    """
    result, gen = run_scripted_with_loop([IMPL_OK_REAL], max_repairs=0)
    ctx = gen.seen_context[0] if gen.seen_context else ""
    check("T22a 实现阶段收到了 test_context", bool(ctx.strip()), f"len={len(ctx)}")
    check("T22b 内容是本需求的测试源码（含 require 与断言）",
          "require(" in ctx and "assert" in ctx, ctx[:70].replace("\n", " "))
    check("T22c 语义正确：真实调用上游的那次运行确实走到了实现阶段",
          result.state == "PASSED", result.state)


def t21_upstream_failed_propagation() -> None:
    """端到端：上游 WEAK_TEST 失败 -> 下游 UPSTREAM_FAILED 且不计入通过率分母。

    手法：先干净跑一次 requirements_sample（REQ-2 依赖 REQ-1）建立输出目录，
    再用「关闭白名单与 RED 审计」重跑同一目录 —— 此时测试已存在且实现已在，
    REQ-1 会因 WEAK_TEST 被阻断，REQ-2 应被上游传播跳过。
    """
    import subprocess

    ws = Path(tempfile.mkdtemp(prefix="upstream-gate-"))
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'arcbench-agent-runtime' / 'src'}"
    base = [sys.executable, str(ROOT / "main.py"), "requirements_sample",
            "--output-dir", str(ws / "out"), "--type", "web", "--install-deps", "never"]
    subprocess.run(base, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)

    dirty = dict(env, FACTORY_WHITELIST="0", FACTORY_AUDIT_GATE="0", FACTORY_UPSTREAM_GATE="1")
    subprocess.run(base, cwd=str(ROOT), env=dirty, capture_output=True, text=True, timeout=300)

    report = json.loads((ws / "out/.arc/factory-report.json").read_text(encoding="utf-8"))
    states = {r["req_id"]: r["state"] for r in report["results"]}
    r2 = next((r for r in report["results"] if r["req_id"] == "REQ-2"), {})
    check("T21a 上游失败的 REQ-2 记为 UPSTREAM_FAILED",
          states.get("REQ-2") == "UPSTREAM_FAILED", str(states))
    check("T21b 报告计数 upstream_failed=1（不计入通过率分母）",
          report.get("upstream_failed") == 1, str(report.get("upstream_failed")))
    check("T21c 下游未进入 TDD 循环（无测试计划、无成本、无重写）",
          not r2.get("test_plan_files") and not r2.get("cost")
          and r2.get("attempts") == 0 and r2.get("write_attempts") == 0,
          f"plan={r2.get('test_plan_files')} attempts={r2.get('attempts')}")
    check("T21d note 归因到具体上游",
          "REQ-1" in (r2.get("note") or ""), (r2.get("note") or "")[:60])

    # 关闭传播后同一场景应尝试执行 REQ-2（对照）
    off = dict(dirty, FACTORY_UPSTREAM_GATE="0")
    ws2 = Path(tempfile.mkdtemp(prefix="upstream-gate-off-"))
    base2 = [sys.executable, str(ROOT / "main.py"), "requirements_sample",
             "--output-dir", str(ws2 / "out"), "--type", "web", "--install-deps", "never"]
    subprocess.run(base2, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
    subprocess.run(base2, cwd=str(ROOT), env=off, capture_output=True, text=True, timeout=300)
    rep2 = json.loads((ws2 / "out/.arc/factory-report.json").read_text(encoding="utf-8"))
    states2 = {r["req_id"]: r["state"] for r in rep2["results"]}
    check("T21e 关闭传播（对照臂）-> REQ-2 仍被执行，不再是 UPSTREAM_FAILED",
          states2.get("REQ-2") != "UPSTREAM_FAILED" and rep2.get("upstream_failed") == 0,
          str(states2))
    shutil.rmtree(ws, ignore_errors=True)
    shutil.rmtree(ws2, ignore_errors=True)


def t20_regression_does_not_consume_budget() -> None:
    """锁住已修 bug：回归回退后必须**继续**用剩余预算，而不是退出循环。

    序列：①通过但依赖未用 -> ②把测试改坏（触发回归）-> ③修好
    若回退后 break，则第③步永远不会发生，最终 FAILED。
    """
    result = run_scripted([IMPL_OK_NO_DEP, IMPL_BAD, IMPL_OK_REAL], max_repairs=3)
    check("T20a 回归后仍有预算继续，最终 PASSED",
          result.state == "PASSED", f"{result.state} note={result.note[:60]}")
    check("T20b 回归被记录（发生过但未终止）",
          bool(result.regressions)
          and result.regressions[0]["code"] == "IMPLEMENTATION_REGRESSION",
          f"regressions={len(result.regressions)} attempts={result.attempts}")


def t18_rewrite_boundary_blocks_test_writes() -> None:
    """重写阶段默认只改实现：写测试文件必须被拦截并记录。"""
    ws = dep_workspace()
    loop = build_loop(ws, FakeGenerator(write_script=[], design=sample_design(), plan=sample_plan()))
    from factory.models import RequirementResult
    res = RequirementResult(req_id="REQ-12", state="PENDING")
    files = [
        GeneratedFile(path=DOWNSTREAM_FILE, content="// impl"),
        GeneratedFile(path=DOWNSTREAM_TEST, content="// 试图改测试"),
    ]
    kept = loop._guard_implementation_files(
        Requirement(req_id="REQ-12", name="n", dependencies=("REQ-3",)), files, res)
    check("T18a 测试文件被拦截，实现放行",
          [f.path for f in kept] == [DOWNSTREAM_FILE], str([f.path for f in kept]))
    check("T18b 记录到 blocked_test_writes",
          bool(res.blocked_test_writes)
          and res.blocked_test_writes[0]["path"] == DOWNSTREAM_TEST,
          str(res.blocked_test_writes[:1]))
    shutil.rmtree(ws, ignore_errors=True)


def t19_declared_test_rewrite_allowed() -> None:
    """显式声明 test_rewrite_reasons 后放行，并记录理由。"""
    ws = dep_workspace()
    from factory.models import RequirementResult
    from factory.testplan import RequirementTestPlan as RTP, TestFileSpec as TFS

    loop = build_loop(ws, FakeGenerator(write_script=[], design=sample_design(), plan=sample_plan()))
    loop._plans["REQ-12"] = RTP(
        req_id="REQ-12",
        test_files=(TFS(path=DOWNSTREAM_TEST, type="unit"),),
        test_rewrite_reasons=((DOWNSTREAM_TEST, "测试断言写错了，需要修正"),),
    )
    res = RequirementResult(req_id="REQ-12", state="PENDING")
    kept = loop._guard_implementation_files(
        Requirement(req_id="REQ-12", name="n", dependencies=("REQ-3",)),
        [GeneratedFile(path=DOWNSTREAM_TEST, content="// 已声明的测试修正")], res)
    check("T19a 已声明 -> 放行", [f.path for f in kept] == [DOWNSTREAM_TEST])
    check("T19b 记录到 test_rewrite_reasons（含理由）",
          bool(res.test_rewrite_reasons)
          and "测试断言写错" in res.test_rewrite_reasons[0]["reason"],
          str(res.test_rewrite_reasons[:1]))

    # 关闭边界（对照臂）-> 允许改测试但记为对照臂放行
    loop2 = build_loop(ws, FakeGenerator(write_script=[], design=sample_design(), plan=sample_plan()),
                       enforce_impl_only_rewrite=False)
    res2 = RequirementResult(req_id="REQ-12", state="PENDING")
    kept2 = loop2._guard_implementation_files(
        Requirement(req_id="REQ-12", name="n", dependencies=("REQ-3",)),
        [GeneratedFile(path=DOWNSTREAM_TEST, content="// x")], res2)
    check("T19c 边界关闭（对照臂）-> 放行并标记", len(kept2) == 1
          and "对照臂" in res2.test_rewrite_reasons[0]["reason"],
          str(res2.test_rewrite_reasons[:1]))
    shutil.rmtree(ws, ignore_errors=True)


def t16_mock_violation_actually_blocks() -> None:
    """锁住已修 bug：只开 mock 检查时，未声明 mock 必须**阻断**，不能只记录。"""
    result = run_scripted([IMPL_OK_NO_DEP], test_src=MOCK_TEST_SRC,
                          enforce_dependency_usage=False, enforce_mock_check=True)
    check("T16 只开 mock 检查 -> 真的阻断（不是记了违规却 PASSED）",
          result.state == "FAILED"
          and any(v.get("verdict") == "UNVERIFIED_DEPENDENCY" for v in result.dependency_violations),
          f"{result.state} violations={[v.get('verdict') for v in result.dependency_violations]}")


def t17_bypass_escalation_blocks() -> None:
    """B 默认只警告；显式升级为阻断后必须真的阻断。"""
    warn = run_scripted([IMPL_OK_BYPASS], enforce_dependency_usage=False,
                        warn_injection_bypass=True, block_injection_bypass=False)
    block = run_scripted([IMPL_OK_BYPASS], enforce_dependency_usage=False,
                         warn_injection_bypass=True, block_injection_bypass=True)
    check("T17a 默认（警告级）不阻断", warn.state == "PASSED" and warn.dependency_injection_warnings)
    check("T17b 升级为阻断后真的阻断", block.state == "FAILED",
          f"{block.state} note={block.note[:60]}")


def t12_undeclared_mock_blocks() -> None:
    result = run_scripted([IMPL_OK_NO_DEP], test_src=MOCK_TEST_SRC)
    check("T12a 未声明 mock 上游 -> 阻断",
          result.state == "FAILED", result.note[:70])
    check("T12b 记为 UNVERIFIED_DEPENDENCY",
          any(v.get("verdict") == "UNVERIFIED_DEPENDENCY" for v in result.dependency_violations),
          str([v.get("verdict") for v in result.dependency_violations]))


def t13_declared_mock_allows() -> None:
    result = run_scripted([IMPL_OK_NO_DEP], test_src=MOCK_TEST_SRC,
                          mocked=(("REQ-3", "上游依赖外部服务，测试期必须 mock"),),
                          enforce_dependency_usage=False)
    check("T13 已声明 mock + 关闭方案1 -> 不因 mock 被阻断",
          result.state == "PASSED" and not result.dependency_violations,
          f"{result.state} violations={len(result.dependency_violations)}")


def t14_injection_bypass_warns_only() -> None:
    result = run_scripted([IMPL_OK_BYPASS], enforce_dependency_usage=False)
    check("T14a 注入旁路只警告、不阻断（本轮策略）",
          result.state == "PASSED", f"{result.state}")
    check("T14b 记录了 dependency_injection_warnings",
          bool(result.dependency_injection_warnings)
          and result.dependency_injection_warnings[0]["parameter"] == "summarizeItems",
          str([w.get("parameter") for w in result.dependency_injection_warnings]))


def t15_regression_reverts() -> None:
    # 第 1 次实现：测试通过但依赖不满足（触发重写）；第 2 次实现：把测试改坏
    result = run_scripted([IMPL_OK_NO_DEP, IMPL_BAD], max_repairs=2)
    check("T15a 检出 IMPLEMENTATION_REGRESSION",
          bool(result.regressions)
          and result.regressions[0]["code"] == "IMPLEMENTATION_REGRESSION",
          str([r.get("code") for r in result.regressions]))
    check("T15b 回退后测试恢复通过",
          bool(result.regressions) and "PASS" in result.regressions[0]["reverted_result"],
          result.regressions[0]["reverted_result"] if result.regressions else "")


def main() -> int:
    print("=" * 74)
    print("门禁行为断言测试（不调用模型）")
    print("=" * 74)
    for fn in (
        t1_initial_reject_then_retry,
        t2_reject_exhaustion,
        t3_feedback_type_regression,
        t4_rewrite_path_gets_whitelist,
        t5_meaningful_guard,
        t6_run_paths_whitelist,
        t7_dependency_not_used_blocks,
        t8_dependency_used_passes,
        t9_indirect_declaration_skips,
        t10_control_arm_off,
        t11_upstream_missing_blocks,
        t12_undeclared_mock_blocks,
        t13_declared_mock_allows,
        t14_injection_bypass_warns_only,
        t15_regression_reverts,
        t16_mock_violation_actually_blocks,
        t17_bypass_escalation_blocks,
        t18_rewrite_boundary_blocks_test_writes,
        t19_declared_test_rewrite_allowed,
        t20_regression_does_not_consume_budget,
        t21_upstream_failed_propagation,
        t22_test_source_is_given_to_implement,
        t23_uncollectable_test_rolls_back_to_test_stage,
        t24_feedback_includes_stderr,
        t25_vitest_prompt_forbids_hoisted_toplevel_ref,
        t26_root_container_is_not_designed,
        t27_json_parse_hardening_and_adaptive_retry,
        t28_verdict_judgment_is_unified,
        t29_test_file_drift_and_broken_priority,
        t30_esm_contract_and_static_syntax_check,
        t31_code_version_anchor,
        t32_missing_implementation_is_valid_red,
        t33_exit_code_contract,
        t36_dependency_audit_runs_even_when_tests_fail,
        t34_sdk_backend_parity_and_summary_arithmetic,
        t35_transient_model_failure_does_not_kill_requirement,
        t37_quota_exhaustion_is_terminal_and_fails_fast,
        t38_implementation_module_graph_audit,
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(f"{fn.__name__} 抛异常", False, f"{type(exc).__name__}: {exc}")
    print("=" * 74)
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 项失败：")
        for item in failed:
            print(f"  - {item}")
        return 1
    print(f"✅ 全部 {len(RESULTS)} 项断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
