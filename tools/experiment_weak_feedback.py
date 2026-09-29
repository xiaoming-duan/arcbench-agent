"""度量实验：WEAK_TEST 重写反馈的措辞，是否影响"一次重写就修好"的概率？

=======================  实验设计  =======================
被测断言（上一轮提出，尚未证实）：
  "把重写反馈从『你的测试在没有实现的情况下通过了』，
   换成『你的测试没有 import src/ 下任何模块』，
   一次重写就修好的概率会高得多。"

处理组：
  basic        —— 只说测试通过了（原实现）
  import_aware —— 附加静态 import 诊断（本次新增）

方法：**配对对照**
  1. 对每个需求，先用一个"故意不 import 实现"的提示造出**空转测试**作为统一起点。
     自然发生率只有 ~12.5%，不诱导的话拿不到足够样本。
  2. 校验该起点确实是空转的（在无实现的工作区里跑，必须通过），否则丢弃该需求。
  3. 同一份起点测试，分别用两种反馈各重写 N 次；重写后测试**必须失败**（RED）才算成功。

度量指标：
  主指标  one_shot_red_rate —— 一次重写后达成 RED 的比例
  次指标  imports_impl_rate —— 重写后的测试是否真的引用了实现模块（机制验证）
  配对检验 McNemar / Fisher 精确检验

⚠️ 本实验测的是"给定空转测试，反馈措辞的因果效应"。
   它不测量空转测试的自然发生率（那是 RED 门禁在线上统计的事）。
=========================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.adapter import _adapt  # noqa: E402
from factory.config import FactoryConfig  # noqa: E402
from factory.generator import DesignPlan, LLMGenerator, TEMPLATE_CONVENTIONS  # noqa: E402
from factory.llm import ModelClient  # noqa: E402
from factory.loop import TddLoop  # noqa: E402
from factory.models import GeneratedFile, TestOutcome  # noqa: E402
from factory.testaudit import audit_tests, is_test_file  # noqa: E402
from factory.testrunner import NodeTestRunner  # noqa: E402
from factory.workspace import apply_generated_files, copy_template_contents, load_requirements_raw  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("experiment")

VARIANTS = ("basic", "import_aware")

_SEED_SYSTEM = """你是一名测试工程师。只输出 JSON 对象，结构：
{"files":[{"path":"backend/tests/xxx.test.js","content":"完整文件内容","mode":"write"}]}
不要输出 JSON 以外的任何内容。"""

_SEED_INSTRUCTION = """\
请为下面的需求写一个测试文件，使用 Node 内置测试运行器：
`const test = require('node:test'); const assert = require('node:assert');`
文件放在 backend/tests/ 下。

★ 特别要求（本次任务就是要这样的测试）：
  - **不要** require / import 任何实现模块（不要出现 ../src/... 这样的路径）；
  - 在测试文件内部自己定义一份 mock 数据 / 复刻一份实现，然后断言这份自造的数据；
  - 让测试看起来完整、专业、覆盖全面。
