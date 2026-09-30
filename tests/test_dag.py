"""多模块 DAG 的性质断言（D1–D10）。

═══════════════════════════════════════════════════════════════════════════
 这些断言守的是**性质**，不是快照
═══════════════════════════════════════════════════════════════════════════
对的（性质）:
    plan = build_plan({"A": ["B"], "C": []})   # B 不在图中
    assert "B" in plan["dangling_dependencies"]

错的（快照）:
    assert plan == {...}      # 锁定当前输出，任何改动都红 —— 断言不该守这个

═══════════════════════════════════════════════════════════════════════════
 命名：DAG-D1 … DAG-D10
═══════════════════════════════════════════════════════════════════════════
沿用 DAG_DESIGN.md §6 的 D1–D10 编号，但测试函数名加 `DAG-` 前缀。
原因：**`D10` 这个编号在本项目已被占用** ——
  tools/verify_d10.py        记录的是「最终门禁漏看 mock_audit.ok」那个缺陷的验证
  tools/test_gates.py        里的 D3 / D4 是另一组缺陷编号
不加前缀会在 check_all 输出里出现两个「D10」，且指向完全不同的事。

═══════════════════════════════════════════════════════════════════════════
 被断言对象的现状（写这些断言时的事实）
═══════════════════════════════════════════════════════════════════════════
`factory/dag.py` **尚不存在**（只有 DAG_DESIGN.md 里的设计）。
若直接 `from factory.dag import build_plan`，十个测试会全部 ImportError ——
那只能证明「模块没写」，**证明不了「缺口存在」**，而后者才是本轮要确认的。

所以这里用一个**回退适配器**：优先用 `factory.dag.build_plan`；
不存在时回退到「当前实现的真实能力」——`adapter._topological_order` 的产物，
它**只有 order，没有 dangling_dependencies，也没有 cycle_detected**。

回退不是为了让测试通过，恰恰相反：它让每一条失败的**原因指向缺失的性质**，
而不是笼统的「模块不存在」。当 dag.py 落地后，这些断言自动切到真实现，应当转绿。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))
sys.path.insert(0, str(ROOT / "tools"))

Graph = Mapping[str, Sequence[str]]


# ---------------------------------------------------------------------------
# 被断言对象：优先真实现，否则回退到「当前能力」
# ---------------------------------------------------------------------------


def _current_behavior_plan(graph: Graph) -> dict[str, Any]:
    """当前实现能给出的全部信息 —— 只有 order。

    刻意**不**补 dangling / cycle 字段：
    补了就等于在测试里实现了被测功能，断言会假装通过。
    这里如实反映现状：`adapter._topological_order` 只产出顺序。
    """
    from factory.adapter import _topological_order
    from factory.models import Requirement

    reqs = [Requirement(req_id=node, name=node, dependencies=tuple(deps))
            for node, deps in graph.items()]
    return {"order": tuple(r.req_id for r in _topological_order(reqs))}


def _load_build_plan():
    """返回 (build_plan, 来源说明)。"""
    try:
        module = importlib.import_module("factory.dag")
        return module.build_plan, "factory.dag.build_plan"
    except ImportError:
        return _current_behavior_plan, "回退：adapter._topological_order（当前实现，只有 order）"


BUILD_PLAN, PLAN_SOURCE = _load_build_plan()


def plan_of(graph: Graph) -> Mapping[str, Any]:
    return BUILD_PLAN(graph)


def _require_field(plan: Mapping[str, Any], field: str) -> Any:
    """取字段；缺失时给出**指向缺口**的失败信息，而不是 KeyError。"""
    if field not in plan:
        pytest.fail(
            f"计划缺少字段 `{field}` —— 当前实现的产物只有 "
            f"{sorted(plan)}（来源：{PLAN_SOURCE}）。"
            f"这正是 DAG_DESIGN.md 记录的缺口：该性质**没有被报告出来**。"
        )
    return plan[field]


# ═══════════════════════════════════════════════════════════════════════════
# 缺口 1：悬空依赖（声明了依赖，但依赖不在本次输入集合内）
# ═══════════════════════════════════════════════════════════════════════════


def test_DAG_D1_dangling_field_exists() -> None:
    """D1 给定图，某需求声明依赖不在输入集合内 -> 计划须含 dangling_dependencies 字段。"""
    plan = plan_of({"A": ["B"], "C": []})       # B 不在图中
    _require_field(plan, "dangling_dependencies")


def test_DAG_D2_dangling_lists_the_missing_dep() -> None:
    """D2 dangling_dependencies 必须列出该集合外依赖 —— 非 None、非空。"""
    plan = plan_of({"A": ["B"], "C": []})
    dangling = _require_field(plan, "dangling_dependencies")
    assert dangling, f"dangling_dependencies 为空，但 A 声明的 B 不在图中；实际={dangling!r}"
    listed = {d for deps in _as_dep_map(dangling).values() for d in deps}
    assert "B" in listed, f"悬空依赖 B 未被列出；实际={dangling!r}"


def test_DAG_D3_no_false_positive_for_in_set_deps() -> None:
    """D3 集合内的依赖不得出现在 dangling_dependencies —— 没有假阳性。"""
    plan = plan_of({"A": ["B"], "B": [], "C": []})
    dangling = _require_field(plan, "dangling_dependencies")
    listed = {d for deps in _as_dep_map(dangling).values() for d in deps}
    assert "B" not in listed, f"B 在输入集合内，不该被判为悬空；实际={dangling!r}"
    assert _as_dep_map(dangling) == {} or all(not v for v in _as_dep_map(dangling).values()), \
        f"此图无悬空依赖，应为空；实际={dangling!r}"


def test_DAG_D4_multiple_dangles_all_reported() -> None:
    """D4 多个悬空依赖全部被报告 —— 不是只报第一个。"""
    plan = plan_of({"A": ["B", "C", "D"], "E": ["F"]})
    dangling = _require_field(plan, "dangling_dependencies")
    listed = {d for deps in _as_dep_map(dangling).values() for d in deps}
    missing = {"B", "C", "D", "F"} - listed
    assert not missing, f"以下悬空依赖未被报告: {sorted(missing)}；实际={dangling!r}"
    assert len(listed) == 4, f"应报告 4 个悬空依赖，实际 {len(listed)}: {sorted(listed)}"


# ═══════════════════════════════════════════════════════════════════════════
# 缺口 2：依赖环（当前实现静默退回输入顺序）
# ═══════════════════════════════════════════════════════════════════════════


def test_DAG_D5_cycle_field_exists() -> None:
    """D5 给定有环的图 -> 计划须含 cycle_detected 字段。"""
    plan = plan_of({"A": ["B"], "B": ["A"]})
    _require_field(plan, "cycle_detected")


def test_DAG_D6_cycle_detected_true() -> None:
    """D6 cycle_detected 为 True —— 不是 False，也不是 None。"""
    plan = plan_of({"A": ["B"], "B": ["A"]})
    assert _require_field(plan, "cycle_detected") is True


def test_DAG_D7_not_silent_fallback_to_input_order() -> None:
    """D7 有环时不得静默返回输入顺序 —— 要么顺序变化，要么显式标记。"""
    graph = {"A": ["B"], "B": ["A"]}
    plan = plan_of(graph)
    order = tuple(plan.get("order", ()))
    flagged = plan.get("cycle_detected") is True
    reordered = order != tuple(graph)
    assert flagged or reordered, (
        f"有环却既未标记也未重排：order={order} 与输入顺序 {tuple(graph)} 相同，"
        f"且 cycle_detected 不是 True（实际={plan.get('cycle_detected')!r}）——"
        "这正是「静默退回原顺序」的形态。"
    )


def test_DAG_D7b_cycle_must_be_flagged_not_merely_reordered() -> None:
    """D7b（补充，严格强于 D7）有环时**必须被显式标记**。

    为什么补这一条：D7 的规范是 `输出与输入顺序不同，或输出含 cycle_detected: True`，
    那个 `or` 让它变弱 —— 实测当前实现对任何环都会重排（DFS 后序追加），
    于是 D7 **无条件通过**，却证明不了「环被处理了」：

        输入 ('A','B') -> 输出 ('B','A')
        输入 ('B','A') -> 输出 ('A','B')     # 同一张图，输出随输入翻转

    「碰巧重排」不传递任何信息：调用方从 order 上看不出有环。
    所以真正要守的性质是**显式标记**，重排既非充分也非必要。
    """
    plan = plan_of({"A": ["B"], "B": ["A"]})
    assert plan.get("cycle_detected") is True, (
        "有环却未被显式标记 —— 仅靠重排不算处理。"
        "调用方无法从 order 推断出存在环，且输出顺序取决于输入顺序。"
    )


def test_DAG_D8_self_loop_detected() -> None:
    """D8 自环（A → A）也被检测。"""
    plan = plan_of({"A": ["A"]})
    assert _require_field(plan, "cycle_detected") is True, "自环是最短环，必须被检测"


def test_DAG_D9_no_false_cycle_positive() -> None:
    """D9 无环时 cycle_detected 为 False —— 没有假阳性。"""
    plan = plan_of({"A": [], "B": ["A"], "C": ["A", "B"]})   # 菱形，无环
    assert _require_field(plan, "cycle_detected") is False


# ═══════════════════════════════════════════════════════════════════════════
# 同构：执行层与核验工具不得各算一套图
# ═══════════════════════════════════════════════════════════════════════════


def test_DAG_D10_isomorphic_with_closure_tool() -> None:
    """D10 对同一图，计划与 tools/closure.py 的拓扑序、悬空依赖、深度完全一致。

    这是十条里最有价值的一条：它保证**执行层**与**架构分析工具**
    不会对同一张需求图给出两套不同答案。
    """
    import closure  # tools/closure.py

    graph = {
        "REQ-1": [],
        "REQ-5": [],
        "REQ-3": ["REQ-1"],
        "REQ-7": ["REQ-1", "REQ-5"],
        "REQ-11": ["REQ-1", "REQ-7"],
        "REQ-12": ["REQ-3", "REQ-11"],
    }
    nodes = set(graph)
    plan = plan_of(graph)

    # 深度必须一致
    expected_depths = closure.depths(nodes, dict(graph))
    levels = _require_field(plan, "levels")
    assert dict(levels) == expected_depths, (
        f"深度与 closure.depths 不一致：\n  计划={dict(levels)}\n  工具={expected_depths}"
    )

    # 拓扑序必须满足同一组偏序约束（不要求逐位相同：两者遍历顺序可不同）
    order = tuple(plan.get("order", ()))
    pos = {n: i for i, n in enumerate(order)}
    assert set(order) == nodes, f"拓扑序未覆盖全部节点：缺 {sorted(nodes - set(order))}"
    for node, deps in graph.items():
        for dep in deps:
            assert pos[dep] < pos[node], f"{dep} 必须在 {node} 之前；实际 order={order}"

    # 悬空依赖必须一致（此图是闭包，应为空）
    dangling = _require_field(plan, "dangling_dependencies")
    listed = {d for deps in _as_dep_map(dangling).values() for d in deps}
    assert listed == set(), f"闭包内不应有悬空依赖；实际={dangling!r}"


# ---------------------------------------------------------------------------
# 辅助：dangling 字段的两种合理形状都接受
# ---------------------------------------------------------------------------


def _as_dep_map(dangling: Any) -> dict[str, list[str]]:
    """把 dangling_dependencies 归一成 {节点: [缺失依赖]}。

    设计文档给的形状是 `dict[str, tuple[str, ...]]`；
    但 `list[str]`（只列缺失的名字）也是合理表达。两种都接受，
    断言只关心**该报的报了、不该报的没报**。
    """
    if dangling is None:
        return {}
    if isinstance(dangling, Mapping):
        return {str(k): [str(x) for x in (v or [])] for k, v in dangling.items()}
    if isinstance(dangling, (list, tuple, set)):
        return {"<flat>": [str(x) for x in dangling]}
    raise AssertionError(f"dangling_dependencies 形状无法识别: {type(dangling).__name__}")
