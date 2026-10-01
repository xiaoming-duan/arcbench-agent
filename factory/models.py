"""内部规范化模型。

关键约定：这是工厂的**内部契约**，与平台原始需求格式解耦。
平台格式的任何变化都只影响 adapter._adapt()，不会波及下游。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------
# 需求侧
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioSpec:
    """验收场景（GIVEN/WHEN/THEN）。"""

    scenario_id: str
    name: str
    req_id: str = ""
    steps: tuple[dict[str, str], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.scenario_id,
            "name": self.name,
            "steps": list(self.steps),
        }


@dataclass(frozen=True)
class InterfaceSpec:
    """实现接口：一个需求对外/对内的可验证边界（UI / API / DB）。"""

    interface_id: str
    req_ids: tuple[str, ...]
    type: str
    content: str
    file_path: str | None = None
    first_line: str | None = None
    implemented: bool = False


@dataclass(frozen=True)
class TestSpec:
    """测试用例。intent 描述"要验证什么"，供生成器产出断言。"""

    test_id: str
    req_id: str
    type: str
    intent: str = ""
    scenario_id: str | None = None
    interface_ids: tuple[str, ...] = ()
    file_path: str | None = None
    first_line: str | None = None


@dataclass(frozen=True)
class CrossModuleCall:
    """下游对上游的一次**声明式跨模块调用契约**。

    为什么需要它：单模块测试无法暴露「同名不同语义」。
    实测（REQ-11）：下游测试要求 `updateQuantity(sku, quantity, from, to)`，
    而上游 REQ-7 实现的是 `updateQuantity(sku, from, to)`（语义为区间变更）。
    两个需求的单模块测试各自通过，矛盾只在集成点出现 ——
    而模型看到「你没 import 上游」时，**无法推断出正确的参数语义**。

    声明后：签名同时注入下游（要这么调）与上游（要这么提供），
    门禁再按声明比对实际调用，不一致判 CONTRACT_MISMATCH。
    """

    upstream: str
    symbol: str
    signature: str = ""
    semantics: str = ""
    side_effects: tuple[str, ...] = ()

    @property
    def declared_arity(self) -> int | None:
        """从签名文本解析形参个数，如 `f(a, b, c)` -> 3。解析不出返回 None。"""
        import re as _re
        m = _re.search(r"\(([^)]*)\)", self.signature or "")
        if not m:
            return None
        body = m.group(1).strip()
        if not body:
            return 0
        # 顶层逗号计数（忽略嵌套括号与默认值里的逗号）
        depth = 0
        count = 1
        for ch in body:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                count += 1
        return count

    def to_dict(self) -> dict[str, Any]:
        return {
            "upstream": self.upstream,
            "symbol": self.symbol,
            "signature": self.signature,
            "semantics": self.semantics,
            "side_effects": list(self.side_effects),
            "declared_arity": self.declared_arity,
        }


@dataclass(frozen=True)
class Requirement:
    """规范化需求节点。"""

    req_id: str
    name: str
    description: str = ""
    parent_id: str | None = None
    children_ids: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    # 本需求**声明**要调用上游的契约（可选；未声明时全链路行为不变）
    cross_module_calls: tuple[CrossModuleCall, ...] = ()
    # 由适配层从全量需求集**推导**：别人声明要调用我的哪些符号。
    # 不是 YAML 字段 —— 让契约天然双向，无需上游重复声明。
    incoming_contracts: tuple[CrossModuleCall, ...] = ()
    visual_reference: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    scenarios: tuple[ScenarioSpec, ...] = ()
    interfaces: tuple[InterfaceSpec, ...] = ()
    tests: tuple[TestSpec, ...] = ()

    # 执行期可变的接口实现状态由 store 负责，不放在这里

    def to_requirement_payload(self) -> dict[str, Any]:
        """转成 ARC-Bench traceability 的 requirement payload。"""
        return {
            "req_id": self.req_id,
            "name": self.name,
            "description": self.description,
            "visual_reference": list(self.visual_reference),
            "scenarios": [s.to_payload() for s in self.scenarios],
            "parent_id": self.parent_id,
            "children_ids": list(self.children_ids),
            "dependencies": list(self.dependencies),
            # 注意：**不要**把 cross_module_calls 塞进这个 payload ——
            # 它是工厂内部的契约概念，不属于平台追溯 schema。
            # 实测：加进去会抛
            # `TypeError: TraceabilityStore.upsert_requirement() got an unexpected
            #  keyword argument 'cross_module_calls'`，整轮运行直接失败。
        }


@dataclass(frozen=True)
class RequirementSet:
    """一次任务的全部需求（已做依赖拓扑排序）。"""

    requirements: tuple[Requirement, ...]
    source_path: Path
    project_name: str = ""
    schema_version: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def by_id(self, req_id: str) -> Requirement | None:
        for req in self.requirements:
            if req.req_id == req_id:
                return req
        return None

    def __len__(self) -> int:
        return len(self.requirements)


# --------------------------------------------------------------------------
# 生成侧
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneratedFile:
    """生成器产出的一个文件变更。

    mode:
      - write        整体写入（覆盖）
      - insert_after 在 marker 行之后插入 content（幂等：已存在则跳过）
    """

    path: str
    content: str
    mode: str = "write"
    marker: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"write", "insert_after"}:
            raise ValueError(f"不支持的 mode: {self.mode}")
        if self.mode == "insert_after" and not self.marker:
            raise ValueError("insert_after 模式必须提供 marker")


@dataclass(frozen=True)
class DesignPlan:
    """一个需求的设计产物。"""

    req_id: str
    summary: str
    steps: tuple[str, ...] = ()
    interfaces: tuple[InterfaceSpec, ...] = ()
    tests: tuple[TestSpec, ...] = ()


@dataclass
class TestOutcome:
    """一次测试执行的结构化结果。"""

    passed: bool
    command: str
    exit_code: int
    total: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    dialect: str = ""

    def summary(self) -> str:
        if self.total:
            return f"{'PASS' if self.passed else 'FAIL'} ({self.total - self.failed}/{self.total})"
        return "PASS" if self.passed else "FAIL"


@dataclass
class RequirementResult:
    """单个需求的执行结果，用于最终报告。"""

    req_id: str
    state: str
    attempts: int = 0
    red_first_ok: bool | None = None
    note: str = ""
    # A：计划内测试文件（白名单）
    test_plan_files: list[str] = field(default_factory=list)
    test_plan_baselines: dict[str, Any] = field(default_factory=dict)
    # 测试重写轮次（WEAK_TEST 回退次数）
    test_rewrites: int = 0
    # 该需求**实际写出**的测试用例数（`it(` / `test(` 块个数，取写测试阶段的总和）。
    # 存在的理由：`test_usecase_limit` 假说（根节点用例过度生成）需要可观测数据 ——
    # 没有这个字段，就只能事后翻产物目录数文件，无法在报告里做方差对比。
    # 口径与 `testplan.measure_source` 一致，不另立第二套。
    test_case_count: int = 0
    # 写测试阶段的尝试次数（白名单拒绝导致的重试）。
    # 必须与 test_rewrites 分开：D2 造成的额外调用发生在这里，不在重写轮次里——
    # 只看 test_rewrites 会把这类浪费完全掩盖掉。
    write_attempts: int = 0
    # 该需求消耗的成本增量（token / 网关重试 / 适配重试）
    cost: dict[str, Any] = field(default_factory=dict)
    # B：依赖使用审计（声明依赖是否被真实调用）
    dependency_violations: list[dict[str, Any]] = field(default_factory=list)
    dependency_uncertain: list[dict[str, Any]] = field(default_factory=list)
    # 方案 2-B：注入旁路警告（第一轮不阻断，只记录）
    dependency_injection_warnings: list[dict[str, Any]] = field(default_factory=list)
    # 重写后回归（曾通过、重写后失败，已回退）
    regressions: list[dict[str, Any]] = field(default_factory=list)
    # 最终门禁三段审计的 ok 标志（用于验证判定本身，而不是只能看结果）
    gate_audits: dict[str, Any] = field(default_factory=dict)
    # 冻结合同完整性违规（CONTRACT_MISSING）：合同缺失 / 未冻结 / 与声明漂移。
    # 与 dependency_violations 分开：那是「实现不符合同」，这是「合同本身不成立」。
    contract_violations: list[dict[str, Any]] = field(default_factory=list)
    # 测试文件无法被收集/执行（RED 门禁判 TEST_BROKEN，回退到写测试阶段）
    broken_test: bool = False
    # 重写边界：被拦截的测试文件写入 / 走显式通道允许的测试重写
    blocked_test_writes: list[dict[str, Any]] = field(default_factory=list)
    test_rewrite_reasons: list[dict[str, Any]] = field(default_factory=list)
    # 被拒绝的计划外测试文件 / 被拒绝的弱化改动
    unauthorized_files: list[str] = field(default_factory=list)
    weakening_violations: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# RunReport schema —— 声明字段必须全部存在，缺失即报错
# ---------------------------------------------------------------------------
#
# 动机：曾出现 patch 锚点静默失败导致 to_dict 少输出 5 个字段，
# 而报告的使用者（实验脚本 / 人工核对）无从察觉。字段清单必须是被断言的事实，
# 而不是"我记得加了"。

RUN_REPORT_FIELDS: tuple[str, ...] = (
    "project_name",
    "requirements_total",
    "upstream_failed",
    "decomposed",
    "generator",
    "test_dialect",
    "ok",
    "error",
    "results",
    "cost",
    "artifacts",
    # 代码版本锚点：head / dirty_count / trustworthy。
    # 没有它就无法回答「这次跑在哪份代码上」——已为此付过三次代价
    # （to_dict 缺字段静默失败、measure_source 别名断言失效、并发写入污染运行）。
    "code_version",
)

REQUIREMENT_RESULT_FIELDS: tuple[str, ...] = (
    "req_id",
    "state",
    "attempts",
    "red_first_ok",
    "note",
    "test_rewrites",
    "test_case_count",
    "contract_violations",
    "write_attempts",
    "cost",
    "test_plan_files",
    "test_plan_baselines",
    "unauthorized_files",
    "weakening_violations",
    "dependency_violations",
    "dependency_uncertain",
    "dependency_injection_warnings",
    "broken_test",
    "regressions",
    "gate_audits",
    "blocked_test_writes",
    "test_rewrite_reasons",
)

COST_FIELDS: tuple[str, ...] = (
    "calls",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "reasoning_tokens",
    "gateway_retries",
    "json_retries",
)


class RunReportSchemaError(ValueError):
    """RunReport 输出缺少声明字段。"""


def validate_run_report(payload: dict[str, Any]) -> dict[str, Any]:
    """校验 RunReport 输出。缺失字段即抛异常，附缺失字段名与位置。"""
    problems: list[str] = []

    missing_top = [f for f in RUN_REPORT_FIELDS if f not in payload]
    if missing_top:
        problems.append(f"顶层缺字段: {missing_top}")

    results = payload.get("results")
    if not isinstance(results, list):
        problems.append("results 不是列表")
    else:
        for index, row in enumerate(results):
            if not isinstance(row, dict):
                problems.append(f"results[{index}] 不是对象")
                continue
            missing = [f for f in REQUIREMENT_RESULT_FIELDS if f not in row]
            if missing:
                problems.append(f"results[{index}] (req_id={row.get('req_id')}) 缺字段: {missing}")

    cost = payload.get("cost")
    if isinstance(cost, dict) and cost:
        missing_cost = [f for f in COST_FIELDS if f not in cost]
        if missing_cost:
            problems.append(f"cost 非空但缺字段: {missing_cost}")

    if problems:
        raise RunReportSchemaError(
            "RunReport schema 校验失败：\n  - " + "\n  - ".join(problems)
        )
    return payload


@dataclass
class RunReport:
    """整次运行的报告。"""

    project_name: str
    requirements_total: int
    results: list[RequirementResult] = field(default_factory=list)
    # 因上游未通过而未进入 TDD 循环的需求数（不计入通过率分母）
    upstream_failed: int = 0
    # 容器节点（ROOT 等）：只分解不设计，不产生 RequirementResult。
    # 但它计入 requirements_total，所以必须单独报出来，否则摘要里账目对不上。
    decomposed: int = 0
    artifacts: list[str] = field(default_factory=list)
    generator: str = ""
    test_dialect: str = ""
    ok: bool = False
    error: str = ""
    # 成本记账：token 累计 + 网关重试（与需求级的 rewrite_rounds 分开）
    cost: dict[str, Any] = field(default_factory=dict)
    # 代码版本锚点（factory/version.py:git_state）。
    # trustworthy=False 表示工作区有未提交改动 / 取不到 git 信息 ——
    # 该次运行的测量基础可疑，结论不应被当成干净数据使用。
    code_version: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "project_name": self.project_name,
            "requirements_total": self.requirements_total,
            "upstream_failed": self.upstream_failed,
            "decomposed": self.decomposed,
            "generator": self.generator,
            "test_dialect": self.test_dialect,
            "ok": self.ok,
            "error": self.error,
            "results": [
                {
                    "req_id": r.req_id,
                    "state": r.state,
                    "attempts": r.attempts,
                    "red_first_ok": r.red_first_ok,
                    "note": r.note,
                    "test_rewrites": r.test_rewrites,
                    "test_case_count": r.test_case_count,
                    "contract_violations": list(r.contract_violations),
                    "write_attempts": r.write_attempts,
                    "cost": dict(r.cost),
                    "test_plan_files": list(r.test_plan_files),
                    "test_plan_baselines": dict(r.test_plan_baselines),
                    "unauthorized_files": list(r.unauthorized_files),
                    "weakening_violations": list(r.weakening_violations),
                    "dependency_violations": list(r.dependency_violations),
                    "dependency_uncertain": list(r.dependency_uncertain),
                    "dependency_injection_warnings": list(r.dependency_injection_warnings),
                    "regressions": list(r.regressions),
                    "gate_audits": dict(r.gate_audits),
                    "broken_test": r.broken_test,
                    "blocked_test_writes": list(r.blocked_test_writes),
                    "test_rewrite_reasons": list(r.test_rewrite_reasons),
                }
                for r in self.results
            ],
            "cost": dict(self.cost),
            "artifacts": list(self.artifacts),
            "code_version": dict(self.code_version),
        }
        # 输出即校验：字段缺失直接报错，不产出不完整的报告
        return validate_run_report(payload)
