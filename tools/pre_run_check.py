"""跑前检查：网关健康 + 拓扑序人工确认。

在跑长任务（12 需求 = 数十次模型调用）之前做两件事：

1. **网关健康**：连续 3 次简单调用，全通过才开跑。
   实测同一配置同一天出现"通过 / 失败 / 失败"，12 需求会把暴露机会成倍放大。
   任一失败 => 退出码 1，不进入正式运行。

2. **拓扑序**：打印执行顺序、根节点、最长链、菱形汇聚点，供人工确认。
   依赖顺序错了会让后面的需求在错误的代码基线上执行。

用法：
  python3 tools/pre_run_check.py [--requirements requirements_probe] [--yes]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.adapter import _adapt  # noqa: E402
from factory.llm import ModelClient  # noqa: E402
from factory.workspace import load_requirements_raw  # noqa: E402

HEALTH_CALLS = 3   # 默认；用 --health-count 覆盖（长任务建议 5）


def check_fields() -> tuple[bool, list[str]]:
    """确认跑时需要记录的字段确实存在（而不是跑完才发现记不了）。"""
    print("=" * 78)
    print("② 字段可记录性检查")
    print("=" * 78)
    from factory.models import (  # noqa: PLC0415
        REQUIREMENT_RESULT_FIELDS,
        RequirementResult,
        RunReport,
    )

    needed = ["write_attempts", "test_rewrites", "weakening_violations", "unauthorized_files", "cost"]
    missing_attr = [n for n in needed if not hasattr(RequirementResult(req_id="R", state="S"), n)]
    missing_schema = [n for n in needed if n not in REQUIREMENT_RESULT_FIELDS]

    probe = RequirementResult(req_id="R", state="S")
    probe.write_attempts = 1
    probe.test_rewrites = 2
    probe.weakening_violations = [{"code": "X"}]
    row = RunReport(project_name="p", requirements_total=1, results=[probe]).to_dict()["results"][0]
    missing_output = [n for n in needed if n not in row]

    lines = []
    for name in needed:
        ok = name not in missing_attr and name not in missing_schema and name not in missing_output
        lines.append(f"  {'✅' if ok else '❌'} {name}")
    print("\n".join(lines))
    ok = not (missing_attr or missing_schema or missing_output)
    if not ok:
        print(f"  缺失: 属性={missing_attr} schema={missing_schema} 输出={missing_output}")
    return ok, lines


def check_gateway(client: ModelClient) -> tuple[bool, list[str]]:
    print("=" * 78)
    print(f"① 网关健康检查（连续 {HEALTH_CALLS} 次简单调用）")
    print("=" * 78)
    results: list[str] = []
    ok = True
    for i in range(1, HEALTH_CALLS + 1):
        t0 = time.time()
        try:
            text = client.complete(
                system="Reply with JSON only.",
                user='Return {"ok": true} as JSON.',
                json_mode=True,
            )
            elapsed = time.time() - t0
            results.append(f"  第 {i} 次: ✅ {elapsed:5.1f}s  响应 {text[:40]!r}")
        except Exception as exc:  # noqa: BLE001
            elapsed = time.time() - t0
            results.append(f"  第 {i} 次: ❌ {elapsed:5.1f}s  {type(exc).__name__}: {str(exc)[:120]}")
            ok = False
    for line in results:
        print(line)
    print(f"\n  网关状态: {'通过 ✅ 可以开跑' if ok else '失败 ❌ 停跑，报用户'}")
    return ok, results


def check_topology(requirements_file: Path) -> tuple[bool, list[str], dict]:
    req_set = _adapt(load_requirements_raw(requirements_file), source=requirements_file)
    reqs = list(req_set.requirements)
    deps = {r.req_id: list(r.dependencies) for r in reqs}
    order = [r.req_id for r in reqs]
    pos = {rid: i for i, rid in enumerate(order)}

    print()
    print("=" * 78)
    print(f"② 拓扑序（{len(order)} 个需求，{requirements_file.name}）")
    print("=" * 78)
    for i, rid in enumerate(order, 1):
        d = deps[rid]
        print(f"  {i:>2}. {rid:<8} 依赖: {', '.join(d) if d else '(根)'}")

    # 依赖可能不在集合内（跑子集时很常见）。此时必须**明确报告**，
    # 而不是 KeyError 崩掉——否则工具本身就成了新的静默失败源。
    dangling = {r: [x for x in ds if x not in pos] for r, ds in deps.items()}
    dangling = {r: v for r, v in dangling.items() if v}
    violations = [
        (r, x) for r, ds in deps.items() for x in ds if x in pos and pos[x] > pos[r]
    ]
    roots = sorted(r for r, ds in deps.items() if not ds)

    def depth(rid: str, memo: dict[str, int]) -> int:
        if rid in memo:
            return memo[rid]
        present = [x for x in deps[rid] if x in deps]   # 悬空依赖不参与深度计算
        value = 0 if not present else 1 + max(depth(x, memo) for x in present)
        memo[rid] = value
        return value

    memo: dict[str, int] = {}
    depths = {r: depth(r, memo) + 1 for r in deps}
    longest = max(depths.values())
    chain = sorted((r for r, d in depths.items() if d == longest))

    def ancestors(rid: str) -> set[str]:
        if rid not in deps:
            return set()
        out: set[str] = set()
        stack = [x for x in deps[rid] if x in deps]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            stack.extend(x for x in deps[cur] if x in deps)
        return out

    joins = {r: ds for r, ds in deps.items() if len(ds) >= 2}
    diamonds = []
    for rid, ds in joins.items():
        # 只对集合内存在的依赖判断菱形；悬空依赖已在上面单独报告
        present = [d for d in ds if d in deps]
        shared = set.intersection(*(ancestors(d) | {d} for d in present)) if len(present) >= 2 else set()
        if shared:
            diamonds.append((rid, ds, sorted(shared)))

    print()
    print(f"  根节点 ({len(roots)}): {roots}")
    print(f"  最长链深度: {longest} 层，末端: {chain}")
    print(f"  汇聚点（多重依赖，{len(joins)} 个）: {sorted(joins)}")
    print(f"  菱形（依赖共享祖先，{len(diamonds)} 个）:")
    for rid, ds, shared in diamonds:
        print(f"    {rid} <- {ds}  共享祖先: {shared}")
    print(f"  拓扑序合法性: {'✅ 全部依赖先于自身' if not violations else f'❌ 违反 {violations}'}")
    if dangling:
        print(f"  ⚠️ 悬空依赖（集合外，不会被执行）: {dangling}")
        print("     影响：这些需求的测试可能因为依赖模块不存在而失败，")
        print("           失败归因时必须与「实现能力不足」区分开。")

    summary = {
        "count": len(order),
        "order": order,
        "roots": roots,
        "longest_depth": longest,
        "longest_tail": chain,
        "joins": sorted(joins),
        "diamonds": [{"node": r, "deps": d, "shared_ancestors": s} for r, d, s in diamonds],
        "topology_valid": not violations,
        "dangling_dependencies": dangling,
    }
    return not violations, order, summary


def main() -> int:
    global HEALTH_CALLS
    parser = argparse.ArgumentParser(description="跑前检查：网关健康 + 字段可记录 + 拓扑序")
    parser.add_argument("--requirements", default="requirements_probe")
    parser.add_argument("--skip-gateway", action="store_true", help="跳过网关检查（仅看拓扑序）")
    parser.add_argument("--health-count", type=int, default=HEALTH_CALLS,
                        help="网关健康检查的连续调用次数（默认 3）")
    args = parser.parse_args()
    HEALTH_CALLS = args.health_count

    gateway_ok = True
    if not args.skip_gateway:
        client = ModelClient()
        print(f"模型: {client.model}   后端: {client.backend()}   {client.base_url}\n")
        if not client.is_available():
            print("❌ 模型不可用：缺少 OPENAI_API_KEY / OPENAI_BASE_URL / MODEL")
            return 1
        gateway_ok, _ = check_gateway(client)

    fields_ok, _ = check_fields()

    req_file = ROOT / args.requirements / "requirements.yaml"
    topo_ok, _, summary = check_topology(req_file)

    print()
    print("=" * 78)
    if gateway_ok and topo_ok and fields_ok:
        print("✅ 跑前检查全部通过 —— 可以开跑")
        print(f"   （网关 5x={'✅' if gateway_ok else '❌'} / "
              f"字段={'✅' if fields_ok else '❌'} / 拓扑={'✅' if topo_ok else '❌'}）")
        return 0
    print(f"❌ 跑前检查未通过（网关 {'✅' if gateway_ok else '❌'} / "
          f"字段 {'✅' if fields_ok else '❌'} / 拓扑 {'✅' if topo_ok else '❌'}）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
