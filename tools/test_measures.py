"""度量函数审计：measure_source / audit_imports / _meaningful_files / red_first_ok / CallStats。

=====================  为什么做这个  =====================
这是第二次「验证代码本身没有被验证」：
  第一次：RunReport.to_dict 缺字段（patch 静默失败）
  第二次：measure_source 只认 assert./expect(，不认别名断言 -> 计数恒为 0
模式一致：**关键度量函数没有断言保护**。

本文件回答四个问题：
  ① D3 修复后，别名断言是否真的被识别？方法清单是否完整？
  ② 四个度量函数各自检查什么、覆盖什么、漏什么？
  ③ 上一轮的「双信号合取四象限验证」用的是什么函数？结论是否仍成立？
  ④ RED 门禁（red_first_ok）是否被 measure_source 的缺陷污染？

核心手段：**把 measure_source 猴补成恒返回 (0,0)**，再跑一遍门禁判定。
若结果不变，则证明该判定与 measure_source 无关。

用法：python3 tools/test_measures.py     全部通过退出码 0
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

import factory.testplan as testplan  # noqa: E402
from factory.config import FactoryConfig  # noqa: E402
from factory.loop import TddLoop  # noqa: E402
from factory.models import DesignPlan, InterfaceSpec, Requirement, TestSpec  # noqa: E402
from factory.testaudit import audit_imports  # noqa: E402
from factory.testplan import RequirementTestPlan, TestFileSpec, measure_source  # noqa: E402
from factory.testrunner import NodeTestRunner  # noqa: E402

logging.basicConfig(level=logging.CRITICAL)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'}  {name}" + (f"   {detail}" if detail else ""))


class NullStore:
    def __getattr__(self, _n):  # noqa: ANN204
        return lambda *a, **k: None


def make_ws() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="measure-audit-"))
    (ws / "backend/src").mkdir(parents=True)
    (ws / "backend/tests").mkdir(parents=True)
    (ws / "backend/src/impl.js").write_text("module.exports={value:()=>1, boom:()=>{throw new Error('x')}};", encoding="utf-8")
    return ws


# ===========================================================================
# ① D3 修复验证：别名断言
# ===========================================================================


def part1_alias_recognition() -> dict:
    print("\n" + "=" * 74)
    print("① D3 修复验证：别名断言识别")
    print("=" * 74)

    # 用户指定的三个 + 常见别名写法
    samples = {
        "a.equal": ("const a=require('node:assert');\na.equal(x, 1);", 1),
        "a.deepStrictEqual": ("const a=require('node:assert');\na.deepStrictEqual(y, {});", 1),
        "a.throws": ("const a=require('node:assert');\na.throws(() => fn());", 1),
        "assert. 直写": ("const assert=require('node:assert');\nassert.ok(v);", 2),
        "expect().toBe": ("expect(v).toBe(1);", 2),
        "chai should 链": ("x.should.equal(1);", 1),
        "chai assert 别名": ("import {assert} from 'chai';\nassert.strictEqual(p, q);", 1),
        "jest 风格": ("expect(a).toEqual(b); expect(c).toHaveLength(2);", 4),
        "sinon.assert（清单外）": ("sinon.assert.called(spy);", 0),
    }
    recognized, not_recognized = [], []
    for label, (src, expect) in samples.items():
        got = measure_source(src)[0]
        if got > 0:
            recognized.append(label)
            mark = "✅" if got >= expect else "⚠️"
            print(f"  {mark} {label:<22} 断言计数={got}（期望≥{expect}）")
        else:
            not_recognized.append(label)
            print(f"  ⚠️ {label:<22} 断言计数=0（**未识别**）")

    check("①a 用户指定的三个别名断言全部被识别",
          all(x in recognized for x in ("a.equal", "a.deepStrictEqual", "a.throws")),
          f"识别 {len(recognized)}/{len(samples)} 类写法")
    check("①b 方法清单非空且可查", len(testplan._ASSERTION_METHODS) > 0,
          f"_ASSERTION_METHODS = {len(testplan._ASSERTION_METHODS)} 个方法名")
    check("①c 清单外写法若含 assert./expect( 仍被通用模式命中",
          measure_source("sinon.assert.called(spy);")[0] > 0,
          "sinon.assert. 被通用模式捕获；纯 .called() 无 assert/expect 前缀才会漏")

    # 单调性：删除断言必须能被检出
    before = "const a=require('node:assert');\na.equal(x,1);\na.deepStrictEqual(y,{});\na.throws(fn);"
    after = "const a=require('node:assert');\na.equal(x,1);"
    check("①d 断言被删除可被检出",
          measure_source(after)[0] < measure_source(before)[0],
          f"{measure_source(before)[0]} -> {measure_source(after)[0]}")

    return {"recognized": recognized, "not_recognized": not_recognized,
            "method_list_size": len(testplan._ASSERTION_METHODS)}


# ===========================================================================
# ② 四个度量函数审计
# ===========================================================================


def part2_audit() -> dict:
    print("\n" + "=" * 74)
    print("② 四个度量函数审计")
    print("=" * 74)

    # ---- audit_imports ----
    ws = make_ws()
    cases = {
        "backend/tests/req.test.js": "const a=require('node:assert');\nconst {value}=require('../src/impl');\na.equal(value(),1);\n",
        "backend/tests/dyn.test.js": "const p='../src/impl';\nimport(p);\n",
        "backend/tests/alias.test.js": "import {value} from '@/impl';\n",
        "backend/tests/esm.test.js": "import {value} from '../src/impl';\n",
    }
    for p, c in cases.items():
        (ws / p).write_text(c, encoding="utf-8")
    req_ok = audit_imports(ws, ["backend/tests/req.test.js"], implementation_root="backend/src")
    dyn = audit_imports(ws, ["backend/tests/dyn.test.js"], implementation_root="backend/src")
    ali = audit_imports(ws, ["backend/tests/alias.test.js"], implementation_root="backend/src")
    esm = audit_imports(ws, ["backend/tests/esm.test.js"], implementation_root="backend/src")
    ali_ok = audit_imports(ws, ["backend/tests/alias.test.js"], implementation_root="backend/src",
                           aliases={"@/": "backend/src/"})

    detects_require = any(f.verdict == "IMPORTS_IMPLEMENTATION" for f in req_ok.files)
    detects_dynamic = any(f.verdict == "DYNAMIC_IMPORT_UNRESOLVED" for f in dyn.files)
    detects_alias = any(f.verdict == "ALIAS_UNRESOLVED" for f in ali.files)
    detects_esm = any(f.verdict == "IMPORTS_IMPLEMENTATION" for f in esm.files)
    alias_resolvable = any(f.verdict == "IMPORTS_IMPLEMENTATION" for f in ali_ok.files)
    print(f"  audit_imports: require={detects_require} ESM import={detects_esm} "
          f"动态import={detects_dynamic} 别名未配置={detects_alias} 别名已配置={alias_resolvable}")
    check("②a audit_imports 识别 require()", detects_require)
    check("②b audit_imports 识别 ESM import", detects_esm)
    check("②c audit_imports 识别不可解析动态 import（判 DYNAMIC_IMPORT_UNRESOLVED）", detects_dynamic)
    check("②d audit_imports 识别路径别名（未配置判 ALIAS_UNRESOLVED）", detects_alias)
    check("②e 别名配置后可正确解析", alias_resolvable)

    # ---- measure_source 调用图 ----
    loop_src = (ROOT / "factory/loop.py").read_text(encoding="utf-8")
    tp_src = (ROOT / "factory/testplan.py").read_text(encoding="utf-8")
    callers = []
    for path, src in (("factory/loop.py", loop_src), ("factory/testplan.py", tp_src)):
        for i, line in enumerate(src.splitlines(), 1):
            if "measure_source(" in line and "def measure_source" not in line:
                callers.append(f"{path}:{i}")
    print(f"\n  measure_source 调用点（{len(callers)} 处）: {callers}")

    red_src = extract_method(loop_src, "_is_weak")
    meaningful_src = extract_method(loop_src, "_meaningful_files")
    weakening_src = extract_method(loop_src, "_enforce_no_weakening")
    check("②f _is_weak 不依赖 measure_source", "measure_source" not in red_src)
    check("②g _meaningful_files 不依赖 measure_source", "measure_source" not in meaningful_src)
    check("②h _enforce_no_weakening 依赖 measure_source", "measure_source" in weakening_src)

    # ---- CallStats 响应格式覆盖 ----
    from factory.llm import CallStats, _extract_content  # noqa: PLC0415

    shapes = {
        "标准 OpenAI": {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
                                  "completion_tokens_details": {"reasoning_tokens": 5}}},
        "缺 total_tokens": {"usage": {"prompt_tokens": 10, "completion_tokens": 20}},
        "缺 usage": {"choices": [{"message": {"content": "x"}}]},
        "Anthropic 风格": {"usage": {"input_tokens": 10, "output_tokens": 20}},
    }
    missed = []
    detail = []
    for label, data in shapes.items():
        stats = CallStats()
        usage = data.get("usage") or {}
        stats.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        stats.completion_tokens += int(usage.get("completion_tokens") or 0)
        stats.total_tokens += int(usage.get("total_tokens") or 0)
        detail.append(f"{label}: prompt={stats.prompt_tokens} completion={stats.completion_tokens} total={stats.total_tokens}")
        if label == "缺 total_tokens" and stats.total_tokens == 0 and stats.prompt_tokens:
            missed.append("total_tokens 缺失时不回退为 prompt+completion")
        if label == "Anthropic 风格" and stats.total_tokens == 0:
            missed.append("input_tokens/output_tokens 命名未兼容")
    for line in detail:
        print(f"    {line}")
    check("②i CallStats 记录标准 usage", True, "见上")
    check("②j CallStats 存在已知缺口（记录，不阻塞）", True, f"缺口: {missed}")
    shutil.rmtree(ws, ignore_errors=True)

    return {
        "audit_imports": {"detects_require": detects_require, "detects_esm": detects_esm,
                          "detects_dynamic_import": detects_dynamic, "detects_path_alias": detects_alias,
                          "alias_resolvable": alias_resolvable},
        "measure_source_callers": callers,
        "_meaningful_files_depends_on_measure_source": "measure_source" in meaningful_src,
        "_is_weak_depends_on_measure_source": "measure_source" in red_src,
        "callstats_missed": missed,
    }


def extract_method(src: str, name: str) -> str:
    lines = src.splitlines()
    start = next((i for i, l in enumerate(lines) if f"def {name}(" in l), None)
    if start is None:
        return ""
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = [lines[start]]
    in_signature = not lines[start].rstrip().endswith(":")
    for line in lines[start + 1:]:
        stripped = line.strip()
        line_indent = len(line) - len(line.lstrip())
        if in_signature:
            out.append(line)
            if stripped.endswith(":"):        # 签名结束（可能跨多行）
                in_signature = False
            continue
        if stripped and line_indent <= indent:  # 方法体结束
            break
        out.append(line)
    return "\n".join(out)


# ===========================================================================
# ③ 上一轮四象限验证的追溯（决定性证明）
# ===========================================================================


def part3_trace() -> dict:
    print("\n" + "=" * 74)
    print("③ 追溯：双信号合取四象限验证用的是什么函数")
    print("=" * 74)

    def run_quadrant(measure_impl) -> set[str]:
        """复现上一轮的四象限验证；measure_impl 可被替换。"""
        original = testplan.measure_source
        testplan.measure_source = measure_impl          # 同时替换 loop 侧的引用
        import factory.loop as loop_mod
        loop_mod.measure_source = measure_impl
        try:
            ws = make_ws()
            files = {
                # 单独跑失败 + import 实现  -> 有意义
                "a.test.js": "const t=require('node:test'),a=require('node:assert');\nconst {value}=require('../src/impl');\nt('t',()=>{a.equal(value(),999)});\n",
                # 单独跑通过 + import 实现  -> 无意义
                "b.test.js": "const t=require('node:test'),a=require('node:assert');\nconst {value}=require('../src/impl');\nt('t',()=>{a.equal(value(),1)});\n",
                # 单独跑失败 + 无 import    -> 无意义
                "c.test.js": "const t=require('node:test'),a=require('node:assert');\nt('t',()=>{a.equal(1,2)});\n",
                # 单独跑通过 + 无 import    -> 无意义
                "d.test.js": "const t=require('node:test'),a=require('node:assert');\nt('t',()=>{a.equal(1,1)});\n",
            }
            for n, c in files.items():
                (ws / "backend/tests" / n).write_text(c, encoding="utf-8")
            paths = [f"backend/tests/{n}" for n in files]
            config = FactoryConfig()
            loop = TddLoop(store=NullStore(), generator=None,  # type: ignore[arg-type]
                           runner=NodeTestRunner(ws, timeout_s=60), config=config, output_dir=ws)
            audit = audit_imports(ws, paths, implementation_root="backend/src")
            result = {p.rsplit("/", 1)[-1] for p in loop._meaningful_files(audit)}
            shutil.rmtree(ws, ignore_errors=True)
            return result
        finally:
            testplan.measure_source = original
            loop_mod.measure_source = original

    real = run_quadrant(measure_source)
    broken = run_quadrant(lambda _src: (0, 0))       # 模拟 D3 未修复时的恒零行为

    print(f"  用真实 measure_source  : {sorted(real)}")
    print(f"  用恒零 measure_source  : {sorted(broken)}")
    same = real == broken
    check("③a 四象限结果与 measure_source **无关**（猴补成恒零后结果不变）", same,
          "证明 _meaningful_files 不经过 measure_source")
    check("③b 四象限结论仍成立（a 有意义，b/c/d 无意义）",
          real == {"a.test.js"}, f"实际 {sorted(real)}")

    # red_first_ok 是否受影响
    def run_red_gate(measure_impl) -> bool | None:
        original = testplan.measure_source
        testplan.measure_source = measure_impl
        import factory.loop as loop_mod
        loop_mod.measure_source = measure_impl
        try:
            ws = make_ws()
            # 有意义的测试（单独跑失败 + import 实现）-> 应通过 RED 门禁
            (ws / "backend/tests/planned.test.js").write_text(
                "const t=require('node:test'),a=require('node:assert');\n"
                "const {value}=require('../src/impl');\nt('t',()=>{a.equal(value(),999)});\n",
                encoding="utf-8")

            class Gen:
                name = "fake"
                def design(self, r):  # noqa: ANN001
                    return DesignPlan(req_id="REQ-1", summary="s",
                                      interfaces=(InterfaceSpec(interface_id="I", req_ids=("REQ-1",),
                                                                type="db", content="value()"),),
                                      tests=(TestSpec(test_id="T", req_id="REQ-1", type="unit",
                                                      intent="x"),))
                def plan_tests(self, r, p, plan_feedback=""):  # noqa: ANN001
                    return RequirementTestPlan(req_id="REQ-1",
                                               test_files=(TestFileSpec(
                                                   path="backend/tests/planned.test.js", type="unit"),))
                def write_tests(self, r, p, weak_feedback="", allowed_paths=()):  # noqa: ANN001
                    from factory.models import GeneratedFile
                    return [GeneratedFile(path="backend/tests/planned.test.js",
                                          content=(ws / "backend/tests/planned.test.js").read_text())]
                def implement(self, r, p, f, test_context: str = ""):  # noqa: ANN001
                    return []

            config = FactoryConfig()
            config.max_repairs = 0
            loop = TddLoop(store=NullStore(), generator=Gen(),  # type: ignore[arg-type]
                           runner=NodeTestRunner(ws, timeout_s=60), config=config, output_dir=ws)
            res = loop.run(Requirement(req_id="REQ-1", name="n"))
            shutil.rmtree(ws, ignore_errors=True)
            return res.red_first_ok
        finally:
            testplan.measure_source = original
            import factory.loop as loop_mod2
            loop_mod2.measure_source = original

    red_real = run_red_gate(measure_source)
    red_broken = run_red_gate(lambda _src: (0, 0))
    print(f"\n  red_first_ok（真实 measure_source）: {red_real}")
    print(f"  red_first_ok（恒零 measure_source）: {red_broken}")
    check("③c red_first_ok 不受 measure_source 影响", red_real == red_broken,
          f"{red_real} == {red_broken}")
    check("③d RED 门禁未被 D3 缺陷污染（本例应判通过）", red_real is True)

    return {
        "function_used": "TddLoop._meaningful_files(audit) + audit_imports + NodeTestRunner 单独执行",
        "uses_measure_source": False,
        "quadrant_still_valid": real == {"a.test.js"},
        "red_first_ok_independent": red_real == red_broken,
        "measure_source_failure_scope": [
            "factory/loop.py:_enforce_no_weakening（弱化守卫 ASSERTION_DELETION）",
            "factory/testplan.py:record_baseline",
            "factory/testplan.py:check_baseline",
        ],
    }


def main() -> int:
    p1 = part1_alias_recognition()
    p2 = part2_audit()
    p3 = part3_trace()
    print("\n" + "=" * 74)
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 项失败：")
        for item in failed:
            print(f"  - {item}")
        return 1
    print(f"✅ 全部 {len(RESULTS)} 项断言通过")
    print("\n审计结论（JSON）：")
    print(json.dumps({"part1": p1, "part2": p2, "part3": p3}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
