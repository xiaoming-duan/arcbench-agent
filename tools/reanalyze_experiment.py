"""对 experiment_weak_feedback 的结果做更严格口径的重新分析。

为什么要重算：
  原实验的判定是"这一组测试文件整体是否失败（RED）"。
  但实测发现模型会**另外新增一个会失败的测试文件**，而把原来那个空转文件原封不动留着。
  整体跑当然失败 -> 被记为成功，可真正要修的那个文件根本没被修。

严格口径（逐种子文件）：
  1. 种子文件必须仍然存在；
  2. **单独运行种子文件**必须失败（RED）；
  3. 种子文件必须引用了 backend/src 下的实现模块。

本脚本只用已存在的工作区做静态+执行分析，不调用模型。
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.testaudit import audit_tests  # noqa: E402
from factory.testrunner import NodeTestRunner  # noqa: E402

# 从实验日志中记录的种子文件路径
SEED_BY_REQ = {
    "REQ-1": "backend/tests/items.list.test.js",
    "REQ-2": "backend/tests/find-by-sku.test.js",
    "REQ-3": "backend/tests/summarize-items.test.js",
    "REQ-4": "backend/tests/validateItem.test.js",
}

VARIANTS = ("basic", "import_aware")


def main() -> int:
    exp_dir = ROOT / "out-exp"
    rows = []
    for ws in sorted(exp_dir.glob("REQ-*")):
        if not ws.is_dir() or ws.name.startswith("_seedcheck"):
            continue
        parts = ws.name.rsplit("-", 2)          # REQ-x, variant, rN
        if len(parts) != 3:
            continue
        req_id, variant, rep = parts[0], parts[1], parts[2]
        seed_rel = SEED_BY_REQ.get(req_id)
        if not seed_rel:
            continue

        seed_path = ws / seed_rel
        exists = seed_path.is_file()
        red = impl = False
        all_tests = sorted(p.name for p in (ws / "backend/tests").glob("*.test.js"))
        if exists:
            runner = NodeTestRunner(ws, timeout_s=120)
            rel = seed_rel[len("backend/"):]
            red = not runner.run([rel]).passed
            impl = audit_tests(ws, [seed_rel], implementation_root="backend/src").imports_implementation
        rows.append({
            "trial": ws.name, "req": req_id, "variant": variant, "rep": rep,
            "seed_exists": exists, "seed_red": red, "seed_imports_impl": impl,
            "test_files": all_tests,
        })

    print(f"{'trial':<30}{'种子仍在':<10}{'种子单独RED':<13}{'种子引用实现':<13}{'目录下测试文件数'}")
    for r in rows:
        print(f"{r['trial']:<30}{str(r['seed_exists']):<10}{str(r['seed_red']):<13}"
              f"{str(r['seed_imports_impl']):<13}{len(r['test_files'])}")

    print("\n" + "=" * 72)
    print(f"{'处理组':<16}{'严格口径：种子文件被修好':<26}{'宽松口径：整体 RED'}")
    summary = {}
    for variant in VARIANTS:
        sub = [r for r in rows if r["variant"] == variant]
        strict = sum(1 for r in sub if r["seed_red"] and r["seed_imports_impl"])
        summary[variant] = {"strict": f"{strict}/{len(sub)}", "n": len(sub)}
        print(f"{variant:<16}{f'{strict}/{len(sub)}':<26}{'-'}")
    print("=" * 72)

    (ROOT / "experiment_reanalysis.json").write_text(
        json.dumps({"rows": rows, "summary": summary}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("已写入 experiment_reanalysis.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
