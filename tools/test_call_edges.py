"""call edge 方向断言 + node contract 落盘断言。

为什么需要单独立一条断言：
  call_edges / node_contracts 两张表长期为 0 行。接入之后，**方向**是最容易在
  下一次改动中被写反的东西——而且写反了不会报错（SDK 不做语义校验），只会让
  调用图静默变错。所以把方向约定固化成断言。

方向约定（与平台一致）：
    source_req_id = **调用方**（声明了 dependencies 的需求）
    target_req_id = **被调用方**（被依赖的需求）
    A depends_on B  ->  edge(source_req_id=A, target_req_id=B)

用法：python3 tools/test_call_edges.py      全部通过退出码 0，否则 1
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from arcbench_agent_runtime import AgentRuntime  # noqa: E402

from factory.models import InterfaceSpec, Requirement  # noqa: E402
from factory.store import FactoryStore  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {label}")
    else:
        FAILED += 1
        print(f"  ❌ {label}    {detail}")


def _read(root: Path, table: str) -> dict:
    return json.loads((root / ".arc" / "traceability" / f"{table}.json").read_text(encoding="utf-8"))


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="calledge-check-"))
    try:
        runtime = AgentRuntime.from_env(project_dir=str(workspace))
        store = FactoryStore(runtime)
        store.init()

        print("call edge 方向（source=调用方）")
        # REQ-A 依赖 REQ-B  =>  A 调用 B
        caller = Requirement(req_id="REQ-A", name="调用方", dependencies=("REQ-B",))
        callee = Requirement(req_id="REQ-B", name="被调用方")
        standalone = Requirement(req_id="REQ-C", name="无依赖")
        store.record_dependency_edges([caller, callee, standalone])

        edges = _read(workspace, "call_edges")
        check("T-CE1 有依赖则写入 call edge", len(edges) == 1, f"实际 {len(edges)} 条")
        check("T-CE2 无依赖的需求不产生边", len(edges) == 1, f"实际 {len(edges)} 条")

        edge = next(iter(edges.values()))
        check("T-CE3 source_req_id = 调用方 REQ-A", edge["source_req_id"] == "REQ-A",
              f"实际 {edge['source_req_id']!r}")
        check("T-CE4 target_req_id = 被调用方 REQ-B", edge["target_req_id"] == "REQ-B",
              f"实际 {edge['target_req_id']!r}")
        # 这条是防翻转的核心断言：把 depends_on 读成「B 被 A 依赖」就会写成 (B, A)
        check("T-CE5 方向未翻转（不得为 source=REQ-B, target=REQ-A）",
              not (edge["source_req_id"] == "REQ-B" and edge["target_req_id"] == "REQ-A"),
              f"实际 {edge['source_req_id']}->{edge['target_req_id']}")
        check("T-CE6 edge_type 存在（用 SDK 默认值，未臆造枚举）",
              bool(str(edge.get("edge_type", "")).strip()), f"实际 {edge.get('edge_type')!r}")

        print("node contract 落盘")
        iface = InterfaceSpec(
            interface_id="REQ-A.API.items",
            req_ids=("REQ-A",),
            type="api",
            content="GET /api/items",
            file_path="backend/src/routes/items.js",
            first_line="3",
        )
        store.record_node_contract("REQ-A", (iface,))

        contracts = _read(workspace, "node_contracts")
        check("T-CE7 node_contracts 非空", len(contracts) == 1, f"实际 {len(contracts)} 行")
        row = contracts.get("REQ-A") or {}
        ifaces = (row.get("content") or {}).get("interfaces") or []
        check("T-CE8 content 携带接口清单", len(ifaces) == 1, f"实际 {len(ifaces)}")
        check("T-CE9 content 的 interface_id 正确",
              bool(ifaces) and ifaces[0].get("interface_id") == "REQ-A.API.items",
              str(ifaces[:1]))

        print("自动刷新信号")
        events = (workspace / ".arc" / "runner-events.jsonl").read_text(encoding="utf-8").splitlines()
        reasons = [json.loads(ln).get("reason") for ln in events if ln.strip()]
        check("T-CE10 写入后产生了 call_edges_updated 刷新信号",
              "call_edges_updated" in reasons, str(reasons))
        check("T-CE11 写入后产生了 node_contract_updated 刷新信号",
              "node_contract_updated" in reasons, str(reasons))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    print()
    if FAILED:
        print(f"❌ {FAILED} 项断言失败（通过 {PASSED} 项）")
        return 1
    print(f"✅ 全部 {PASSED} 项 call edge / node contract 断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
