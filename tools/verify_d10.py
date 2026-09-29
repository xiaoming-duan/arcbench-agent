"""D10 修复验证（阻塞项）。

=====================  D10 的内容  =====================
最终门禁判定只检查 `dep_audit.ok`（方案1），**漏看 `mock_audit.ok`**（方案2-A）。
后果：mock 违规被记录进报告，需求却仍判 PASSED —— 下一轮实验的「阻断」数据会被污染。

=====================  验证方式（可证伪）  =====================
不只看"现在是 FAILED"，还要证明"修复前会是 PASSED"：

  1. 构造 mock 违规用例（测试 vi.mock 了声明依赖的上游、未在计划里声明）
  2. 跑完整 TddLoop，断言 FAILED + UNVERIFIED_DEPENDENCY
  3. 用**同一次运行的真实审计结果**求值旧判定表达式：
       旧: dep_ok_old = dep_audit.ok or not enforce_dependency_usage
     若 dep_ok_old == True 而 combined_ok == False，
     则证明「漏看 mock_audit」正是唯一让旧代码放行的原因。

退出码 0 = D10 修复有效；非 0 = 修复未生效，E1–E3 不得开跑。
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

from factory.config import FactoryConfig  # noqa: E402
from factory.loop import TddLoop  # noqa: E402
from factory.models import (  # noqa: E402
    DesignPlan,
    GeneratedFile,
    InterfaceSpec,
    Requirement,
    TestSpec,
)
from factory.testplan import RequirementTestPlan, TestFileSpec  # noqa: E402
from factory.testrunner import NodeTestRunner  # noqa: E402

logging.basicConfig(level=logging.CRITICAL)

UPSTREAM_FILE = "backend/src/services/summary.js"
UPSTREAM_SRC = "function summarizeItems(i=[]){return {total:i.length};}\nmodule.exports={summarizeItems};\n"
DOWNSTREAM_FILE = "backend/src/services/report.js"
DOWNSTREAM_TEST = "backend/tests/req12.report.test.js"

# 测试层 mock 了声明依赖的上游、且没在计划里声明
MOCKED_TEST_SRC = (
    "const vi = { mock: (...a) => {} };\n"
    "vi.mock('../src/services/summary', () => ({ summarizeItems: () => ({ total: 99 }) }));\n"
    "const t=require('node:test'),a=require('node:assert');\n"
    "const {buildReport}=require('../src/services/report');\n"
    "t('REQ-12 报表结构完整',()=>{\n"
    "  const r=buildReport([{quantity:5}]);\n"
    "  a.deepStrictEqual(Object.keys(r).sort(),['movements','summary']);\n"
    "});\n"
)
# 实现不调用上游（参数注入），保证测试能通过、从而走到依赖门禁
IMPL_NO_DEP = (
    "function buildReport(items=[],movements=[],summarizeItems){\n"
    "  return {summary: typeof summarizeItems==='function'?summarizeItems(items):{},movements};\n"
    "}\nmodule.exports={buildReport};\n"
)


class NullStore:
    def __getattr__(self, _n):  # noqa: ANN204
        return lambda *a, **k: None


class StaticGenerator:
    name = "static"

    def design(self, requirement):  # noqa: ANN001
        return DesignPlan(
            req_id=requirement.req_id, summary="s",
            interfaces=(InterfaceSpec(interface_id="I", req_ids=(requirement.req_id,),
                                      type="db", content="buildReport()"),),
            tests=(TestSpec(test_id="T", req_id=requirement.req_id, type="unit", intent="x"),),
        )

    def plan_tests(self, requirement, plan, plan_feedback=""):  # noqa: ANN001
        return RequirementTestPlan(
            req_id=requirement.req_id,
            test_files=(TestFileSpec(path=DOWNSTREAM_TEST, type="unit", covers=()),),
            scenarios=(),
        )

    def write_tests(self, requirement, plan, weak_feedback="", allowed_paths=()):  # noqa: ANN001
        return [GeneratedFile(path=DOWNSTREAM_TEST, content=MOCKED_TEST_SRC)]

    def implement(self, requirement, plan, failures, test_context: str = ""):  # noqa: ANN001
        return [GeneratedFile(path=DOWNSTREAM_FILE, content=IMPL_NO_DEP)]


def main() -> int:
    print("=" * 78)
    print("D10 修复验证：最终门禁是否看 mock_audit.ok")
    print("=" * 78)

    ws = Path(tempfile.mkdtemp(prefix="d10-verify-"))
    (ws / "backend/src/services").mkdir(parents=True)
    (ws / "backend/tests").mkdir(parents=True)
    (ws / UPSTREAM_FILE).write_text(UPSTREAM_SRC, encoding="utf-8")

    # 只开 mock 检查、关掉方案1 —— 这正是 D10 暴露时的配置
    config = FactoryConfig.from_env(
        enforce_dependency_usage=False, enforce_mock_check=True,
        warn_injection_bypass=False, block_injection_bypass=False)
    config.max_repairs = 0

    loop = TddLoop(store=NullStore(), generator=StaticGenerator(),  # type: ignore[arg-type]
                   runner=NodeTestRunner(ws, timeout_s=90), config=config, output_dir=ws)
    loop._impl_files["REQ-3"] = [UPSTREAM_FILE]
    result = loop.run(Requirement(req_id="REQ-12", name="报表", dependencies=("REQ-3",)))

    verdicts = [v.get("verdict") for v in result.dependency_violations]
    audits = result.gate_audits
    print(f"  状态            : {result.state}")
    print(f"  违规判定        : {verdicts}")
    print(f"  gate_audits     : {audits}")

    # 旧判定：只看 dep_audit
    dep_audit_ok = not any(v.get("verdict") in
                           {"DEPENDENCY_NOT_USED", "FAKE_DEPENDENCY", "UPSTREAM_MISSING"}
                           for v in result.dependency_violations)
    old_dep_ok = dep_audit_ok or not config.enforce_dependency_usage
    combined_ok = audits.get("combined_ok")

    print()
    print("  ── 旧判定复算（同一份真实审计结果）──")
    print(f"    dep_audit_ok(方案1)          = {dep_audit_ok}")
    print(f"    enforce_dependency_usage     = {config.enforce_dependency_usage}")
    print(f"    旧判定 old_dep_ok            = {old_dep_ok}  -> 修复前会判 "
          f"{'PASSED' if old_dep_ok else 'FAILED'}")
    print(f"    新判定 combined_ok           = {combined_ok}  -> 现在判 "
          f"{'PASSED' if combined_ok else 'FAILED'}")

    checks = [
        ("① 构造了 mock 违规用例（测试 mock 上游且未声明）",
         "UNVERIFIED_DEPENDENCY" in verdicts, str(verdicts)),
        ("② 修复后判定 FAILED", result.state == "FAILED", result.state),
        ("③ 判定码为 UNVERIFIED_DEPENDENCY", "UNVERIFIED_DEPENDENCY" in verdicts, str(verdicts)),
        ("④ 旧判定会判 PASSED（证明漏看 mock 是唯一原因）", old_dep_ok is True, str(old_dep_ok)),
        ("⑤ 新判定 combined_ok 为 False", combined_ok is False, str(combined_ok)),
        ("⑥ mock_ok 标志被记录且为 False", audits.get("mock_ok") is False, str(audits.get("mock_ok"))),
    ]
    print()
    failed = []
    for name, ok, detail in checks:
        print(f"{'✅' if ok else '❌'}  {name}   {detail}")
        if not ok:
            failed.append(name)
    shutil.rmtree(ws, ignore_errors=True)

    print("=" * 78)
    if failed:
        print(f"❌ D10 修复未生效（{len(failed)} 项未过）—— **停，E1–E3 不得开跑**")
        for item in failed:
            print(f"   - {item}")
        return 1
    print(f"✅ 全部 {len(checks)} 项断言通过")
    print("   D10 修复有效：mock 违规现在会真的阻断，且旧判定确实会放行 → E1–E3 可以开跑")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
