"""方案 2（A + B）的四组配对对照实验。

=======================  三种逃逸形态  =======================
实测与推演出的三种「依赖空转」形态，覆盖从粗到细：

  ① mock_api        测试用 vi.mock('../src/services/summary') 把上游整个换掉
  ② param_injection 测试把假实现**当参数**传进去（chain5 里 REQ-12 的真实形态）
  ③ nominal_require 实现加了名义 require，但运行时仍优先用注入的回调
                    （上一轮 B+feedback 组的真实产物）

=======================  四组  =======================
  control          三项检查全关 -> 预期假阳性通过
  A                只开 mock 检查
  A+B              方案1 + mock 检查 + 注入旁路检查（B 仅警告）
  A+B+feedback     A+B，并把拒绝理由回传给模型重写（需要模型调用）

关键指标：是否阻断、判定码、重写后是否真实调用上游、重写后测试是否通过。
=============================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.config import FactoryConfig  # noqa: E402
from factory.generator import LLMGenerator  # noqa: E402
from factory.llm import ModelClient  # noqa: E402
from factory.loop import TddLoop  # noqa: E402
from factory.models import (  # noqa: E402
    DesignPlan,
    GeneratedFile,
    InterfaceSpec,
    Requirement,
    TestSpec,
)
from factory.testaudit import DEP_USED, audit_dependency_usage, extract_exports  # noqa: E402
from factory.testplan import RequirementTestPlan, TestFileSpec  # noqa: E402
from factory.testrunner import NodeTestRunner  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")

UPSTREAM_FILE = "backend/src/services/summary.js"
UPSTREAM_SRC = (
    "'use strict';\n"
    "function summarizeItems(items = [], threshold = 10) {\n"
    "  const list = Array.isArray(items) ? items : [];\n"
    "  return { total: list.length,\n"
    "           totalQuantity: list.reduce((s, x) => s + (x.quantity || 0), 0),\n"
    "           lowStock: list.filter((x) => (x.quantity || 0) < threshold).length };\n"
    "}\n"
    "module.exports = { summarizeItems };\n"
)
DOWNSTREAM_FILE = "backend/src/services/report.js"
DOWNSTREAM_TEST = "backend/tests/req12.report.test.js"

# ---- 三种逃逸形态 ---------------------------------------------------------

# ① 测试层 mock 上游
TEST_MOCK_API = (
    "const vi = { mock: (...a) => {} };\n"
    "vi.mock('../src/services/summary', () => ({ summarizeItems: () => ({ total: 99 }) }));\n"
    "const t=require('node:test'),a=require('node:assert');\n"
    "const {buildReport}=require('../src/services/report');\n"
    "t('REQ-12 报表结构完整',()=>{\n"
    "  const r=buildReport([{quantity:5}]);\n"
    "  a.deepStrictEqual(Object.keys(r).sort(),['movements','summary']);\n"
    "});\n"
)
# ② 测试层参数注入假实现
TEST_PARAM_INJECTION = (
    "const t=require('node:test'),a=require('node:assert');\n"
    "const {buildReport}=require('../src/services/report');\n"
    "t('REQ-12 报表结构完整',()=>{\n"
    "  const fakeSummarize = () => ({ total: 99 });\n"
    "  const r=buildReport([{quantity:5}], [], fakeSummarize);\n"
    "  a.deepStrictEqual(Object.keys(r).sort(),['movements','summary']);\n"
    "});\n"
)

IMPL_NO_DEP = (
    "'use strict';\n"
    "function buildReport(items = [], movements = [], summarizeItems) {\n"
    "  const summary = typeof summarizeItems === 'function' ? summarizeItems(items) : {};\n"
    "  return { summary, movements };\n"
    "}\n"
    "module.exports = { buildReport };\n"
)
# ③ 名义 require + 仍优先用注入回调（上一轮 B+feedback 的真实产物形态）
IMPL_NOMINAL_REQUIRE = (
    "'use strict';\n"
    "const { summarizeItems: upstreamSummarizeItems } = require('./summary');\n"
    "function buildReport(items = [], movements = [], summarizeItems) {\n"
    "  let summary;\n"
    "  if (typeof summarizeItems === 'function') { summary = summarizeItems(items); }\n"
    "  else { summary = upstreamSummarizeItems(items); }\n"
    "  return { summary, movements };\n"
    "}\n"
    "module.exports = { buildReport };\n"
)

FORMS = {
    "mock_api":        {"test": TEST_MOCK_API,        "impl": IMPL_NO_DEP},
    "param_injection": {"test": TEST_PARAM_INJECTION, "impl": IMPL_NO_DEP},
    "nominal_require": {"test": TEST_PARAM_INJECTION, "impl": IMPL_NOMINAL_REQUIRE},
}

ARMS = {
    #                       方案1   2-A     2-B(警告)  2-B(阻断)
    "control": {"enforce_dependency_usage": False, "enforce_mock_check": False,
                "warn_injection_bypass": False, "block_injection_bypass": False},
    "A":       {"enforce_dependency_usage": False, "enforce_mock_check": True,
                "warn_injection_bypass": False, "block_injection_bypass": False},
    "A+B":     {"enforce_dependency_usage": True,  "enforce_mock_check": True,
                "warn_injection_bypass": True,  "block_injection_bypass": False},
}
# 假设性升级臂：把 B 从警告升为阻断，回答"若升级，模型能否修好"
ESCALATED = {"enforce_dependency_usage": True, "enforce_mock_check": True,
             "warn_injection_bypass": True, "block_injection_bypass": True}


class NullStore:
    def __getattr__(self, _n):  # noqa: ANN204
        return lambda *a, **k: None


@dataclass
class Trial:
    form: str
    arm: str
    status: str = ""
    verdicts: list[str] = field(default_factory=list)
    violation_count: int = 0
    bypass_warnings: int = 0
    rewrote: bool = False
    upstream_called_after: bool = False
    test_passed_after: bool = False
    note: str = ""
    error: str = ""


class SeededGenerator:
    def __init__(self, inner, *, test_src: str, seed_impl: str):
        self.inner = inner
        self.test_src = test_src
        self.seed_impl = seed_impl
        self.calls: list[list[str]] = []
        self.last_impl = seed_impl

    def design(self, requirement):  # noqa: ANN001
        return DesignPlan(
            req_id=requirement.req_id, summary="报表装配",
            interfaces=(InterfaceSpec(interface_id="REQ-12.SVC.Report",
                                      req_ids=(requirement.req_id,), type="db",
                                      content="buildReport(...)"),),
            tests=(TestSpec(test_id="T", req_id=requirement.req_id, type="unit", intent="结构完整"),),
        )

    def plan_tests(self, requirement, plan, plan_feedback=""):  # noqa: ANN001
        return RequirementTestPlan(
            req_id=requirement.req_id,
            test_files=(TestFileSpec(path=DOWNSTREAM_TEST, type="unit", covers=()),),
            scenarios=(),
        )

    def write_tests(self, requirement, plan, weak_feedback="", allowed_paths=()):  # noqa: ANN001
        return [GeneratedFile(path=DOWNSTREAM_TEST, content=self.test_src)]

    def implement(self, requirement, plan, failures, test_context: str = ""):  # noqa: ANN001
        self.calls.append(list(failures))
        if len(self.calls) == 1 or self.inner is None:
            self.last_impl = self.seed_impl
            return [GeneratedFile(path=DOWNSTREAM_FILE, content=self.seed_impl)]
        for item in self.inner.implement(requirement, plan, failures):
            if item.path.lstrip("./") == DOWNSTREAM_FILE and item.mode == "write":
                self.last_impl = item.content
        return [GeneratedFile(path=DOWNSTREAM_FILE, content=self.last_impl)]


def build_workspace() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="plan2-exp-"))
    (ws / "backend/src/services").mkdir(parents=True)
    (ws / "backend/tests").mkdir(parents=True)
    (ws / UPSTREAM_FILE).write_text(UPSTREAM_SRC, encoding="utf-8")
    return ws


def run_trial(form: str, arm: str, *, inner=None, max_repairs: int = 0) -> Trial:
    trial = Trial(form=form, arm=arm)
    ws = build_workspace()
    try:
        gen = SeededGenerator(inner, test_src=FORMS[form]["test"], seed_impl=FORMS[form]["impl"])
        config = FactoryConfig.from_env(**(ESCALATED if arm == "ESCALATED" else ARMS[arm]))
        config.max_repairs = max_repairs
        loop = TddLoop(store=NullStore(), generator=gen,  # type: ignore[arg-type]
                       runner=NodeTestRunner(ws, timeout_s=90), config=config, output_dir=ws)
        loop._impl_files["REQ-3"] = [UPSTREAM_FILE]

        result = loop.run(Requirement(req_id="REQ-12", name="库存周转报表", dependencies=("REQ-3",)))
        trial.status = result.state
        trial.note = result.note[:100]
        trial.verdicts = [v.get("verdict", "?") for v in result.dependency_violations]
        trial.violation_count = len(result.dependency_violations)
        trial.bypass_warnings = len(result.dependency_injection_warnings)
        trial.rewrote = len(gen.calls) > 1

        usage = audit_dependency_usage(
            ws, downstream="REQ-12", upstream="REQ-3",
            downstream_files=[DOWNSTREAM_FILE], upstream_files=[UPSTREAM_FILE])
        trial.upstream_called_after = usage.verdict == DEP_USED
        outcome = NodeTestRunner(ws, timeout_s=90).run([DOWNSTREAM_TEST[len("backend/"):]])
        trial.test_passed_after = outcome.passed
    except Exception as exc:  # noqa: BLE001
        trial.error = f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    return trial


def main() -> int:
    parser = argparse.ArgumentParser(description="方案 2（A+B）四组配对对照")
    parser.add_argument("--json", default="experiment_plan2_result.json")
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()

    inner = None
    if not args.no_llm:
        client = ModelClient()
        print(f"模型: {client.model}   后端: {client.backend()}")
        if client.is_available():
            inner = LLMGenerator(client, "node")
        else:
            print("⚠️ 模型不可用，A+B+feedback 组跳过")
    print(f"上游 REQ-3 导出: {sorted(extract_exports(UPSTREAM_SRC))}\n")

    trials: list[Trial] = []
    for form in FORMS:
        for arm in ARMS:
            trials.append(run_trial(form, arm))

    # 反馈臂：
    #   A+B+feedback          —— A+B 常态（B 只警告），预期不触发重写
    #   A+B(escalated)+feedback —— 假设把 B 升为阻断，看模型能否修好
    if inner is not None:
        trials.append(run_trial("nominal_require", "A+B", inner=inner, max_repairs=1))
        trials[-1].arm = "A+B+feedback"
        trials.append(run_trial("nominal_require", "ESCALATED",
                                inner=inner, max_repairs=1))
        trials[-1].arm = "A+B(escalated)+feedback"

    print("=" * 104)
    print(f"{'形态':<18}{'组':<14}{'状态':<9}{'违规':<6}{'判定':<44}{'旁路警告':<9}{'重写':<6}{'重写后调用上游'}")
    print("=" * 104)
    for t in trials:
        print(f"{t.form:<18}{t.arm:<14}{t.status:<9}{t.violation_count:<6}"
              f"{(','.join(t.verdicts) or '-'):<44}{t.bypass_warnings:<9}"
              f"{str(t.rewrote):<6}{t.upstream_called_after}"
              + (f"  [{t.error}]" if t.error else ""))
    print("=" * 104)

    print("\n检测矩阵（✔=阻断，W=仅警告，·=未检出）：")
    forms = list(FORMS)
    heads = ("control", "A", "A+B", "A+B+feedback", "A+B(escalated)+feedback")
    print(f"{'形态':<18}" + "".join(f"{a:<26}" for a in heads))
    for form in forms:
        cells = []
        for arm in heads:
            t = next((x for x in trials if x.form == form and x.arm == arm), None)
            if t is None:
                cells.append("-")
            elif t.status == "FAILED":
                cells.append("✔ 阻断")
            elif t.rewrote and not t.bypass_warnings and t.upstream_called_after:
                cells.append("✔ 重写后修复")
            elif t.bypass_warnings:
                cells.append("W 仅警告")
            else:
                cells.append("· 未检出")
        print(f"{form:<18}" + "".join(f"{c:<26}" for c in cells))

    (ROOT / args.json).write_text(
        json.dumps({"trials": [asdict(t) for t in trials]}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
