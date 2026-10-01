"""import 冒烟守卫：**所有 factory 模块必须可导入，且跨模块符号必须都存在**。

═══════════════════════════════════════════════════════════════════════════
 为什么需要它（真实事故）
═══════════════════════════════════════════════════════════════════════════
某轮我在两个 git worktree 之间用 `cp` 同步文件，把**另一条工作流**的
`loop.py` / `pipeline.py` / `test_gates.py` 复制了过来 —— 它们引用
`ModelQuotaExhaustedError` / `_is_quota_exhausted`，而本分支的 `llm.py`
是重构前的版本，**没有这两个符号**。

后果：
  • `import factory` 直接 ImportError
  • 我随后那次 closure6 运行 **5 秒内失败**（EXIT=1）
  • 而**没有任何检查发现它** —— check_all 是在主树跑的，
    分支上的 check_all 从未被执行过

也就是说：分支上有 4 个提交（b062436 → 7dd9394）都是**不可导入**的，
却一路「绿色」通过了我的报告。

这条断言把一个「跑起来才知道」的问题变成**秒级可查**：
    python3 tools/test_imports.py

═══════════════════════════════════════════════════════════════════════════
 检查两层
═══════════════════════════════════════════════════════════════════════════
  ① 每个 factory.* 子模块都能 import（捕捉跨模块符号缺失）
  ② 第三方依赖（yaml 等）在本环境可用（捕捉环境漂移）
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'}  {name}" + (f"   {detail}" if detail else ""))


def main() -> int:
    print("=" * 78)
    print("import 冒烟守卫：跨模块符号一致性")
    print("=" * 78)

    # ---- ① 逐模块导入 ----
    try:
        import factory
    except Exception:  # noqa: BLE001
        check("① 导入 factory 包", False,
              "包级导入即失败 —— 下面给出完整栈:\n" + traceback.format_exc())
        print()
        print("=" * 78)
        print("❌ 1/1 项失败：factory 包无法导入")
        print("   最常见原因：某模块 import 了同项目另一模块**不存在的符号**")
        print("   （例如跨 worktree 复制文件后，两边的 llm.py 版本不一致）")
        return 1

    check("① 导入 factory 包", True)

    mods = sorted(m.name for m in pkgutil.iter_modules(factory.__path__))
    bad: list[tuple[str, str]] = []
    for name in mods:
        full = f"factory.{name}"
        try:
            importlib.import_module(full)
        except Exception as exc:  # noqa: BLE001
            bad.append((full, f"{type(exc).__name__}: {exc}"))
    check(f"② 逐个导入 {len(mods)} 个子模块（跨模块符号齐全）",
          not bad,
          "; ".join(f"{n} -> {e}" for n, e in bad) if bad else
          "、".join(mods))
    if bad:
        for n, e in bad:
            print(f"      ❌ {n}: {e}")

    # ---- ③ 关键模块的公开符号可用性（把最常见的断裂点单列）----
    # 这些符号曾因跨 worktree 复制而缺失；单列出来让报错更指向问题。
    expectations = [
        ("factory.llm", ["ModelClient", "CallStats", "ModelCallError"]),
        ("factory.loop", ["TddLoop"]),
        ("factory.pipeline", ["run_factory"]),
        ("factory.contracts", ["write_contracts", "check_contracts", "load_frozen_calls"]),
        ("factory.testplan", ["apply_root_test_limit", "root_case_limit_prompt"]),
    ]
    missing: list[str] = []
    for mod_name, names in expectations:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{mod_name} 无法导入: {exc}")
            continue
        for n in names:
            if not hasattr(mod, n):
                missing.append(f"{mod_name}.{n} 不存在")
    check("③ 关键模块的公开符号存在", not missing,
          "; ".join(missing) if missing else
          "、".join(m for m, _ in expectations))

    # ---- ④ 环境依赖 ----
    env_bad: list[str] = []
    for dep in ("yaml",):
        try:
            importlib.import_module(dep)
        except Exception as exc:  # noqa: BLE001
            env_bad.append(f"{dep}: {exc}")
    check("④ 第三方依赖可用", not env_bad, "; ".join(env_bad) or "yaml")

    print()
    print("=" * 78)
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 项失败：")
        for item in failed:
            print(f"  - {item}")
        print()
        print("★ 跨 worktree 复制文件后**务必**跑本入口 —— 两边的同名模块")
        print("  可能是不同版本，import 是最先断裂的地方。")
        return 1
    print(f"✅ 全部 {len(RESULTS)} 项断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
