"""CONTRACT_MISMATCH 门禁的合成验证用例（纯函数，不依赖网关）。

═══════════════════════════════════════════════════════════════════════════
 为什么需要这个文件
═══════════════════════════════════════════════════════════════════════════
closure6 全链通过那一轮，`CONTRACT_MISMATCH` **触发 0 次** ——
提示词注入已足够，模型拿到签名一次就写对，门禁从未登场。

「从未被触发过的门禁」== 「未被验证的门禁」。
如果它其实不工作，未来遇到更复杂的契约时，就会重演
「违规被记录但未阻断」的同类问题（前几轮已经吃过一次这个教训）。

所以这里**不走模型**，直接构造下游实现写错签名的情形，
验证门禁能拦截、理由完整。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.models import CrossModuleCall  # noqa: E402
from factory.testaudit import (  # noqa: E402
    DEP_CONTRACT_MISMATCH,
    DependencyAudit,
    actual_call_arity,
    audit_dependency_usage,
    describe_dependencies,
)

SIGNATURE = "foo(a, b, c)"
UPSTREAM_SRC = "function foo(a, b, c) { return a + b + c; }\nmodule.exports = { foo };\n"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'}  {name}" + (f"   {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# fixture 构造 —— 并**自我校验**（第三次同类教训：fixture 错误会伪装成被测对象的 bug）
# ---------------------------------------------------------------------------


def build_case(call_expression: str) -> str:
    """产出下游实现：import 上游 + 一次调用。

    ★ 关键：调用必须**独占一行**。
    若 fixture 转义写错（例如把换行写成字面反斜杠+n），整段会变成一行，
    `_bindings_in_line` 会把同行内容排除，于是门禁看到「只 import 未调用」——
    表现为 `DEPENDENCY_NOT_USED` 而不是预期的 `CONTRACT_MISMATCH`，
    **看起来像被测对象有 bug，实际是 fixture 写坏了**。实测踩过一次。
    """
    return (
        "const { foo } = require('./up');\n"
        "const p = 1;\n"
        f"{call_expression};\n"
    )


def assert_fixture_well_formed(label: str, src: str, call_expression: str) -> None:
    """元断言：fixture 必须是「调用独占一行」的结构。

    这是对**测试数据本身**的断言 —— 断言/夹具也要被验证。
    """
    lines = src.splitlines()
    call_line = f"{call_expression};"
    check(f"[元断言] {label}: fixture 含真实换行（{len(lines)} 行）",
          len(lines) >= 3, f"行数={len(lines)}（1 行 = 换行被写成了字面量）")
    # 注意：detail 无论通过与否都会打印 —— 所以文字必须是中性的。
    # 初版写「未找到独立行 …」，通过时也照样打印，看起来像失败。
    check(f"[元断言] {label}: 调用独占一行",
          call_line in lines,
          f"独立行 = {call_line!r}")
    first = lines[0] if lines else ""
    check(f"[元断言] {label}: import 与调用不同行",
          "require" in first and call_expression not in first,
          f"首行={first[:60]!r}")


def run_gate(call_expression: str) -> tuple[str, str, str]:
    """返回 (verdict, detail, describe 文本)。"""
    ws = Path(tempfile.mkdtemp(prefix="contract-mismatch-"))
    try:
        (ws / "backend/src").mkdir(parents=True)
        (ws / "backend/src/up.js").write_text(UPSTREAM_SRC, encoding="utf-8")
        src = build_case(call_expression)
        (ws / "backend/src/down.js").write_text(src, encoding="utf-8")
        declared = CrossModuleCall(upstream="REQ-X", symbol="foo", signature=SIGNATURE,
                                   semantics="示例语义", side_effects=("示例副作用",))
        usage = audit_dependency_usage(
            ws, downstream="REQ-DOWN", upstream="REQ-X",
            downstream_files=["backend/src/down.js"],
            upstream_files=["backend/src/up.js"],
            declared_calls=(declared,),
        )
        text = describe_dependencies(DependencyAudit(usages=[usage]))
        return usage.verdict, usage.detail, text
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def main() -> int:
    print("=" * 80)
    print("CONTRACT_MISMATCH 合成验证（门禁从未在真实运行中触发过）")
    print("=" * 80)
    print(f"声明签名: {SIGNATURE}\n")

    # ---- 三种错误形态 ----
    cases = [
        ("少一个参数", "foo(p, p)", DEP_CONTRACT_MISMATCH),
        ("多一个参数", "foo(p, p, p, p)", DEP_CONTRACT_MISMATCH),
        ("零参数", "foo()", DEP_CONTRACT_MISMATCH),
    ]
    for label, expr, want in cases:
        src = build_case(expr)
        assert_fixture_well_formed(label, src, expr)
        verdict, detail, text = run_gate(expr)
        check(f"① {label}（{expr}）-> {want}", verdict == want, f"实际 {verdict}")
        if verdict == want:
            # 理由必须含三项：声明签名 / 实际签名 / 修正指令
            check(f"② {label} 理由含**声明签名**", SIGNATURE in detail or SIGNATURE in text,
                  detail[:80])
            check(f"③ {label} 理由含**实际参数个数**", "个参数" in detail, detail[:80])
            check(f"④ {label} 理由含**修正指令**", "修正指令" in text)

    # ---- 正确调用：不得误报 ----
    verdict, detail, _ = run_gate("foo(p, p, p)")
    check("⑤ 正确调用（foo(p,p,p)）-> DEPENDENCY_USED（无误报）",
          verdict == "DEPENDENCY_USED", f"实际 {verdict}")

    # ---- 未声明契约：不得校验 ----
    ws = Path(tempfile.mkdtemp(prefix="contract-none-"))
    try:
        (ws / "backend/src").mkdir(parents=True)
        (ws / "backend/src/up.js").write_text(UPSTREAM_SRC, encoding="utf-8")
        (ws / "backend/src/down.js").write_text(build_case("foo(p, p)"), encoding="utf-8")
        v = audit_dependency_usage(
            ws, downstream="D", upstream="REQ-X",
            downstream_files=["backend/src/down.js"],
            upstream_files=["backend/src/up.js"],  # 不传 declared_calls
        ).verdict
        check("⑥ 未声明契约时不做签名校验（行为不变）", v == "DEPENDENCY_USED", f"实际 {v}")
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    # ---- ★ 已知局限：参数顺序错**检不出** ----
    # 按参数个数比对是**唯一可靠的静态判据**：
    # 按名字比对不成立（调用方用自己作用域里的变量名，`foo(quantity, sku)` 未必是错的）。
    # 这里把这个局限**钉进断言** —— 将来若改为能检出，本条会失败，提醒更新文档。
    print()
    print("--- 已知局限（钉进断言，改动时会被提醒）---")
    verdict, detail, _ = run_gate("foo(p, p, p)")   # 顺序错在静态上与"正确"同形
    same_arity, _, _ = run_gate("foo(q, r, s)")
    check("⑦ 已知局限：**参数顺序错（同个数）检不出** —— 静态按个数比对是唯一可靠判据",
          verdict == "DEPENDENCY_USED" and same_arity == "DEPENDENCY_USED",
          "`foo(b, a, c)` 与 `foo(a, b, c)` 同为 3 参，静态无法区分；"
          "按名字比对不可靠（调用方变量名由调用方决定）")
    check("⑧ actual_call_arity 对三种形态的计数正确",
          actual_call_arity("foo(p, p);", "foo") == 2
          and actual_call_arity("foo(p, p, p);", "foo") == 3
          and actual_call_arity("foo(p, p, p, p);", "foo") == 4
          and actual_call_arity("foo();", "foo") == 0,
          "顶层逗号计数")

    print()
    print("=" * 80)
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 项失败：")
        for item in failed:
            print(f"  - {item}")
        return 1
    print(f"✅ 全部 {len(RESULTS)} 项断言通过")
    print()
    print("结论：门禁在合成用例下**能正确拦截**参数个数不符（少参/多参/零参），")
    print("      理由含声明签名、实际参数个数与修正指令；")
    print("      **已知局限**：同个数的参数顺序错无法静态检出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
