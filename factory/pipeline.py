"""顶层编排：把需求目录编译成工作区里的应用。

流水线（对应架构文档"五个车间"）：
  需求解析 -> 复制模板 -> 规划(适配+拓扑) -> 生产(TDD循环) -> 质检(门禁) -> 集成(提交+报告)
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path

from arcbench_agent_runtime import AgentRuntime

from .adapter import _adapt
from .config import FactoryConfig
from .contracts import contracts_dir
from .generator import build_generator
from .llm import ModelClient, ModelQuotaExhaustedError
from .loop import TddLoop
from .models import RequirementResult, RunReport
from .store import FactoryStore
from .testrunner import build_runner, ensure_backend_dependencies
from .version import git_state, log_state
from .workspace import copy_template_contents, load_requirements_raw, write_json

logger = logging.getLogger("factory.pipeline")

# 代码版本锚点的仓库根：factory/ 的上一级即工作区根
ROOT_FOR_VERSION = Path(__file__).resolve().parent.parent


def build_children_map(requirements) -> dict[str, list[str]]:
    """需求树的父子关系映射。

    **两种层级表达都要认**：父节点自带 children_ids，或子节点声明 parent_id。
    只认前者会在「平台只用 parent_id 表达层级」时漏判，ROOT 照样被当成叶子去设计。
    """
    children_of: dict[str, list[str]] = {}
    for item in requirements:
        for child_id in item.children_ids:
            children_of.setdefault(item.req_id, []).append(child_id)
        if item.parent_id:
            children_of.setdefault(item.parent_id, []).append(item.req_id)
    return children_of


def container_ids_of(requirements) -> set[str]:
    """容器节点集合（如平台 ROOT）：它们只应分解，不应被设计。

    若照常走设计，等于让模型**一次性设计整个平台**：prompt 体积与输出 token
    同时爆炸，实测每次调用卡满超时、重试耗尽后整条 ROOT 失败。
    分解信息本来就在输入里，这里只需把它们识别出来并跳过设计。
    """
    return {parent for parent, kids in build_children_map(requirements).items() if kids}

DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "template"
FIXTURE_DIRNAME = "fixtures"
REPORT_RELPATH = Path(".arc") / "factory-report.json"


def _resolve_test_dialect(config: FactoryConfig, output_dir: Path) -> str:
    """决定测试方言，必要时尝试安装依赖后再判定。"""
    dialect = config.resolve_test_dialect(output_dir)
    if dialect == "vitest":
        logger.info("测试方言: vitest（生产路径）")
        return dialect

    if config.install_deps != "never":
        logger.info("未检测到 vitest，尝试安装 backend 依赖以启用生产路径...")
        if ensure_backend_dependencies(output_dir, timeout_s=config.install_timeout_s):
            if (output_dir / "backend" / "node_modules" / "vitest").exists():
                logger.info("测试方言: vitest（依赖安装成功）")
                return "vitest"

    logger.warning(
        "测试方言降级为 node（内置 test runner）。这是**链路验证路径**，"
        "生产环境应在有 node_modules 时使用 vitest。"
    )
    return "node"


def run_factory(
    runtime: AgentRuntime,
    requirements_dir: Path,
    output_dir: Path,
    *,
    config: FactoryConfig | None = None,
    template_dir: Path | None = None,
) -> RunReport:
    config = config or FactoryConfig.from_env()
    template_dir = template_dir or DEFAULT_TEMPLATE_DIR
    store = FactoryStore(runtime)

    report = RunReport(project_name="", requirements_total=0)
    logger.info("工厂启动: requirements=%s output=%s", requirements_dir, output_dir)
    logger.info("配置: %s", config.describe())

    # ---- 代码版本锚点 ----
    # 记录本次运行跑在哪份代码上，并写入报告（.arc/factory-report.json）。
    # 脏工作区 -> trustworthy=False：不是失败，而是标注「测量基础可疑」。
    # 这个工作区已为此付过三次代价（to_dict 缺字段静默失败、measure_source
    # 别名断言失效、并发写入污染运行到一半的测量），见 factory/version.py。
    report.code_version = git_state(ROOT_FOR_VERSION)
    log_state(report.code_version)

    store.start_run(f"工厂启动: {requirements_dir.name} -> {output_dir.name}")

    try:
        # ---- 车间 1: 需求解析 ----
        requirements_file = config.resolve_requirements_file(requirements_dir)
        logger.info("需求文件: %s", requirements_file)

        # ---- 集成准备: 复制模板 + 初始化仓库 ----
        copy_template_contents(template_dir, output_dir)
        store.ensure_repo()

        # ---- 规划: 适配为内部模型 ----
        raw = load_requirements_raw(requirements_file)
        req_set = _adapt(raw, source=requirements_file)
        report.project_name = req_set.project_name
        report.requirements_total = len(req_set)

        store.init()
        store.record_requirement_tree(req_set)
        # 依赖边是需求的静态属性，与执行结果无关，故在进入需求循环之前一次性写全，
        # 这样即使某些需求因上游失败被跳过，依赖图依然完整。
        store.record_dependency_edges(req_set.requirements)
        store.commit("requirements: 需求树入库")

        # ---- 决定生成器与测试方言 ----
        # ---- 契约冻结：编译前把 cross_module_calls 写成只读合同 ----
        # 位置刻意放在跑需求之前、模板就位之后：
        #   需求 YAML 已解析完（req_set 可用），输出目录已存在（可写 .arc/contracts/）。
        # 未声明 cross_module_calls 的需求不生成合同（可选能力的默认关闭语义）。
        from .contracts import write_contracts
        write_contracts(output_dir, req_set.requirements)

        dialect = _resolve_test_dialect(config, output_dir)
        generator_kind = config.resolve_generator()
        report.generator = generator_kind
        report.test_dialect = dialect

        fixture_root = requirements_dir / FIXTURE_DIRNAME
        # 只有真的要调模型时才构造模型客户端，避免无谓的配置告警
        model_client = None
        if generator_kind == "llm":
            model_client = ModelClient(
                temperature=config.model_temperature,
                max_tokens=config.model_max_tokens,
                timeout_s=config.model_timeout_s,
            )
        generator = build_generator(
            generator_kind,
            fixture_root=fixture_root,
            test_dialect=dialect,
            model_client=model_client,
            contracts_dir=contracts_dir(output_dir),
        )
        runner = build_runner(dialect, output_dir, timeout_s=config.test_timeout_s)

        # ---- 车间 3-5: 逐需求 TDD ----
        loop = TddLoop(
            store=store,
            generator=generator,
            runner=runner,
            config=config,
            output_dir=output_dir,
        )
        # 上游失败传播：声明依赖的上游没通过时，下游**不进入 TDD 循环**。
        #
        # 为什么必须做：不修的话，上游缺失时下游仍会被调度并消耗预算，
        # 而且它可能靠参数注入绕过依赖门禁、造成假阳性通过，
        # 让「依赖累积」的失败归因变得不可分辨（是上游的问题还是下游的问题）。
        # 有这条传播后，下游判 UPSTREAM_FAILED 并**不计入通过率分母**。
        outcomes: dict[str, RequirementResult] = {}
        quota_exhausted = False

        # ---- 容器节点（ROOT）识别 ----
        # 平台会把「整个平台」作为 ROOT 塞进需求树。若照常走设计，等于让模型
        # **一次性设计整个平台**：prompt 体积与输出 token 同时爆炸，实测每次调用
        # 卡满 3 分钟超时、重试耗尽后整条 ROOT 失败。分解信息本来就在输入里
        # （children 数组或子节点的 parent_id），这里只需**跳过设计**。
        #
        # 两种父子表达都要认：父节点自带 children_ids，或子节点声明 parent_id。
        # 只认前者会在「平台只用 parent_id 表达层级」时漏判，ROOT 照样被设计。
        children_of = build_children_map(req_set.requirements)
        container_ids = {parent for parent, kids in children_of.items() if kids}

        for requirement in req_set.requirements:
            if config.dry_run:
                logger.info("[dry-run] 跳过执行: %s", requirement.req_id)
                continue

            if requirement.req_id in container_ids:
                logger.info(
                    "[分解节点] %s 是容器节点（%d 个子需求），只分解不设计——跳过设计/TDD",
                    requirement.req_id,
                    len(children_of[requirement.req_id]),
                )
                continue

            blocked_by = (
                [
                    dep
                    for dep in requirement.dependencies
                    if dep in outcomes and outcomes[dep].state != "PASSED"
                ]
                if config.enforce_upstream_gate
                else []
            )
            if blocked_by:
                reason = "; ".join(f"{d}={outcomes[d].state}" for d in blocked_by)
                outcome = RequirementResult(
                    req_id=requirement.req_id,
                    state="UPSTREAM_FAILED",
                    note=f"上游未通过，未进入 TDD 循环（{reason}）",
                )
                logger.warning(
                    "[上游传播] %s 跳过（%s）—— 不消耗预算、不计入通过率分母",
                    requirement.req_id,
                    reason,
                )
                store.set_state(requirement.req_id, "UPSTREAM_FAILED", "dependency")
                report.results.append(outcome)
                outcomes[requirement.req_id] = outcome
                continue

            # 按需求切分成本：模型客户端是全局的，用快照求增量
            cost_before = model_client.stats.snapshot() if model_client else {}
            try:
                outcome = loop.run(requirement)
            except ModelQuotaExhaustedError as exc:
                # ★配额/余额耗尽：后面每个需求都注定失败。立刻收摊，
                #   而不是逐个把重试预算烧完 —— 实测代价：
                #   「网关重试 11 次（成功 0）」外加每个需求 3 次循环级重试。
                quota_exhausted = True
                report.error = f"ModelQuotaExhaustedError: {exc}"
                logger.error(
                    "[致命] 模型配额/余额耗尽，停止本轮剩余需求（共 %d 个）: %s",
                    len(req_set.requirements),
                    exc,
                )
                break
            if model_client:
                outcome.cost = model_client.stats.delta(cost_before)
            report.results.append(outcome)
            outcomes[requirement.req_id] = outcome

        # ---- 容器节点收敛状态 ----
        # 容器节点自己不跑测试，状态由子需求聚合而来。用的是平台文档枚举里的
        # CONVERGED / CONVERGED_WITH_FAILED_CHILDREN（见 schema.md 的 9 值清单），
        # 不新造状态名。嵌套容器递归求解。
        def _container_state(req_id: str) -> str:
            kids = children_of.get(req_id, [])
            if not kids:
                return outcomes[req_id].state if req_id in outcomes else "UNSEEN"
            kid_states = [
                _container_state(kid) if kid in container_ids
                else (outcomes[kid].state if kid in outcomes else "UNSEEN")
                for kid in kids
            ]
            if kid_states and all(state == "PASSED" for state in kid_states):
                return "CONVERGED"
            return "CONVERGED_WITH_FAILED_CHILDREN"

        for container_id in sorted(container_ids):
            container_state = _container_state(container_id)
            store.set_state(container_id, container_state, "decompose")
            logger.info("[分解节点] %s 收敛状态: %s", container_id, container_state)

        # ---- 集成交付 ----
        passed = sum(1 for item in report.results if item.state == "PASSED")
        failed = sum(1 for item in report.results if item.state == "FAILED")
        upstream_failed = sum(1 for item in report.results if item.state == "UPSTREAM_FAILED")
        report.upstream_failed = upstream_failed
        # 通过率分母**不含** UPSTREAM_FAILED —— 它们根本没跑，
        # 计入分母会把上游的问题算成下游的问题。
        attempted = passed + failed
        # 空转测试（实现前就通过）意味着这条"绿灯"不构成证据，不能算通过。
        # 测试是唯一权威；一条永远不会失败的测试不是权威。
        weak = sum(1 for item in report.results if item.red_first_ok is False)
        # ★容器节点（ROOT 等）只分解不设计，因此不产生 RequirementResult。
        #   它们仍计入 requirements_total，但此前在摘要里**完全不出现**——
        #   实测平台日志：共 42 个需求，尝试 3 个 + 上游失败 21 个 = 24，
        #   剩下 18 个无从解释。现在单独列出来，账目当场对得上。
        report.decomposed = len(container_ids)
        summary = (
            f"完成: {passed} 通过 / {failed} 失败 / {upstream_failed} 上游失败跳过"
            f" / {weak} 空转测试(WEAK_TEST)"
            f" / {report.decomposed} 分解节点（只分解不设计）"
            f" / 共 {report.requirements_total} 个需求（尝试 {attempted} 个）"
        )
        store.commit(f"factory: {summary}")

        # 有上游失败跳过时整体不算 ok：闭包没有被完整验证
        # 配额耗尽中止时绝不能判 ok：即使恰好没有失败，本轮也没跑完
        report.ok = (
            failed == 0 and passed > 0 and weak == 0
            and upstream_failed == 0 and not quota_exhausted
        )
        if model_client is not None:
            report.cost = model_client.stats.to_dict()
            logger.info("成本记账: %s", model_client.stats.summary())
        report.artifacts = [
            str(REPORT_RELPATH),
            ".arc/runner-events.jsonl",
            ".arc/traceability/",
        ]
        write_json(output_dir / REPORT_RELPATH, report.to_dict())

        if report.ok:
            store.complete_run(summary)
        else:
            store.fail_run(summary)
        logger.info(summary)
        return report

    except Exception as exc:  # 任何异常都要让平台看到失败态
        report.ok = False
        report.error = f"{type(exc).__name__}: {exc}"
        logger.error("工厂失败: %s", report.error)
        logger.debug(traceback.format_exc())
        try:
            store.fail_run(f"工厂失败: {report.error}")
            write_json(output_dir / REPORT_RELPATH, report.to_dict())
        except Exception:  # pragma: no cover
            logger.debug("写入失败报告时出错", exc_info=True)
        raise
