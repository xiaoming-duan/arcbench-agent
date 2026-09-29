"""B（依赖使用审计）的断言测试：不调用模型，用合成复现真实病理。

==========================  被测的病理  ==========================
实测（chain5 运行）：REQ-12 声明依赖 REQ-3，但
  - REQ-3 在设计阶段失败，从未产出任何文件
  - REQ-12 的实现 report.js **没有任何 require**，依赖走参数注入
  - REQ-12 的测试注入了一个假的 summarizeItems
  - 结果：REQ-12 PASSED

这过了 RED 门禁，也过了 import 审计（它确实 import 了自己的 report.js）。
「依赖是否被真实使用」此前无任何检测。
==================================================================
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.testaudit import (  # noqa: E402
    DEP_FAKE,
    INJECTION_BYPASS,
    MOCK_DECLARED,
    UNVERIFIED_DEPENDENCY,
    audit_injection_bypass,
    audit_mocked_dependencies,
    describe_injection_bypass,
    describe_mocked_dependencies,
    extract_mock_targets,
    DEP_NOT_USED,
    DEP_SKIPPED_INDIRECT,
    DEP_UNCERTAIN,
    DEP_UPSTREAM_MISSING,
    DEP_USED,
    audit_dependency_usage,
    audit_requirement_dependencies,
    describe_dependencies,
    extract_exports,
)

logging.basicConfig(level=logging.CRITICAL)
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'}  {name}" + (f"   {detail}" if detail else ""))


UPSTREAM = "backend/src/services/summary.js"
UPSTREAM_SRC = """'use strict';
function summarizeItems(items = [], threshold = 10) {
  return { total: items.length, totalQuantity: 0, lowStock: 0 };
}
module.exports = { summarizeItems };
"""

DOWNSTREAM = "backend/src/services/report.js"
# 逐字复现 chain5 里 REQ-12 的真实实现形态：无 require，依赖走参数注入
DOWNSTREAM_PARAM_INJECTION = """'use strict';
function buildReport(items = [], movements = [], summarizeItems) {
  const summary = typeof summarizeItems === 'function' ? summarizeItems(items) : {};
  return { summary, movements };
}
module.exports = { buildReport };
"""


def ws_with(upstream: bool = True, downstream: str = DOWNSTREAM_PARAM_INJECTION) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="dep-audit-"))
    (ws / "backend/src/services").mkdir(parents=True)
    if upstream:
        (ws / UPSTREAM).write_text(UPSTREAM_SRC, encoding="utf-8")
    (ws / DOWNSTREAM).write_text(downstream, encoding="utf-8")
    return ws


def run(ws: Path, *, up_files=None, indirect: str = ""):
    return audit_dependency_usage(
        ws, downstream="REQ-12", upstream="REQ-3",
        downstream_files=[DOWNSTREAM],
        upstream_files=[UPSTREAM] if up_files is None else up_files,
        indirect_reason=indirect,
    )


def main() -> int:
    print("=" * 78)
    print("B：依赖使用审计断言测试")
    print("=" * 78)

    # ---- 真实病理 ----
    ws = ws_with(upstream=True)
    u = run(ws)
    check("① 真实病理：参数注入、完全不 import 上游 -> DEPENDENCY_NOT_USED",
          u.verdict == DEP_NOT_USED, u.detail[:78])
    check("①b 该判定属于违规（会阻断）", u.violation)
    shutil.rmtree(ws, ignore_errors=True)

    # ---- 上游未产出（REQ-3 失败的真实情况）----
    ws = ws_with(upstream=False)
    u = run(ws, up_files=[])
    check("② 上游未产出任何文件 -> UPSTREAM_MISSING（违规）",
          u.verdict == DEP_UPSTREAM_MISSING and u.violation, u.detail[:78])
    shutil.rmtree(ws, ignore_errors=True)

    # ---- 只 import 不调用 ----
    ws = ws_with()
    (ws / DOWNSTREAM).write_text(
        "const { summarizeItems } = require('./summary');\n"
        "function buildReport(items) { return { summary: {} }; }\n"
        "module.exports = { buildReport };\n", encoding="utf-8")
    u = run(ws)
    check("③ 只 import 不引用 -> DEPENDENCY_NOT_USED", u.verdict == DEP_NOT_USED, u.detail[:78])
    shutil.rmtree(ws, ignore_errors=True)

    # ---- 访问非导出成员 ----
    ws = ws_with()
    (ws / DOWNSTREAM).write_text(
        "const summary = require('./summary');\n"
        "function buildReport() { return summary.computeTotals(); }\n"
        "module.exports = { buildReport };\n", encoding="utf-8")
    u = run(ws)
    check("④ 访问非导出成员 -> FAKE_DEPENDENCY",
          u.verdict == DEP_FAKE, u.detail[:90])
    shutil.rmtree(ws, ignore_errors=True)

    # ---- 真实调用：解构 ----
    ws = ws_with()
    (ws / DOWNSTREAM).write_text(
        "const { summarizeItems } = require('./summary');\n"
        "function buildReport(items) { return { summary: summarizeItems(items) }; }\n"
        "module.exports = { buildReport };\n", encoding="utf-8")
    u = run(ws)
    check("⑤ 解构后真实调用 -> DEPENDENCY_USED",
          u.verdict == DEP_USED and "summarizeItems" in u.called_symbols, u.detail[:78])
    shutil.rmtree(ws, ignore_errors=True)

    # ---- 真实调用：命名空间 ----
    ws = ws_with()
    (ws / DOWNSTREAM).write_text(
        "const summary = require('./summary');\n"
        "function buildReport(items) { return { summary: summary.summarizeItems(items) }; }\n"
        "module.exports = { buildReport };\n", encoding="utf-8")
    u = run(ws)
    check("⑥ 命名空间成员调用 -> DEPENDENCY_USED",
          u.verdict == DEP_USED and "summarizeItems" in u.called_symbols, u.detail[:78])
    shutil.rmtree(ws, ignore_errors=True)

    # ---- 间接依赖显式声明 ----
    ws = ws_with()
    u = run(ws, indirect="由调用方注入 summarizeItems，见测试计划的 indirect_dependencies")
    check("⑦ 显式声明间接依赖 -> SKIPPED_INDIRECT（不阻断）",
          u.verdict == DEP_SKIPPED_INDIRECT and not u.violation and u.uncertain,
          u.detail[:70])
    shutil.rmtree(ws, ignore_errors=True)

    # ---- 导出解析 ----
    exports = extract_exports(UPSTREAM_SRC)
    check("⑧ 导出解析：module.exports = { summarizeItems }", exports == {"summarizeItems"}, str(exports))
    exports2 = extract_exports("exports.a = 1;\nexport function b(){}\nexport const c = 2;")
    check("⑧b 导出解析：exports.a / export function / export const",
          {"a", "b", "c"} <= exports2, str(sorted(exports2)))

    # ---- re-export 壳跟随 ----
    ws = Path(tempfile.mkdtemp(prefix="dep-reexport-"))
    (ws / "backend/src/services").mkdir(parents=True)
    (ws / "backend/src/services/real.js").write_text(
        "function doIt(){}\nmodule.exports = { doIt };\n", encoding="utf-8")
    (ws / "backend/src/services/shim.js").write_text(
        "module.exports = require('./real');\n", encoding="utf-8")
    (ws / DOWNSTREAM).write_text(
        "const { doIt } = require('./shim');\nfunction f(){ return doIt(); }\nmodule.exports={f};\n",
        encoding="utf-8")
    u = audit_dependency_usage(
        ws, downstream="D", upstream="U",
        downstream_files=[DOWNSTREAM], upstream_files=["backend/src/services/shim.js"])
    check("⑨ re-export 壳可被跟随 -> DEPENDENCY_USED", u.verdict == DEP_USED, u.detail[:70])
    shutil.rmtree(ws, ignore_errors=True)

    # ---- 解析不出导出 -> UNCERTAIN（不阻断）----
    ws = ws_with()
    (ws / UPSTREAM).write_text("// 无任何导出语句\n", encoding="utf-8")
    u = run(ws)
    check("⑩ 解析不出上游导出 -> DEPENDENCY_CHECK_UNCERTAIN（不阻断）",
          u.verdict == DEP_UNCERTAIN and not u.violation and u.uncertain, u.detail[:70])
    shutil.rmtree(ws, ignore_errors=True)

    # ---- 多依赖聚合 + 拒绝理由文本 ----
    ws = ws_with()
    audit = audit_requirement_dependencies(
        ws, downstream="REQ-12", declared_dependencies=["REQ-3", "REQ-11"],
        downstream_files=[DOWNSTREAM],
        produced_files={"REQ-3": [UPSTREAM], "REQ-11": []},
        indirect_dependencies={})
    text = describe_dependencies(audit)
    check("⑪ 多依赖聚合：1 个 NOT_USED + 1 个 UPSTREAM_MISSING",
          len(audit.violations) == 2, f"violations={[u.verdict for u in audit.violations]}")
    check("⑫ 拒绝理由含可执行指引（真实调用 + 间接声明出口）",
          "真实调用" in text and "indirect_dependencies" in text and "上游导出清单" in text)
    shutil.rmtree(ws, ignore_errors=True)

    # ==================== 方案 2-A：测试层 mock 检测 ====================
    print()
    print("=" * 78)
    print("方案 2-A：测试层 mock 上游检测")
    print("=" * 78)
    TEST_FILE = "backend/tests/req12.report.test.js"

    def mock_ws(mock_line: str) -> Path:
        ws = Path(tempfile.mkdtemp(prefix="mock-audit-"))
        (ws / "backend/src/services").mkdir(parents=True)
        (ws / "backend/tests").mkdir(parents=True)
        (ws / UPSTREAM).write_text(UPSTREAM_SRC, encoding="utf-8")
        (ws / TEST_FILE).write_text(
            "import { describe, it, expect, vi } from 'vitest';\n"
            f"{mock_line}\n"
            "describe('x', () => { it('y', () => { expect(1).toBe(1); }); });\n",
            encoding="utf-8")
        return ws

    # 未声明的 vi.mock 上游 -> 阻断
    ws = mock_ws("vi.mock('../src/services/summary', () => ({ summarizeItems: () => ({}) }));")
    a = audit_mocked_dependencies(
        ws, downstream="REQ-12", declared_dependencies=["REQ-3"],
        test_files=[TEST_FILE], produced_files={"REQ-3": [UPSTREAM]})
    check("A① 未声明 mock 上游 -> UNVERIFIED_DEPENDENCY（阻断）",
          a.violations and a.violations[0].verdict == UNVERIFIED_DEPENDENCY and not a.ok,
          a.violations[0].detail[:70] if a.violations else "")
    txt = describe_mocked_dependencies(a)
    check("A② 理由含 违规码 + mock 路径 + 上游导出 + 修复指引/合法出口",
          UNVERIFIED_DEPENDENCY in txt and UPSTREAM in txt and "summarizeItems" in txt
          and "真实调用上游" in txt and "mocked_dependencies" in txt)
    shutil.rmtree(ws, ignore_errors=True)

    # 已声明 -> 放行
    ws = mock_ws("vi.mock('../src/services/summary');")
    a = audit_mocked_dependencies(
        ws, downstream="REQ-12", declared_dependencies=["REQ-3"],
        test_files=[TEST_FILE], produced_files={"REQ-3": [UPSTREAM]},
        declared_mocks={"REQ-3": "上游依赖外部服务，测试期必须 mock"})
    check("A③ 已在测试计划声明 -> MOCK_DECLARED_OK（不阻断）",
          a.ok and a.declared and a.declared[0].verdict == MOCK_DECLARED,
          a.declared[0].detail[:60] if a.declared else "")
    shutil.rmtree(ws, ignore_errors=True)

    # mock 非依赖模块 -> 与本检查无关
    ws = mock_ws("vi.mock('axios');")
    a = audit_mocked_dependencies(
        ws, downstream="REQ-12", declared_dependencies=["REQ-3"],
        test_files=[TEST_FILE], produced_files={"REQ-3": [UPSTREAM]})
    check("A④ mock 非依赖模块（axios）-> 不产生判定", a.ok and not a.usages)
    shutil.rmtree(ws, ignore_errors=True)

    check("A⑤ mock 目标抽取覆盖 vi/jest/node:test",
          extract_mock_targets("vi.mock('a');jest.mock('b');mock.module('c');") == ["a", "b", "c"],
          str(extract_mock_targets("vi.mock('a');jest.mock('b');mock.module('c');")))

    # ==================== 方案 2-B：实现层注入旁路 ====================
    print()
    print("=" * 78)
    print("方案 2-B：实现层注入旁路检测（警告级）")
    print("=" * 78)
    INJ_IMPL = (
        "const { summarizeItems: upstreamSummarizeItems } = require('./summary');\n"
        "function buildReport(items = [], movements = [], summarizeItems) {\n"
        "  let summary;\n"
        "  if (typeof summarizeItems === 'function') { summary = summarizeItems(items); }\n"
        "  else { summary = upstreamSummarizeItems(items); }\n"
        "  return { summary, movements };\n"
        "}\n"
        "module.exports = { buildReport };\n"
    )
    ws = ws_with()
    (ws / DOWNSTREAM).write_text(INJ_IMPL, encoding="utf-8")
    b = audit_injection_bypass(
        ws, downstream="REQ-12", declared_dependencies=["REQ-3"],
        downstream_files=[DOWNSTREAM], produced_files={"REQ-3": [UPSTREAM]})
    check("B① 形参 + typeof 守卫 -> 检出 DEPENDENCY_INJECTION_BYPASS",
          b.findings and b.findings[0].verdict == INJECTION_BYPASS
          and b.findings[0].parameter == "summarizeItems",
          b.findings[0].detail[:78] if b.findings else "")
    btxt = describe_injection_bypass(b)
    check("B② 理由说明「即使 import 了上游，运行时仍可能走注入路径」",
          "运行时仍可能走注入路径" in btxt)
    check("B④ 理由给出**可直接使用的 require 路径**（实测漏了它导致模型写错文件名）",
          b.findings and b.findings[0].require_path == "./summary"
          and "require('./summary')" in btxt,
          f"require_path={b.findings[0].require_path if b.findings else '?'}")
    check("B⑤ 理由含三条硬要求（去旁路 / 用给定路径 / 不改签名与返回结构）",
          "不要**保留" in btxt and "不要自己推断文件名" in btxt and "不得改变" in btxt)
    shutil.rmtree(ws, ignore_errors=True)

    # 直接调用上游、无守卫 -> 不告警
    ws = ws_with()
    (ws / DOWNSTREAM).write_text(
        "const { summarizeItems } = require('./summary');\n"
        "function buildReport(items = []) { return { summary: summarizeItems(items), movements: [] }; }\n"
        "module.exports = { buildReport };\n", encoding="utf-8")
    b = audit_injection_bypass(
        ws, downstream="REQ-12", declared_dependencies=["REQ-3"],
        downstream_files=[DOWNSTREAM], produced_files={"REQ-3": [UPSTREAM]})
    check("B③ 无守卫、直接调用上游 -> 不告警", b.ok and not b.findings)
    shutil.rmtree(ws, ignore_errors=True)

    print()
    print("=" * 78)
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 项失败：")
        for item in failed:
            print(f"  - {item}")
        return 1
    print(f"✅ 全部 {len(RESULTS)} 项断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
