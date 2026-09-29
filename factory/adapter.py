"""需求适配层 —— 整个工厂**唯一**与平台原始需求格式耦合的地方。

======================  替换契约（重要）  ======================
真实 requirements.yaml 格式到位后，**只需要改 `_adapt()` 这一个函数**。
它必须返回 `RequirementSet`（见 models.py）。

下游（generator / loop / store / pipeline / testrunner）只认
`RequirementSet` 及其内部 dataclass，不做任何格式假设。
==============================================================
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

from .models import (
    InterfaceSpec,
    Requirement,
    RequirementSet,
    ScenarioSpec,
    TestSpec,
)

logger = logging.getLogger("factory.adapter")

# ---------------------------------------------------------------------------
# 字段别名表：让 _adapt 对常见的命名差异有容错，减少"改格式就要改代码"
# ---------------------------------------------------------------------------

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "req_id": ("req_id", "id", "requirement_id", "node_id", "key"),
    "name": ("name", "title", "summary", "label"),
    "description": ("description", "desc", "detail", "details", "body", "text"),
    "parent_id": ("parent_id", "parent", "parentId"),
    "children": ("children_ids", "children", "sub_requirements", "subs"),
    "dependencies": ("dependencies", "depends_on", "depends", "deps", "requires"),
    "visual_reference": ("visual_reference", "visual_references", "images", "screenshots", "references"),
    "acceptance": ("acceptance", "acceptance_criteria", "criteria", "checks"),
    "scenarios": ("scenarios", "cases", "acceptance_scenarios"),
    "interfaces": ("interfaces", "apis", "endpoints", "contracts"),
    "tests": ("tests", "test_cases", "testcases"),

    "scenario_id": ("scenario_id", "id", "scenarioId"),
    "scenario_name": ("name", "title", "summary"),
    "steps": ("steps", "flow", "given_when_then"),

    "interface_id": ("interface_id", "id", "interfaceId", "key"),
    "interface_req_ids": ("req_ids", "requirement_ids", "req_id", "requirement_id"),
    "interface_type": ("type", "kind", "interface_type"),
    "interface_content": ("content", "signature", "contract", "description", "detail"),
    "file_path": ("file_path", "path", "file", "target_file"),
    "first_line": ("first_line", "line", "line_number"),

    "test_id": ("test_id", "id", "testId", "key"),
    "test_req_id": ("req_id", "requirement_id"),
    "test_type": ("type", "kind", "test_type", "level"),
    "test_intent": ("intent", "assertion", "assert", "expectation", "verifies", "description", "content"),
    "test_interface_ids": ("interface_ids", "interfaces", "interface_id"),
    "test_scenario_id": ("scenario_id", "scenario"),
}

_ROOT_LIST_KEYS = ("requirements", "requirement_tree", "nodes", "items", "tasks", "features")


def _pick(mapping: Any, field: str, default: Any = None) -> Any:
    """按别名表取值。"""
    if not isinstance(mapping, dict):
        return default
    for alias in _FIELD_ALIASES.get(field, (field,)):
        if alias in mapping and mapping[alias] not in (None, ""):
            return mapping[alias]
    return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _as_str_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            candidate = _pick(item, "req_id") or _pick(item, "name")
            text = _as_str(candidate)
        else:
            text = _as_str(item)
        if text:
            out.append(text)
    return tuple(out)


def _as_optional_str(value: Any) -> str | None:
    text = _as_str(value)
    return text or None


# ---------------------------------------------------------------------------
# 各子结构解析
# ---------------------------------------------------------------------------


def _parse_steps(raw: Any) -> tuple[dict[str, str], ...]:
    """解析场景步骤。支持 [{keyword, content}] 与 ['GIVEN ...', ...] 两种写法。"""
    if not isinstance(raw, (list, tuple)):
        return ()
    steps: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            keyword = _as_str(item.get("keyword") or item.get("type") or item.get("when") or "").upper()
            content = _as_str(item.get("content") or item.get("text") or item.get("value") or "")
            if content:
                steps.append({"keyword": keyword or "GIVEN", "content": content})
        elif isinstance(item, str) and item.strip():
            text = item.strip()
            head, _, tail = text.partition(" ")
            if head.upper() in {"GIVEN", "WHEN", "THEN", "AND", "BUT"}:
                steps.append({"keyword": head.upper(), "content": tail.strip()})
            else:
                steps.append({"keyword": "GIVEN", "content": text})
    return tuple(steps)


def _parse_scenarios(raw: Any, req_id: str) -> tuple[ScenarioSpec, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[ScenarioSpec] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            out.append(
                ScenarioSpec(
                    scenario_id=f"{req_id}-SCN-{index}",
                    name=item.strip() or f"Scenario {index}",
                    req_id=req_id,
                )
            )
            continue
        if not isinstance(item, dict):
            continue
        scenario_id = _as_str(_pick(item, "scenario_id")) or f"{req_id}-SCN-{index}"
        name = _as_str(_pick(item, "scenario_name")) or scenario_id
        out.append(
            ScenarioSpec(
                scenario_id=scenario_id,
                name=name,
                req_id=req_id,
                steps=_parse_steps(_pick(item, "steps")),
            )
        )
    return tuple(out)


def _parse_interfaces(raw: Any, req_id: str) -> tuple[InterfaceSpec, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[InterfaceSpec] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            out.append(
                InterfaceSpec(
                    interface_id=f"{req_id}.IF.{index}",
                    req_ids=(req_id,),
                    type="api",
                    content=item.strip(),
                )
            )
            continue
        if not isinstance(item, dict):
            continue
        interface_id = _as_str(_pick(item, "interface_id")) or f"{req_id}.IF.{index}"
        req_ids = _as_str_list(_pick(item, "interface_req_ids")) or (req_id,)
        out.append(
            InterfaceSpec(
                interface_id=interface_id,
                req_ids=req_ids,
                type=_as_str(_pick(item, "interface_type"), "api").lower(),
                content=_as_str(_pick(item, "interface_content")),
                file_path=_as_optional_str(_pick(item, "file_path")),
                first_line=_as_optional_str(_pick(item, "first_line")),
                implemented=bool(item.get("implemented", False)),
            )
        )
    return tuple(out)


def _parse_tests(raw: Any, req_id: str) -> tuple[TestSpec, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[TestSpec] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            out.append(
                TestSpec(
                    test_id=f"{req_id}.TEST.{index}",
                    req_id=req_id,
                    type="unit",
                    intent=item.strip(),
                )
            )
            continue
        if not isinstance(item, dict):
            continue
        test_id = _as_str(_pick(item, "test_id")) or f"{req_id}.TEST.{index}"
        out.append(
            TestSpec(
                test_id=test_id,
                req_id=_as_str(_pick(item, "test_req_id"), req_id) or req_id,
                type=_as_str(_pick(item, "test_type"), "unit"),
                intent=_as_str(_pick(item, "test_intent")),
                scenario_id=_as_optional_str(_pick(item, "test_scenario_id")),
                interface_ids=_as_str_list(_pick(item, "test_interface_ids")),
                file_path=_as_optional_str(_pick(item, "file_path")),
                first_line=_as_optional_str(_pick(item, "first_line")),
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# 需求树解析（支持扁平列表 + 嵌套 children 两种形态）
# ---------------------------------------------------------------------------


def _iter_nodes(raw_node: Any, inherited_parent: str | None = None) -> Iterable[tuple[dict[str, Any], str | None]]:
    """深度优先展开需求树，yield (原始节点, 父节点 id)。"""
    if not isinstance(raw_node, dict):
        return
    req_id = _as_str(_pick(raw_node, "req_id"))
    parent_id = _as_optional_str(_pick(raw_node, "parent_id")) or inherited_parent
    yield raw_node, parent_id
    children = raw_node.get("children") or raw_node.get("sub_requirements") or raw_node.get("subs")
    if isinstance(children, (list, tuple)):
        for child in children:
            yield from _iter_nodes(child, req_id or inherited_parent)


def _extract_node_list(raw: dict[str, Any] | list[Any]) -> list[Any]:
    if isinstance(raw, list):
        return list(raw)
    if not isinstance(raw, dict):
        raise ValueError("需求根结构必须是 mapping 或 list")
    for key in _ROOT_LIST_KEYS:
        value = raw.get(key)
        if isinstance(value, list):
            return list(value)
        if isinstance(value, dict):
            # 例如 requirement_tree: {REQ-1: {...}}
            return list(value.values())
    # 兜底：把顶层本身当作单个需求节点
    return [raw]


# ---------------------------------------------------------------------------
# ★★★ 替换点 ★★★
# ---------------------------------------------------------------------------


def _adapt(raw: dict[str, Any] | list[Any], *, source: Path) -> RequirementSet:
    """把平台原始需求结构映射为内部规范模型 `RequirementSet`。

    这是工厂与外部格式的**唯一耦合点**。真实格式到位后：

        def _adapt(raw, *, source):
            reqs = [ ... 从真实结构构造 Requirement ... ]
            return RequirementSet(requirements=topo_sort(reqs), source_path=source)

    下游代码无需任何改动。
    """
    nodes: list[tuple[dict[str, Any], str | None]] = []
    for item in _extract_node_list(raw):
        nodes.extend(_iter_nodes(item))

    requirements: list[Requirement] = []
    seen: set[str] = set()

    for node, parent_id in nodes:
        req_id = _as_str(_pick(node, "req_id"))
        if not req_id:
            logger.warning("跳过无 req_id 的需求节点: keys=%s", sorted(node.keys()))
            continue
        if req_id in seen:
            logger.warning("重复的 req_id，已忽略后出现的节点: %s", req_id)
            continue
        seen.add(req_id)

        scenarios = _parse_scenarios(_pick(node, "scenarios"), req_id)
        interfaces = _parse_interfaces(_pick(node, "interfaces"), req_id)
        tests = _parse_tests(_pick(node, "tests"), req_id)

        # 需求未声明 tests 时，用验收场景自动补一条冒烟测试，保证每个需求都可验证
        if not tests and scenarios:
            tests = tuple(
                TestSpec(
                    test_id=f"{req_id}.TEST.{scenario.scenario_id}",
                    req_id=req_id,
                    type="unit",
                    intent=scenario.name,
                    scenario_id=scenario.scenario_id,
                )
                for scenario in scenarios
            )

        requirements.append(
            Requirement(
                req_id=req_id,
                name=_as_str(_pick(node, "name"), req_id) or req_id,
                description=_as_str(_pick(node, "description")),
                parent_id=parent_id,
                children_ids=_as_str_list(_pick(node, "children")),
                dependencies=_as_str_list(_pick(node, "dependencies")),
                visual_reference=_as_str_list(_pick(node, "visual_reference")),
                acceptance=_as_str_list(_pick(node, "acceptance")),
                scenarios=scenarios,
                interfaces=interfaces,
                tests=tests,
            )
        )

    if not requirements:
        raise ValueError(f"需求文件未解析出任何需求节点: {source}")

    ordered = _topological_order(requirements)

    project_name = ""
    schema_version = ""
    if isinstance(raw, dict):
        project = raw.get("project")
        if isinstance(project, dict):
            project_name = _as_str(project.get("name") or project.get("id"))
        elif isinstance(project, str):
            project_name = project.strip()
        schema_version = _as_str(raw.get("schema_version") or raw.get("version"))
    project_name = project_name or source.parent.name or "arcbench-project"

    logger.info(
        "适配完成: %d 个需求, 项目=%s, schema=%s",
        len(ordered),
        project_name,
        schema_version or "(未声明)",
    )
    return RequirementSet(
        requirements=tuple(ordered),
        source_path=source,
        project_name=project_name,
        schema_version=schema_version,
        raw=raw if isinstance(raw, dict) else {"requirements": raw},
    )


def _topological_order(requirements: list[Requirement]) -> list[Requirement]:
    """按 dependencies 做稳定拓扑排序；存在环时退回原始顺序并告警。"""
    by_id = {req.req_id: req for req in requirements}
    order: list[Requirement] = []
    visiting: set[str] = set()
    done: set[str] = set()
    cyclic: set[str] = set()

    def visit(req: Requirement) -> None:
        if req.req_id in done:
            return
        if req.req_id in visiting:
            cyclic.add(req.req_id)
            return
        visiting.add(req.req_id)
        for dep in req.dependencies:
            target = by_id.get(dep)
            if target is not None:
                visit(target)
        visiting.discard(req.req_id)
        done.add(req.req_id)
        order.append(req)

    for requirement in requirements:
        visit(requirement)

    if cyclic:
        logger.warning("检测到依赖环，涉及节点: %s —— 已按稳定顺序执行", ", ".join(sorted(cyclic)))
    return order