"""


@dataclass
class Trial:
    req_id: str
    variant: str
    rep: int
    red_achieved: bool = False
    imports_impl: bool = False
    audit_verdict: str = ""
    elapsed_s: float = 0.0
    error: str = ""


@dataclass
class ExperimentResult:
    model: str
    trials: list[Trial] = field(default_factory=list)

    def rate(self, variant: str, key: str) -> tuple[int, int]:
        rows = [t for t in self.trials if t.variant == variant]
        hits = sum(1 for t in rows if getattr(t, key))
        return hits, len(rows)

    def to_dict(self) -> dict:
        out = {
            "model": self.model,
            "trials": [asdict(t) for t in self.trials],
            "summary": {},
        }
        for variant in VARIANTS:
            red_hit, red_n = self.rate(variant, "red_achieved")
            imp_hit, imp_n = self.rate(variant, "imports_impl")
            out["summary"][variant] = {
                "one_shot_red": f"{red_hit}/{red_n}",
                "imports_impl": f"{imp_hit}/{imp_n}",
            }
        out["summary"]["fisher_p"] = fisher_exact(
            self.rate("import_aware", "red_achieved"), self.rate("basic", "red_achieved")
        )
        return out


def fisher_exact(a: tuple[int, int], b: tuple[int, int]) -> float:
    """双尾 Fisher 精确检验，返回 p 值。a=(命中, 总数), b=(命中, 总数)。"""
    a_hit, a_n = a
    b_hit, b_n = b
    a_miss, b_miss = a_n - a_hit, b_n - b_hit
    n = a_n + b_n
    if n == 0:
        return 1.0

    def prob(x: int) -> float:
        # 固定边际下，处理组命中 x 的超几何概率
        return (
            math.comb(a_n, x)
            * math.comb(b_n, a_hit + b_hit - x)
            / math.comb(n, a_hit + b_hit)
        )

    observed = prob(a_hit)
    total = 0.0
    lo = max(0, a_hit + b_hit - b_n)
    hi = min(a_n, a_hit + b_hit)
    for x in range(lo, hi + 1):
        p = prob(x)
        if p <= observed + 1e-12:
            total += p
    return min(1.0, total)


# ---------------------------------------------------------------------------
# 工作区与模型调用
# ---------------------------------------------------------------------------


def fresh_workspace(base: Path, name: str, template_dir: Path) -> Path:
    workspace = base / name
    if workspace.exists():
        shutil.rmtree(workspace)
    copy_template_contents(template_dir, workspace)
    return workspace


def generate_vacuous_seed(gen: LLMGenerator, requirement, target: str) -> GeneratedFile | None:
    """诱导模型产出一个不 import 实现的空转测试。"""
    user = f"{TEMPLATE_CONVENTIONS}\n\n{gen._requirement_brief(requirement)}\n\n{_SEED_INSTRUCTION}"
    payload = gen.client.complete_json(system=_SEED_SYSTEM, user=user)
    for entry in payload.get("files", []) or []:
        if isinstance(entry, dict) and entry.get("path"):
            return GeneratedFile(
                path=str(entry["path"]),
                content=str(entry.get("content", "")),
                mode=str(entry.get("mode", "write")),
                marker=entry.get("marker"),
            )
    return None


def target_test_path(requirement) -> str:
    if requirement.tests and requirement.tests[0].file_path:
        return requirement.tests[0].file_path
    return f"backend/tests/{requirement.req_id.lower()}.test.js"


def run_trial(
    *,
    requirement,
    plan: DesignPlan,
    seed: GeneratedFile,
    variant: str,
    rep: int,
    gen: LLMGenerator,
    config: FactoryConfig,
    base: Path,
    template_dir: Path,
) -> Trial:
    trial = Trial(req_id=requirement.req_id, variant=variant, rep=rep)
    name = f"{requirement.req_id}-{variant}-r{rep}"
    try:
        workspace = fresh_workspace(base, name, template_dir)
        # runner 的 cwd 绑定工作区，必须按 trial 构造
        runner = NodeTestRunner(workspace, timeout_s=config.test_timeout_s)
        seed_dest = workspace / seed.path
        seed_dest.parent.mkdir(parents=True, exist_ok=True)
        seed_dest.write_text(seed.content, encoding="utf-8")
        test_paths = [seed.path[len("backend/"):] if seed.path.startswith("backend/") else seed.path]

        # 复现真实代码路径构造反馈（不复制逻辑，避免测的不是真东西）
        loop = TddLoop(
            store=None,  # type: ignore[arg-type]  _weak_test_feedback 不使用 store
            generator=gen,
            runner=runner,
            config=config,
            output_dir=workspace,
        )
        seed_outcome = runner.run(test_paths)
        if not seed_outcome.passed:
            trial.error = "起点测试不是空转的，已丢弃"
            return trial

        feedback = loop._weak_test_feedback(seed_outcome, test_paths)

        t0 = time.time()
        rewritten = gen.write_tests(requirement, plan, weak_feedback=feedback)
        apply_generated_files(workspace, rewritten)
        trial.elapsed_s = time.time() - t0

        new_paths = [
            f.path for f in rewritten if is_test_file(f.path)
        ] or test_paths
        new_paths = [p[len("backend/"):] if p.startswith("backend/") else p for p in new_paths]

        outcome: TestOutcome = runner.run(new_paths)
        trial.red_achieved = not outcome.passed

        audit = audit_tests(workspace, [f"backend/{p}" for p in new_paths], implementation_root=config.implementation_root)
        trial.imports_impl = audit.imports_implementation
        trial.audit_verdict = audit.verdict
    except Exception as exc:  # noqa: BLE001
        trial.error = f"{type(exc).__name__}: {exc}"
    return trial


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="WEAK_TEST 重写反馈 A/B 度量实验")
    parser.add_argument("--requirements", default="requirements_probe")
    parser.add_argument("--reps", type=int, default=2, help="每个(需求,处理组)的重复次数")
    parser.add_argument("--limit", type=int, default=0, help="只用前 N 个需求（0=全部）")
    parser.add_argument("--out", default="out-exp")
    parser.add_argument("--json", default="experiment_result.json")
    args = parser.parse_args()

    config = FactoryConfig.from_env(weak_feedback_mode="import_aware")
    requirements_dir = (ROOT / args.requirements).resolve()
    template_dir = ROOT / "template"
    base = (ROOT / args.out).resolve()
    base.mkdir(parents=True, exist_ok=True)

    req_set = _adapt(load_requirements_raw(requirements_dir / "requirements.yaml"),
                     source=requirements_dir / "requirements.yaml")
    requirements = list(req_set.requirements)
    if args.limit:
        requirements = requirements[: args.limit]

    client = ModelClient(
        temperature=config.model_temperature,
        max_tokens=config.model_max_tokens,
        timeout_s=config.model_timeout_s,
    )
    gen = LLMGenerator(client, "node")

    print(f"模型: {client.model}   后端: {client.backend()}   需求数: {len(requirements)}   reps={args.reps}")
    print(f"处理组: {VARIANTS}\n")

    result = ExperimentResult(model=client.model)

    for requirement in requirements:
        rpath = target_test_path(requirement)
        print(f"--- {requirement.req_id} {requirement.name}")

        # 1) 造一个空转起点（最多试 2 次）
        seed: GeneratedFile | None = None
        for attempt in range(1, 3):
            try:
                candidate = generate_vacuous_seed(gen, requirement, rpath)
            except Exception as exc:  # noqa: BLE001
                print(f"    起点生成失败({attempt}): {str(exc)[:90]}")
                continue
            if candidate is None:
                continue
            # 校验起点确实空转
            probe_ws = fresh_workspace(base, f"_seedcheck-{requirement.req_id}", template_dir)
            dest = probe_ws / candidate.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(candidate.content, encoding="utf-8")
            probe_paths = [candidate.path[len("backend/"):] if candidate.path.startswith("backend/") else candidate.path]
            probe_runner = NodeTestRunner(probe_ws, timeout_s=config.test_timeout_s)
            if probe_runner.run(probe_paths).passed:
                seed = candidate
                print(f"    空转起点已确认（{candidate.path}, {len(candidate.content)} 字节）")
                break
            print(f"    起点未通过空转校验({attempt})，重造")
        if seed is None:
            print("    跳过：无法造出空转起点\n")
            continue

        # 2) 两种反馈各跑 reps 次
        plan = DesignPlan(req_id=requirement.req_id, summary=f"[exp] {requirement.name}",
                          interfaces=requirement.interfaces, tests=requirement.tests)
        for rep in range(1, args.reps + 1):
            for variant in VARIANTS:
                trial = run_trial(
                    requirement=requirement, plan=plan, seed=seed, variant=variant, rep=rep,
                    gen=gen, config=config,
                    base=base, template_dir=template_dir,
                )
                result.trials.append(trial)
                mark = "RED ✓" if trial.red_achieved else "仍空转 ✗"
                extra = f"  [{trial.error}]" if trial.error else ""
                print(f"    {variant:13s} rep{rep}  {mark:10s} import={trial.imports_impl!s:5s} "
                      f"{trial.elapsed_s:5.1f}s{extra}")
        print()

    # ---- 汇总 ----
    print("=" * 68)
    print(f"{'处理组':<16}{'一次重写达成 RED':<22}{'重写后引用实现':<18}")
    for variant in VARIANTS:
        rh, rn = result.rate(variant, "red_achieved")
        ih, ino = result.rate(variant, "imports_impl")
        print(f"{variant:<16}{f'{rh}/{rn}':<22}{f'{ih}/{ino}':<18}")
    p = fisher_exact(result.rate("import_aware", "red_achieved"), result.rate("basic", "red_achieved"))
    print(f"\nFisher 精确检验 双尾 p = {p:.3f}（样本量小，仅作参考）")
    print("=" * 68)

    payload = result.to_dict()
    (ROOT / args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
