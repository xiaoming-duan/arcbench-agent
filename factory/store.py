"""数据面桥接层。

**设计原则：不重造数据面。** ARC-Bench SDK 已经提供了架构文档中数据面的
三个子系统，这里只做高层封装：

  审计日志(append-only)  -> runtime.events  -> .arc/runner-events.jsonl
  统一对象模型 + 追溯矩阵 -> runtime.traceability -> .arc/traceability/*.json
  检查点与版本管理        -> runtime.git      -> git commit / reset
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from arcbench_agent_runtime import AgentRuntime

from .models import InterfaceSpec, Requirement, RequirementSet, TestSpec

logger = logging.getLogger("factory.store")


class FactoryStore:
    """把工厂语义翻译成 ARC-Bench SDK 调用。"""

    def __init__(self, runtime: AgentRuntime) -> None:
        self.runtime = runtime
        self.events = runtime.events
        self.traceability = runtime.traceability
        self.git = runtime.git

    # ---- 生命周期 ----

    def start_run(self, message: str) -> None:
        self.events.mark_run_started(message)

    def complete_run(self, message: str) -> None:
        self.events.mark_run_completed(message)

    def fail_run(self, message: str) -> None:
        self.events.mark_run_failed(message)

    def init(self) -> None:
        self.traceability.init_db()
        logger.info("traceability 已初始化: %s", self.traceability.root)

    def ensure_repo(self) -> None:
        self.git.ensure_repo(create_initial_commit=True)

    def commit(self, message: str) -> bool:
        return self.git.commit(message)

    # ---- 需求树 ----

    def record_requirement_tree(self, req_set: RequirementSet) -> None:
        for requirement in req_set.requirements:
            self.traceability.upsert_requirement(**requirement.to_requirement_payload())
        logger.info("已写入 %d 个需求节点到 traceability", len(req_set))

    def record_dependency_edges(self, requirements: Iterable[Requirement]) -> None:
        """把需求依赖显式转换成 call edge。

        ★方向约定（与平台一致，**不要翻转**）：
            source_req_id = **调用方**（声明了 dependencies 的那个需求）
            target_req_id = **被调用方**（被依赖的那个需求）

            A depends_on B  ->  insert_call_edge(source_req_id=A, target_req_id=B)

        常见误读是把 depends_on 读成「B 被 A 依赖」而写成 (B, A)——那是反的。
        函数名与注释都写明 source=调用方，就是为了下次不再写反。

        平台文档（Core APIs / schema.md）只定义了 req 级方向，未定义接口级语义，
        因此 from_interface_id / to_interface_id 传空串：写的是**需求级**依赖边，
        不臆造接口锚点。注意这两个是 SDK 的必填参数，省略会 TypeError。
        """
        count = 0
        for requirement in requirements:
            for dep_id in requirement.dependencies:
                self.traceability.insert_call_edge(
                    source_req_id=requirement.req_id,   # 调用方
                    target_req_id=dep_id,               # 被调用方
                    from_interface_id="",
                    to_interface_id="",
                )
                count += 1
        logger.info("已写入 %d 条依赖 call edge（source=调用方）", count)

    def record_node_contract(self, req_id: str, interfaces: tuple[InterfaceSpec, ...]) -> None:
        """写节点契约（node_contracts 表，此前恒为 0 行）。

        content 直接由该需求的接口派生。平台 schema 未定义 node contract 的
        content 结构（schema.md 只列了 Requirement/Scenario/Interface/Test/Call Edge/
        Node State 六类），故不臆造字段，只放接口自身的 id/type/content/落点。
        """
        self.traceability.upsert_node_contract(
            req_id,
            {
                "interfaces": [
                    {
                        "interface_id": spec.interface_id,
                        "type": spec.type,
                        "content": spec.content,
                        "file_path": spec.file_path,
                        "first_line": spec.first_line,
                    }
                    for spec in interfaces
                ]
            },
        )

    # ---- 阶段状态（同时驱动前端刷新） ----

    def design_started(self, req_id: str, message: str | None = None) -> None:
        self.events.mark_design_started(req_id, message)

    def design_done(self, req_id: str, message: str | None = None) -> None:
        self.events.mark_design_done(req_id, message)

    def design_failed(self, req_id: str, message: str | None = None) -> None:
        self.events.mark_design_failed(req_id, message)

    def implement_started(self, req_id: str, message: str | None = None) -> None:
        self.events.mark_implementation_started(req_id, message)

    def implement_done(self, req_id: str, message: str | None = None) -> None:
        self.events.mark_implementation_done(req_id, message)

    def implement_failed(self, req_id: str, message: str | None = None) -> None:
        self.events.mark_implementation_failed(req_id, message)

    def test_passed(self, req_id: str, message: str | None = None) -> None:
        self.events.mark_test_passed(req_id, message)

    def test_failed(self, req_id: str, message: str | None = None) -> None:
        self.events.mark_test_failed(req_id, message)

    def set_state(self, req_id: str, state: str, phase: str | None = None) -> None:
        self.traceability.upsert_node_state(req_id, state, phase)

    # ---- 接口 / 测试记录 ----

    def record_interfaces(self, interfaces: tuple[InterfaceSpec, ...]) -> None:
        for spec in interfaces:
            self.traceability.upsert_interface(
                interface_id=spec.interface_id,
                req_ids=list(spec.req_ids),
                type=spec.type,
                content=spec.content,
                file_path=spec.file_path,
                first_line=spec.first_line,
                implemented=spec.implemented,
            )

    def record_tests(self, tests: tuple[TestSpec, ...]) -> None:
        for spec in tests:
            self.traceability.upsert_test(
                test_id=spec.test_id,
                req_id=spec.req_id,
                type=spec.type,
                file_path=spec.file_path,
                first_line=spec.first_line,
                interface_ids=list(spec.interface_ids),
                passed=None,
                scenario_id=spec.scenario_id,
            )

    def mark_interface_implemented(self, interface_id: str, message: str = "") -> None:
        self.traceability.set_interface_implemented(interface_id, True, message)

    def set_test_status(self, test_id: str, passed: bool) -> None:
        self.traceability.set_test_pass_status(test_id, passed)

    def link_interface_file(self, interface_id: str, file_path: str, first_line: str | None = None) -> None:
        current: dict[str, Any] | None = self.traceability.get_interface(interface_id)
        if current is None:
            return
        self.traceability.update_interface_fields(interface_id, file_path=file_path, first_line=first_line)

    # ---- 查询（供报告使用） ----

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return self.traceability.export_snapshot()

    def requirement_state(self, req_id: str) -> dict[str, Any] | None:
        return self.traceability.get_node_state(req_id)
