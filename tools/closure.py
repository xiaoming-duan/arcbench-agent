"""传递闭包计算：需求列表 + 拓扑深度 + 枢纽需求 + 悬空依赖。

=======================  为什么四个输出都要  =======================
只给需求列表不够用：
  拓扑深度   —— 最深节点是最可能失败的（依赖累积压力最大）
  枢纽需求   —— 被多个下游依赖的节点，失败影响面最大
  悬空依赖   —— 闭包外的依赖；若有，说明种子集合不完整，结论会被污染

=======================  策略（按闭包大小）  =======================
  ≤ 8    → 全跑
  9–12   → 跑闭包，记录新增传递依赖
  > 12   → 选最短完整链，只跑那条链的闭包

用法：
  python3 tools/closure.py                    # 全量探针集
  python3 tools/closure.py --target REQ-12    # 只算某个需求的传递闭包
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402


def load_graph(path: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    deps = {r["id"]: list(r.get("depends_on") or []) for r in data["requirements"]}
    names = {r["id"]: r.get("name", "") for r in data["requirements"]}
    return deps, names


def closure_of(targets: set[str], deps: dict[str, list[str]]) -> set[str]:
    """自底向上求传递闭包（含 targets 自身）。"""
    seen: set[str] = set()
    stack = list(targets)
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(deps.get(node, []))
    return seen


def depths(nodes: set[str], deps: dict[str, list[str]]) -> dict[str, int]:
    """最长路径深度（根 = 0）。闭包内无环时良定义。"""
    memo: dict[str, int] = {}

    def walk(node: str, seen: frozenset[str]) -> int:
        if node in memo:
            return memo[node]
        if node in seen:
            return 0  # 环保护
        parents = [d for d in deps.get(node, []) if d in nodes]
        value = 0 if not parents else 1 + max(walk(p, seen | {node}) for p in parents)
        memo[node] = value
        return value

    for node in nodes:
        walk(node, frozenset())
    return memo


def main() -> int:
    parser = argparse.ArgumentParser(description="传递闭包与拓扑分析")
    parser.add_argument("--requirements", default="requirements_probe")
    parser.add_argument("--target", default="", help="只算该需求的传递闭包（逗号分隔）")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    req_file = ROOT / args.requirements / "requirements.yaml"
    deps, names = load_graph(req_file)
    all_ids = set(deps)

    if args.target:
        targets = {t.strip() for t in args.target.split(",") if t.strip()}
        unknown = targets - all_ids
        if unknown:
            print(f"❌ 未知需求: {sorted(unknown)}")
            return 1
        nodes = closure_of(targets, deps)
        mode = f"闭包 of {sorted(targets)}"
    else:
        nodes = set(all_ids)
        mode = "全量探针集"

    dep = depths(nodes, deps)

    # 悬空依赖：被引用但不在闭包内的依赖
    dangling: dict[str, list[str]] = {}
    for node in nodes:
        miss = [d for d in deps.get(node, []) if d not in nodes]
        if miss:
            dangling[node] = miss
    # 引用完全未知需求（探针集缺失）
    unknown_refs = {
        node: [d for d in deps.get(node, []) if d not in all_ids]
        for node in nodes
        if any(d not in all_ids for d in deps.get(node, []))
    }

    # 枢纽：闭包内被直接依赖的次数
    indeg = {n: 0 for n in nodes}
    for node in nodes:
        for d in deps.get(node, []):
            if d in nodes:
                indeg[d] += 1
    hubs = sorted(((n, c) for n, c in indeg.items() if c >= 2),
                  key=lambda kv: (-kv[1], kv[0]))

    max_depth = max(dep.values()) if dep else 0
    deepest = sorted(n for n, v in dep.items() if v == max_depth)
    leaves = sorted(n for n in nodes if not any(n in deps.get(o, []) for o in nodes))

    size = len(nodes)
    strategy = "full" if size <= 8 else ("closure" if size <= 12 else "shortest_chain")

    print("=" * 84)
    print(f"传递闭包分析 —— {mode}")
    print("=" * 84)
    print(f"\n① 需求列表（{size} 个）:")
    for node in sorted(nodes, key=lambda x: (dep.get(x, 0), int(x.split('-')[1]))):
        mark = "★" if node in deepest else " "
        print(f"  {mark} {node:<8}{names.get(node, ''):<16}深度 {dep.get(node, 0)}  "
              f"依赖 {deps.get(node, []) or '(根)'}")

    print(f"\n② 拓扑深度（最长链 {max_depth + 1} 层）:")
    for level in range(max_depth + 1):
        layer = sorted(n for n, v in dep.items() if v == level)
        if layer:
            print(f"  第 {level} 层: {layer}")
    print(f"  最深节点（最可能失败）: {deepest}")

    print(f"\n③ 枢纽需求（被 ≥2 个下游依赖，失败影响面最大）:")
    if hubs:
        for node, count in hubs:
            downstream = sorted(o for o in nodes if node in deps.get(o, []))
            print(f"  {node:<8}{names.get(node, ''):<16}被 {count} 个下游依赖: {downstream}")
    else:
        print("  （无）")

    print(f"\n④ 悬空依赖:")
    if unknown_refs:
        print(f"  ⚠️ 引用了**探针集之外**的需求（种子集合不完整）: {unknown_refs}")
    else:
        print("  ✅ 无引用探针集之外的需求")
    if dangling:
        print(f"  闭包外依赖（相对本次闭包）: {dangling}")
    else:
        print("  ✅ 闭包内自洽，无悬空依赖")

    print(f"\n⑤ 策略: 闭包大小 {size} -> "
          + {"full": "≤8，全跑", "closure": "9–12，跑闭包并记录新增传递依赖",
             "shortest_chain": ">12，选最短完整链，只跑那条链的闭包"}[strategy])
    print(f"  末端（无下游）: {leaves}")

    if args.json:
        (ROOT / args.json).write_text(json.dumps({
            "requirements": sorted(nodes),
            "topological_depth": dep,
            "max_depth": max_depth,
            "deepest": deepest,
            "hub_requirements": [{"req": n, "dependents": c} for n, c in hubs],
            "dangling_dependencies": unknown_refs,
            "closure_relative_dangling": dangling,
            "size": size,
            "strategy": strategy,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
