"""E1–E3 配对对照：重写预算 × 重写边界。

=======================  背景  =======================
三条检测器已能看见所有已知逃逸形态（mock_api / param_injection / nominal_require）。
剩下的问题不是「看不见」，而是「看见了但修不好」——
上一轮里模型重写后确实开始调用上游，却把测试改坏了，被回归检查回退。
这是**收敛问题**，不是检测问题。

=======================  四组（同一起点）  =======================
| 组      | max_repairs | 检测器              | 重写边界 |
|---------|-------------|---------------------|----------|
| control | 1           | A+B 升级为阻断       | 可改测试 |
| E1      | 3           | A+B 升级为阻断       | 可改测试 |
| E2      | 5           | A+B 升级为阻断       | 可改测试 |
| E3      | 3           | A+B 升级为阻断       | 只改实现 |

起点统一为 **nominal_require** 形态：实现加了名义 require，但运行时仍优先用
注入的回调。方案1 放行（require 存在）、A 放行（无 mock API），只有升级后的 B 能拦住。

=======================  四项指标  =======================
  最终 PASSED 率 / 重写后真实调用上游的比例 / 重写后测试被改坏的比例 / token 增量
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

TEST_SRC = (
    "const t=require('node:test'),a=require('node:assert');\n"
    "const {buildReport}=require('../src/services/report');\n"
    "t('REQ-12 报表结构完整',()=>{\n"
    "  const fakeSummarize = () => ({ total: 99 });\n"
    "  const r=buildReport([{quantity:5}], [], fakeSummarize);\n"
    "  a.deepStrictEqual(Object.keys(r).sort(),['movements','summary']);\n"
    "});\n"
)
# 起点：名义 require + 仍优先用注入回调
SEED_IMPL = (
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

# 检测器统一：A+B 升级为阻断
DETECTORS = {
    "enforce_dependency_usage": True,
    "enforce_mock_check": True,
    "warn_injection_bypass": True,
    "block_injection_bypass": True,
}

ARMS = (
    ("control", 1, False),
    ("E1", 3, False),
    ("E2", 5, False),
    ("E3", 3, True),    # 只改实现
)


class NullStore:
    def __getattr__(self, _n):  # noqa: ANN204
        return lambda *a, **k: None


@dataclass
class Trial:
    arm: str
    rep: int = 0
    status: str = ""
    rewrites: int = 0
    upstream_called: bool = False
    regression: bool = False
    blocked_test_writes: int = 0
    last_impl: str = ""
    first_failure: str = ""
    tokens: int = 0
    calls: int = 0
    note: str = ""
    error: str = ""


class PassthroughGenerator:
    """第 1 次 implement 返回种子；之后**原样**返回模型给的所有文件，交给护栏过滤。

    这一点很关键：若在这里预先过滤掉测试文件，就看不出「可改测试」与
    「只改实现」两种边界的差异了。
    """

    def __init__(self, inner: LLMGenerator | None, *, test_src: str, seed_impl: str):
        self.inner = inner
        self.test_src = test_src
        self.seed_impl = seed_impl
        self.calls: list[list[str]] = []

    def design(self, requirement):  # noqa: ANN001
        return DesignPlan(
            req_id=requirement.req_id, summary="报表装配",
            interfaces=(InterfaceSpec(interface_id="REQ-12.SVC.Report",
                                      req_ids=(requirement.req_id,), type="db",
                                      content="buildReport(items, movements, summarizeItems)"),),
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
            return [GeneratedFile(path=DOWNSTREAM_FILE, content=self.seed_impl)]
        files = self.inner.implement(requirement, plan, failures)
        for item in files:
            if item.path.lstrip("./") == DOWNSTREAM_FILE and item.mode == "write":
                self._last_seen_impl = item.content
        return files


def build_workspace() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="e13-"))
    (ws / "backend/src/services").mkdir(parents=True)
    (ws / "backend/tests").mkdir(parents=True)
    (ws / UPSTREAM_FILE).write_text(UPSTREAM_SRC, encoding="utf-8")
    return ws


def run_trial(arm: str, max_repairs: int, impl_only: bool, rep: int,
              inner, client: ModelClient | None) -> Trial:
    trial = Trial(arm=arm, rep=rep)
    ws = build_workspace()
    before = client.stats.snapshot() if client else {}
    try:
        gen = PassthroughGenerator(inner, test_src=TEST_SRC, seed_impl=SEED_IMPL)
        config = FactoryConfig.from_env(
            **DETECTORS, enforce_impl_only_rewrite=impl_only)
        config.max_repairs = max_repairs
        loop = TddLoop(store=NullStore(), generator=gen,  # type: ignore[arg-type]
                       runner=NodeTestRunner(ws, timeout_s=120), config=config, output_dir=ws)
        loop._impl_files["REQ-3"] = [UPSTREAM_FILE]

        result = loop.run(Requirement(req_id="REQ-12", name="库存周转报表",
                                      dependencies=("REQ-3",)))
        trial.status = result.state
        trial.rewrites = max(0, len(gen.calls) - 1)
        trial.regression = bool(result.regressions)
        trial.blocked_test_writes = len(result.blocked_test_writes)
        trial.note = result.note[:100]

        trial.last_impl = getattr(gen, "_last_seen_impl", "")[:1800]
        for _r in result.regressions:
            trial.first_failure = str(_r.get("after", ""))[:400]
            break
        if not trial.first_failure:
            trial.first_failure = result.note[:400]
        usage = audit_dependency_usage(
            ws, downstream="REQ-12", upstream="REQ-3",
            downstream_files=[DOWNSTREAM_FILE], upstream_files=[UPSTREAM_FILE])
        trial.upstream_called = usage.verdict == DEP_USED
    except Exception as exc:  # noqa: BLE001
        trial.error = f"{type(exc).__name__}: {exc}"
    finally:
        if client:
            delta = client.stats.delta(before)
            trial.tokens = delta.get("total_tokens", 0)
            trial.calls = delta.get("calls", 0)
        shutil.rmtree(ws, ignore_errors=True)
    return trial


def main() -> int:
    parser = argparse.ArgumentParser(description="E1-E3 重写预算 × 边界 配对对照")
    parser.add_argument("--json", default="experiment_e13_result.json")
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--arms", default="", help="只跑指定组，逗号分隔")
    args = parser.parse_args()

    client = ModelClient()
    print(f"模型: {client.model}   后端: {client.backend()}   每次重复: {args.reps} 轮")
    if not client.is_available():
        print("❌ 模型不可用，E1-E3 需要真实重写，无法运行")
        return 1
    inner = LLMGenerator(client, "node")
    print(f"上游 REQ-3 导出: {sorted(extract_exports(UPSTREAM_SRC))}")
    print("起点形态: nominal_require（名义 require + 仍优先用注入回调）\n")

    wanted = {a.strip() for a in args.arms.split(",") if a.strip()}
    trials: list[Trial] = []
    for arm, max_repairs, impl_only in ARMS:
        if wanted and arm not in wanted:
            continue
        for rep in range(args.reps):
            t = run_trial(arm, max_repairs, impl_only, rep, inner, client)
            trials.append(t)
            print(f"  {arm:<8} rep{rep}  {t.status:<7} 重写={t.rewrites} "
                  f"调用上游={t.upstream_called} 回归={t.regression} "
                  f"拦截测试写={t.blocked_test_writes} token={t.tokens}"
                  + (f"  [{t.error}]" if t.error else ""))

    print("\n" + "=" * 100)
    print(f"{'组':<9}{'预算':<6}{'边界':<11}{'PASSED率':<11}{'调用上游率':<12}"
          f"{'测试改坏率':<12}{'token合计':<12}{'token/次':<11}{'重写次数'}")
    print("=" * 100)
    summary = {}
    for arm, max_repairs, impl_only in ARMS:
        rows = [t for t in trials if t.arm == arm]
        n = len(rows) or 1
        passed = sum(1 for t in rows if t.status == "PASSED")
        called = sum(1 for t in rows if t.upstream_called)
        broken = sum(1 for t in rows if t.regression)
        tokens = sum(t.tokens for t in rows)
        rewrites = sum(t.rewrites for t in rows)
        entry = {
            "max_repairs": max_repairs,
            "only_impl": impl_only,
            "trials": len(rows),
            "passed_rate": f"{passed}/{len(rows)}",
            "upstream_called_rate": f"{called}/{len(rows)}",
            "tests_broken_rate": f"{broken}/{len(rows)}",
            "token_total": tokens,
            "token_per_trial": tokens // n,
            "rewrites_total": rewrites,
            "statuses": [t.status for t in rows],
        }
        summary[arm] = entry
        print(f"{arm:<9}{max_repairs:<6}{('只改实现' if impl_only else '可改测试'):<11}"
              f"{entry['passed_rate']:<11}{entry['upstream_called_rate']:<12}"
              f"{entry['tests_broken_rate']:<12}{tokens:<12}{tokens // n:<11}{rewrites}")

    base = summary["control"]["token_per_trial"] or 1
    print("\ntoken 增量（相对 control）:")
    for arm in summary:
        delta = summary[arm]["token_per_trial"] - base
        pct = delta / base * 100
        print(f"  {arm:<9} {delta:+7d}  ({pct:+.1f}%)")

    (ROOT / args.json).write_text(
        json.dumps({"trials": [asdict(t) for t in trials], "summary": summary},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
