"""B（依赖使用审计）的三组配对对照实验。

=======================  实验设计  =======================
复现 chain5 里观察到的真实病理（REQ-12 声明依赖 REQ-3，却用参数注入绕过）：

  起点（三组共用）：下游 REQ-12 的实现是参数注入式，**不 import 上游**；
                    上游 REQ-3 的模块已存在于工作区，所以"真实调用"是可达的。

  ① control    enforce_dependency_usage=False
               预期：假阳性通过（这正是 chain5 里 REQ-12 的结果）
  ② B          enforce=True, max_repairs=0（不给重写机会）
               预期：阻断，判 DEPENDENCY_NOT_USED
  ③ B+feedback enforce=True, max_repairs=1（把拒绝理由回传给模型重写）
               预期：重写后真实调用上游 -> DEPENDENCY_USED

三组跑**同一份起点、同一个 TddLoop**，只切换依赖门禁配置与是否给重写机会。

关键指标：
  dependency_violations 计数
  重写后是否真实调用上游（verdict == DEPENDENCY_USED）
=========================================================
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
from factory.testaudit import DEP_USED, extract_exports  # noqa: E402
from factory.testplan import RequirementTestPlan, TestFileSpec  # noqa: E402
from factory.testrunner import NodeTestRunner  # noqa: E402

logging.basicConfig(level=logging.ERROR, format="%(levelname)-7s %(message)s")

UPSTREAM_FILE = "backend/src/services/summary.js"
UPSTREAM_SRC = (
    "'use strict';\n"
    "// REQ-3：库存汇总统计\n"
    "function summarizeItems(items = [], threshold = 10) {\n"
    "  const list = Array.isArray(items) ? items : [];\n"
    "  return {\n"
    "    total: list.length,\n"
    "    totalQuantity: list.reduce((s, x) => s + (x.quantity || 0), 0),\n"
    "    lowStock: list.filter((x) => (x.quantity || 0) < threshold).length,\n"
    "  };\n"
    "}\n"
    "module.exports = { summarizeItems };\n"
)

DOWNSTREAM_FILE = "backend/src/services/report.js"
DOWNSTREAM_TEST = "backend/tests/req12.report.test.js"

DOWNSTREAM_TEST_SRC = (
    "const t=require('node:test'),a=require('node:assert');\n"
    "const {buildReport}=require('../src/services/report');\n"
    "t('REQ-12 报表结构完整',()=>{\n"
    "  const r=buildReport([{quantity:5}]);\n"
    "  a.deepStrictEqual(Object.keys(r).sort(),['movements','summary']);\n"
    "});\n"
)

# 起点：参数注入式，完全不 import 上游（chain5 里 REQ-12 的真实形态）
SEED_IMPL = (
    "'use strict';\n"
    "function buildReport(items = [], movements = [], summarizeItems) {\n"
    "  const summary = typeof summarizeItems === 'function' ? summarizeItems(items) : {};\n"
    "  return { summary, movements };\n"
    "}\n"
    "module.exports = { buildReport };\n"
)


class NullStore:
    def __getattr__(self, _n):  # noqa: ANN204
        return lambda *a, **k: None


@dataclass
class Trial:
    arm: str
    status: str = ""
    verdicts: list[str] = field(default_factory=list)
    violation_count: int = 0
    rewrite_called: bool = False
    upstream_called_after_rewrite: bool = False
    impl_snippet: str = ""
    note: str = ""
    error: str = ""


class SeededGenerator:
    """第 1 次 implement 返回注入式种子；第 2 次（若允许）交给真实模型重写。"""

    name = "seeded"

    def __init__(self, inner: LLMGenerator | None, seed: str) -> None:
        self.inner = inner
        self.seed = seed
        self.implement_calls: list[list[str]] = []
        self.last_impl = seed

    def design(self, requirement):  # noqa: ANN001
        return DesignPlan(
            req_id=requirement.req_id, summary="报表装配",
            interfaces=(InterfaceSpec(interface_id="REQ-12.SVC.Report",
                                      req_ids=(requirement.req_id,), type="db",
                                      content="buildReport(items, movements, summarizeItems)"),),
            tests=(TestSpec(test_id="REQ-12.TEST.report", req_id=requirement.req_id,
                            type="unit", intent="报表结构完整"),),
        )

    def plan_tests(self, requirement, plan, plan_feedback=""):  # noqa: ANN001
        return RequirementTestPlan(
            req_id=requirement.req_id,
            test_files=(TestFileSpec(path=DOWNSTREAM_TEST, type="unit", covers=()),),
            scenarios=(),
        )

    def write_tests(self, requirement, plan, weak_feedback="", allowed_paths=()):  # noqa: ANN001
        return [GeneratedFile(path=DOWNSTREAM_TEST, content=DOWNSTREAM_TEST_SRC)]

    def implement(self, requirement, plan, failures, test_context: str = ""):  # noqa: ANN001
        self.implement_calls.append(list(failures))
        if len(self.implement_calls) == 1 or self.inner is None:
            self.last_impl = self.seed
            return [GeneratedFile(path=DOWNSTREAM_FILE, content=self.seed)]
        files = self.inner.implement(requirement, plan, failures)
        for item in files:
            if item.path.lstrip("./") == DOWNSTREAM_FILE and item.mode == "write":
                self.last_impl = item.content
        return files


def build_workspace() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="dep-exp-"))
    (ws / "backend/src/services").mkdir(parents=True)
    (ws / "backend/tests").mkdir(parents=True)
    # 上游 REQ-3 已存在（模拟它已 PASSED），所以"真实调用"是可达的
    (ws / UPSTREAM_FILE).write_text(UPSTREAM_SRC, encoding="utf-8")
    return ws


def run_arm(arm: str, *, enforce: bool, max_repairs: int, inner: LLMGenerator | None) -> Trial:
    trial = Trial(arm=arm)
    ws = build_workspace()
    try:
        gen = SeededGenerator(inner, SEED_IMPL)
        config = FactoryConfig.from_env(enforce_dependency_usage=enforce)
        config.max_repairs = max_repairs
        loop = TddLoop(store=NullStore(), generator=gen,  # type: ignore[arg-type]
                       runner=NodeTestRunner(ws, timeout_s=90), config=config, output_dir=ws)
        loop._impl_files["REQ-3"] = [UPSTREAM_FILE]   # 上游已跑完

        req = Requirement(req_id="REQ-12", name="库存周转报表", dependencies=("REQ-3",))
        result = loop.run(req)

        trial.status = result.state
        trial.note = result.note[:120]
        trial.violation_count = len(result.dependency_violations)
        trial.verdicts = [v["verdict"] for v in result.dependency_violations]
        trial.rewrite_called = len(gen.implement_calls) > 1
        trial.impl_snippet = gen.last_impl[:400]

        # 重写后是否真实调用上游：用审计函数独立复核（不依赖门禁内部状态）
        from factory.testaudit import audit_dependency_usage  # noqa: PLC0415

        usage = audit_dependency_usage(
            ws, downstream="REQ-12", upstream="REQ-3",
            downstream_files=[DOWNSTREAM_FILE], upstream_files=[UPSTREAM_FILE],
        )
        trial.upstream_called_after_rewrite = usage.verdict == DEP_USED
    except Exception as exc:  # noqa: BLE001
        trial.error = f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    return trial


def main() -> int:
    parser = argparse.ArgumentParser(description="B 依赖门禁三组配对对照")
    parser.add_argument("--json", default="experiment_dependency_result.json")
    parser.add_argument("--no-llm", action="store_true", help="跳过需要模型的 B+feedback 臂")
    args = parser.parse_args()

    inner = None
    if not args.no_llm:
        client = ModelClient()
        print(f"模型: {client.model}   后端: {client.backend()}")
        if not client.is_available():
            print("❌ 模型不可用，B+feedback 臂无法运行（可用 --no-llm 只跑前两臂）")
            return 1
        inner = LLMGenerator(client, "node")

    print("\n上游 REQ-3 的导出:", sorted(extract_exports(UPSTREAM_SRC)))
    print("起点实现: 参数注入式，不 import 上游\n")

    trials: list[Trial] = []
    trials.append(run_arm("control", enforce=False, max_repairs=0, inner=None))
    trials.append(run_arm("B", enforce=True, max_repairs=0, inner=None))
    if inner is not None:
        trials.append(run_arm("B_feedback", enforce=True, max_repairs=1, inner=inner))

    print("=" * 84)
    print(f"{'组':<12}{'状态':<10}{'违规数':<9}{'判定':<28}{'重写':<7}{'重写后真实调用上游'}")
    print("=" * 84)
    for t in trials:
        print(f"{t.arm:<12}{t.status:<10}{t.violation_count:<9}"
              f"{','.join(t.verdicts) or '-':<28}{str(t.rewrite_called):<7}"
              f"{t.upstream_called_after_rewrite}"
              + (f"   [{t.error}]" if t.error else ""))
    print("=" * 84)

    b = next(t for t in trials if t.arm == "B")
    bf = next((t for t in trials if t.arm == "B_feedback"), None)
    control = next(t for t in trials if t.arm == "control")
    print(f"\n① control 假阳性复现: {'是' if control.status == 'PASSED' else '否'}")
    print(f"② B 阻断: {'是' if b.status == 'FAILED' and b.violation_count else '否'}（{','.join(b.verdicts) or '-'}）")
    if bf:
        print(f"③ B+feedback 重写后真实调用上游: {'是' if bf.upstream_called_after_rewrite else '否'}")

    (ROOT / args.json).write_text(
        json.dumps({"trials": [asdict(t) for t in trials]}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
