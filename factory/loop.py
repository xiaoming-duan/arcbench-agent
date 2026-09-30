"""控制面：单需求 TDD 循环 + RED/GREEN 门禁。

对应架构文档的：
  生产车间  TDDLoop：生成测试 -> 实现 -> 运行 -> 分析失败 -> 修复 -> 直到通过或预算耗尽
  正确面    验证门禁位于 RED 与 GREEN 之间，不可跳过
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Sequence

from .config import FactoryConfig
from .generator import Generator
from .models import (
    DesignPlan,
    GeneratedFile,
    Requirement,
    RequirementResult,
    TestOutcome,
)
from .store import FactoryStore
from .testaudit import (
    DependencyAudit,
    ImportAudit,
    InjectionAudit,
    MockAudit,
    audit_imports,
    audit_injection_bypass,
    audit_mocked_dependencies,
    audit_requirement_dependencies,
    describe,
    describe_dependencies,
    describe_injection_bypass,
    describe_mocked_dependencies,
    is_meaningful_import,
)
from .testplan import (
    RequirementTestPlan,
    TestPlan,
    measure_source,
    record_baseline,
    validate_requirement_plan,
    write_test_plan,
)
from .testrunner import TestRunner
from .workspace import apply_generated_files

logger = logging.getLogger("factory.loop")

# ESM 环境下非法的 CommonJS 惯用法（④ 静态拦截用）
_CJS_REQUIRE = re.compile(r"""require\s*\(\s*['"]vitest['"]\s*\)""")

# 「测试自身坏了」的信号：一旦出现就不再是「缺实现」，而是真的 TEST_BROKEN
_BROKEN_TEST_PATTERNS = (
    re.compile(r"Parse failure|Unexpected token|SyntaxError", re.IGNORECASE),
    re.compile(r"ReferenceError", re.IGNORECASE),
)

# 收集阶段「模块找不到」的两种典型措辞。**路径两侧的引号可有可无**：
#   Rolldown/Node: Cannot find module '../src/x.js' imported from /abs/tests/x.test.js
#                  平台实测该路径**带单引号**：imported from '/workspace/.../x.test.js'
#   Vite:          Failed to resolve import "../src/x.js" from "tests/x.test.js"
#
# 初版写成 `imported from\s*(?P<frm>[^\s'"]+)` —— 要求 from 之后第一个字符不是引号，
# 于是平台上带引号的真实报错**一条都匹配不上**，整个修复静默失效。
# 而单测却全绿：因为夹具是我自己手写的「无引号版」，而不是平台原文。
# 教训：夹具必须来自真实日志，不能来自我对格式的假设。
_MISSING_MODULE_PATTERNS = (
    re.compile(
        r"""Cannot find module\s*['"]?(?P<spec>[^'"]+)['"]?\s*imported from\s*"""
        r"""['"]?(?P<frm>[^'"\s]+)['"]?""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""Failed to resolve import\s*['"]?(?P<spec>[^'"]+)['"]?\s*from\s*"""
        r"""['"]?(?P<frm>[^'"\s]+)['"]?""",
        re.IGNORECASE,
    ),
)


def _backend_relative(path: str, backend_dir: str = "backend") -> str:
    """把 output_dir 相对路径转成 backend/ 相对路径，供测试运行器使用。"""
    prefix = f"{backend_dir}/"
    return path[len(prefix):] if path.startswith(prefix) else path


class TddLoop:
    """把一个需求从设计推进到 PASSED / FAILED。"""

    def __init__(
        self,
        *,
        store: FactoryStore,
        generator: Generator,
        runner: TestRunner,
        config: FactoryConfig,
        output_dir: Path,
    ) -> None:
        self.store = store
        self.generator = generator
        self.runner = runner
        self.config = config
        self.output_dir = output_dir
        # 本次运行已产出的测试计划，最终汇总写入 .arc/test_plan.yaml
        self._plans: dict[str, RequirementTestPlan] = {}
        # 每个需求产出的实现文件（output_dir 相对）—— 下游据此查上游导出清单
        self._impl_files: dict[str, list[str]] = {}

    # ---- 内部工具 ----

    def _record_test_status(self, plan: DesignPlan, passed: bool) -> None:
        for spec in plan.tests:
            try:
                self.store.set_test_status(spec.test_id, passed)
            except Exception as exc:  # 记录失败不应中断主流程
                logger.warning("写入测试状态失败 %s: %s", spec.test_id, exc)

    def _record_interfaces_implemented(self, plan: DesignPlan) -> None:
        for spec in plan.interfaces:
            try:
                self.store.mark_interface_implemented(
                    spec.interface_id, f"{spec.interface_id} implemented"
                )
            except Exception as exc:
                logger.warning("标记接口已实现失败 %s: %s", spec.interface_id, exc)

    def _broken_test_feedback(self, outcome: TestOutcome) -> str:
        """给写测试阶段的、针对「测试跑不起来」的可执行反馈。"""
        excerpt = self._runner_excerpt(outcome, 24)
        return (
            "你的测试文件**无法被收集执行**（0 个测试被发现），必须修好它。\n"
            f"运行器输出:\n{excerpt}\n\n"
            "常见成因与修法：\n"
            "  1. `vi.hoisted(() => ...)` 里引用了顶层 `import` 的绑定 —— "
            "hoisted 回调先于所有 import 执行，必然抛 "
            "`Cannot access ... before initialization`。"
            "改成在回调内部 `require('包名')`，或改用 `vi.importActual`。\n"
            "  2. 语法错误或 import 路径不存在 —— 确保 require/import 的路径真实可解析。\n"
            "  3. 使用了运行器不支持的 API —— 只用当前方言支持的写法。\n"
            "请保证文件能被成功收集（哪怕断言失败也可以），"
            "因为「断言失败」是实现缺失的有效证据，「收集失败」不是。"
        )

    def _weak_test_feedback(
        self,
        outcome: TestOutcome,
        test_paths: Sequence[str],
        audit: ImportAudit | None = None,
        violations: Sequence[dict[str, Any]] = (),
        allowed_paths: Sequence[str] = (),
    ) -> str:
        """把 WEAK_TEST 判定变成可执行的反馈，交给生成器重写测试。

        `weak_feedback_mode` 控制反馈形态，用于 A/B 度量：
          basic        —— 只说"测试在没有实现的情况下就通过了"
          import_aware —— 附加静态 import 诊断，直接指出"没引用实现模块"

        注：A/B 实验（EXPERIMENT.md）已证明该措辞对重写成功率零增量。
        保留两种形态仅为可复现该实验；真正起作用的是审计**参与门禁判定**。
        """
        lines = [
            f"问题：测试文件 {', '.join(test_paths)} 未通过 RED 门禁"
            f"（本轮结果 {outcome.summary()}）。",
            "这意味着这些测试没有真正验证被测行为，属于空转测试。",
        ]
        if audit is not None and not audit.ok:
            lines.append(describe(audit, implementation_root=self.config.implementation_root))
        elif self.config.weak_feedback_mode == "import_aware":
            lines.append(
                describe(
                    audit_imports(
                        self.output_dir,
                        [self._to_output_relative(p) for p in test_paths],
                        implementation_root=self.config.implementation_root,
                        aliases=self.config.import_aliases,
                    ),
                    implementation_root=self.config.implementation_root,
                )
            )
        if violations:
            lines.append(self._rejection_feedback(violations, allowed_paths))
        if outcome.total:
            lines.append(f"本轮共 {outcome.total} 条测试、{outcome.failed} 条失败——必须是全部失败才算 RED。")
        return "\n".join(lines)

    # ---- import 审计（C） ----

    def _to_output_relative(self, path: str) -> str:
        """把 backend/ 相对路径还原成 output_dir 相对路径。"""
        prefix = f"{self.config.backend_dir}/"
        normalized = path.lstrip("./")
        return normalized if normalized.startswith(prefix) else f"{prefix}{normalized}"

    def _declared_types(self, plan: DesignPlan) -> dict[str, tuple[str, ...]]:
        """测试文件 -> 声明的类型集合（用于 e2e 豁免）。"""
        mapping: dict[str, tuple[str, ...]] = {}
        for spec in plan.tests:
            if not spec.file_path:
                continue
            key = spec.file_path.lstrip("./")
            mapping[key] = mapping.get(key, ()) + ((spec.type or "unit"),)
        return mapping

    def _audit(self, test_paths: Sequence[str], plan: DesignPlan) -> ImportAudit:
        return audit_imports(
            self.output_dir,
            [self._to_output_relative(p) for p in test_paths],
            implementation_root=self.config.implementation_root,
            declared_types=self._declared_types(plan),
            aliases=self.config.import_aliases,
        )

    def _is_weak(self, outcome: TestOutcome, audit: ImportAudit) -> bool:
        """逐文件判定，而不是只看整组退出码。

        这是对结构性漏洞的修补：整组失败可能只是模型**新增了一个会失败的文件**，
        而空转文件原封不动（实测 15/15 次都走这条路）。
        """
        if outcome.passed:
            return True
        if not self.config.audit_in_red_gate:
            # 对照臂：只看整组退出码（原行为，存在结构性逃逸路径）
            return False
        return bool(audit.weak_files)

    def _expected_red_reason(self, outcome: TestOutcome) -> str | None:
        """0 个测试是否只是「import 了尚未实现的模块」——是则返回说明，否则 None。

        ★这是一处**架构性矛盾**，不是模型写得不好：

          A（反 WEAK_TEST）：测试必须 import 真实实现，否则断言的是自己造的假数据
          B（RED 门禁）    ：测试必须在实现存在前失败

        在 vitest + 静态 ESM 下，A 会让文件在**收集阶段**就因模块不存在而整体失败，
        于是 B 永远观察不到 `total > 0` 的失败。实测（vitest 4.1.8）：

            Error: Cannot find module '../src/services/summary.js'
                   imported from .../tests/missing.test.js
            Test Files  1 failed (1)
            Tests  no tests            <- total == 0

        而模块存在、只是断言失败时是 `Tests 1 failed (1)`（total == 1）。

        旧判据把 `total == 0` 一律当 TEST_BROKEN，于是模型被要求去修一个
        **它无法修**的问题：删掉 import 就不再引用实现（变成 WEAK_TEST），
        留着 import 就永远收集失败。重写预算被烧光，真凶却不在测试里。

        实测对照：node 方言下「加载失败」被计为 1 个失败测试（total == 1），
        所以从来没暴露这个问题 —— 这也是它只在 vitest 上出现的原因。
        """
        if outcome.passed or outcome.total > 0:
            return None
        merged = f"{outcome.stdout or ''}\n{outcome.stderr or ''}"
        if not merged.strip():
            return None
        # 出现语法 / 运行期错误 -> 确实是测试自身坏了，仍判 TEST_BROKEN
        if any(p.search(merged) for p in _BROKEN_TEST_PATTERNS):
            return None

        impl_root = (self.output_dir / self.config.implementation_root).resolve()
        for pattern in _MISSING_MODULE_PATTERNS:
            for match in pattern.finditer(merged):
                spec = match.group("spec")
                if not spec.startswith((".", "/")):
                    continue  # 裸包名（如 'vitest'）缺失不是「实现尚未生产」
                origin = match.group("frm")
                # 报错里的「from」基准不统一，必须逐个试：
                #   Rolldown/Node 给**绝对路径**（.../out/backend/tests/x.test.js）
                #   Vite 给**相对 vitest root 的路径**（tests/x.test.js，root=backend/）
                # 只按 output_dir 解析会让 Vite 措辞漏判（实测 T32h 就是这么挂的）。
                candidates = []
                base = Path(origin)
                if base.is_absolute():
                    candidates.append(base)
                else:
                    candidates.append(self.output_dir / origin)
                    candidates.append(self.output_dir / self.config.backend_dir / origin)
                for candidate in candidates:
                    target = (candidate.parent / spec).resolve()
                    if target == impl_root or impl_root in target.parents:
                        return (
                            f"收集失败的原因是 import 的实现模块尚不存在（{spec}）——"
                            "这是 RED 阶段的常态而非测试缺陷，按有效 RED 放行"
                        )
        return None

    def _is_uncollectable(self, outcome: TestOutcome) -> bool:
        """测试文件能否被收集执行 —— 区分「测试失败」与「测试根本跑不起来」。

        实测病理（REQ-7）：模型写了非法的 vitest 惯用法 ——
        在 `vi.hoisted(() => { new Database() })` 里引用顶层 import 的 `Database`。
        `vi.hoisted` 先于所有 import 执行，必然抛 ReferenceError，
        整个文件在**收集阶段**就失败（0 个测试被收集）。

        这跟「断言失败」有本质区别：
          断言失败 = 实现缺失的证据（RED 有效，该进实现阶段）
          收集失败 = 测试自己写坏了（实现阶段无论如何都修不好）
        旧行为把两者都当"整组失败"，于是 RED 门禁放行，
        4 轮实现修复全部白费 —— 任何实现都救不了一个坏掉的测试文件。

        ★但「收集失败」本身还要再分两种（实测补充，见 _expected_red_reason）：
          1) 测试自身坏了（语法错误 / 非法惯用法）      -> TEST_BROKEN，回退重写
          2) 测试 import 了**尚未实现**的模块           -> 有效 RED，进实现阶段
        第 2 种在静态 ESM + vitest 下是 RED 阶段的**常态**：
        实现还没生产出来，import 必然解析不到。把它当 TEST_BROKEN
        等于要求模型去修一个它修不了的问题（删掉 import 反而变成 WEAK_TEST）。
        """
        if self._expected_red_reason(outcome) is not None:
            return False
        return not outcome.passed and outcome.total == 0

    def _runner_excerpt(self, outcome: TestOutcome, limit: int = 18) -> str:
        """合并 stdout 与 stderr —— **两者都要**。

        实测（REQ-7）：vitest 把摘要写到 stdout、把失败详情写到 stderr。
        旧写法 `stdout or stderr` 在 stdout 非空时**永远看不到 stderr**，
        于是模型只收到「0 个测试被收集」，从未知道 ReferenceError 在第 5 行。
        这正是「反馈缺少可执行细节」这一类缺陷的又一次复发。
        """
        parts = []
        err = (outcome.stderr or "").strip()
        out = (outcome.stdout or "").strip()
        if err:
            parts.append(err)      # stderr 优先：真正的错误在这里
        if out:
            parts.append(out)      # stdout 作为上下文补充
        merged = "\n".join(parts).strip() or "(无输出)"
        return "\n".join(merged.splitlines()[:limit])

    def _broken_test_diagnostics(
        self, test_paths: Sequence[str], outcome: TestOutcome, head_lines: int = 10
    ) -> str:
        """构造 TEST_BROKEN 的「收集阶段」诊断文本（纯函数，便于断言）。

        只写「0 个测试被发现」等于让下一次继续猜。这里一次给出三样东西：
          1. 退出码与收集到的测试数
          2. stderr 前 500 字符（真正的错误在这里——语法错误 / import 失败 /
             非法的框架惯用法都在这一行里见分晓）
          3. 每个测试文件的**前 10 行源码**（带行号）

        实测价值：`vi.hoisted` 顶层引用这类问题，第 5 行就是案发现场；
        有了行号与源码，无需再让模型自己猜是哪一行。
        """
        blocks: list[str] = []
        for rel in test_paths:
            # test_paths 是 **backend 相对**路径（如 tests/x.test.js，供运行器用），
            # 而落盘位置是 output_dir/backend/tests/x.test.js。两种形式都试，
            # 避免把「文件不存在」写进诊断——那会把最有用的证据变成一句误导。
            candidates = (
                self.output_dir / rel,
                self.output_dir / self.config.backend_dir / rel,
            )
            path = next((c for c in candidates if c.is_file()), candidates[-1])
            try:
                head = path.read_text(encoding="utf-8").splitlines()[:head_lines]
            except OSError as exc:
                head = [f"(读取失败: {exc})"]
            numbered = "\n".join(f"    {i:>2}| {line}" for i, line in enumerate(head, 1))
            blocks.append(f"  --- {rel} 前 {head_lines} 行 ---\n{numbered}")
        body = "\n".join(blocks) if blocks else "  (无测试文件)"
        return (
            f"[TEST_BROKEN 诊断] exit_code={outcome.exit_code}，"
            f"收集到 {outcome.total} 个测试\n"
            f"  stderr（前 500 字符）:\n{(outcome.stderr or '(空)')[:500]}\n{body}"
        )

    def _log_broken_test_diagnostics(
        self, test_paths: Sequence[str], outcome: TestOutcome, head_lines: int = 10
    ) -> None:
        logger.error("%s", self._broken_test_diagnostics(test_paths, outcome, head_lines))

    def _broken_test_reason(self, outcome: TestOutcome) -> str:
        excerpt = self._runner_excerpt(outcome, 18)
        return (
            "测试文件无法被收集/执行（0 个测试被发现）——"
            "多半是语法错误、import 失败，或使用了非法的测试框架惯用法。"
            "这是**测试自身的问题**，改实现无法修复。\n"
            f"运行器输出:\n{excerpt}"
        )

    def _weak_reason(self, outcome: TestOutcome, audit: ImportAudit) -> str:
        """WEAK_TEST 的成因。**必须带 verdict**，否则下次仍然看不出是哪一类。"""
        if outcome.passed:
            return "整组测试在实现前就通过（无 RED 证据）"
        weak = audit.weak_files
        detail = ", ".join(f"{f.path}[{f.verdict}]" for f in weak[:3])
        return f"{len(weak)} 个测试文件未引用实现（{detail}）"

    # ---- 测试计划（A 的前提） ----

    def _make_test_plan(
        self, requirement: Requirement, plan: DesignPlan
    ) -> RequirementTestPlan | None:
        """产出并校验测试计划。计划本身也走门禁：不合理则重试，耗尽即阻断。"""
        scenarios = [s.scenario_id for s in requirement.scenarios]
        feedback = ""
        for attempt in range(1, 3):
            try:
                entry = self.generator.plan_tests(requirement, plan, plan_feedback=feedback)
            except Exception as exc:  # noqa: BLE001
                logger.error("[计划门禁] %s 第 %d 次产出失败: %s", requirement.req_id, attempt, exc)
                feedback = f"上一次产出失败: {exc}"
                continue
            violations = validate_requirement_plan(
                entry,
                requirement_scenarios=scenarios,
                requirement_dependencies=list(requirement.dependencies),
                backend_dir=self.config.backend_dir,
            )
            if not violations:
                return entry
            logger.warning(
                "[计划门禁] %s 第 %d 次校验未通过: %s",
                requirement.req_id,
                attempt,
                " | ".join(violations[:4]),
            )
            feedback = "上一次计划存在以下问题，请修正后重新产出：\n" + "\n".join(
                f"- {v}" for v in violations
            )
        return None

    def _persist_test_plan(self, requirement: Requirement, entry: RequirementTestPlan) -> None:
        """把计划落盘（可审计、可复现），并写入 .arc/ 供平台查看。"""
        self._plans[requirement.req_id] = entry
        aggregate = TestPlan(entries=dict(self._plans))
        write_test_plan(self.output_dir / ".arc" / "test_plan.yaml", aggregate)

    def _run_paths(
        self, allowed_paths: Sequence[str], written: Sequence[GeneratedFile]
    ) -> list[str]:
        """本轮要运行的测试路径。

        白名单开启时**只跑计划内路径**；关闭时还原原行为（计划路径 + 所有写出的测试文件），
        这正是"新增一个失败文件即可绕过门禁"的来源。
        """
        paths = [_backend_relative(p, self.config.backend_dir) for p in allowed_paths]
        if not self.config.enforce_test_whitelist:
            for item in written:
                normalized = item.path.lstrip("./")
                if normalized.endswith((".test.js", ".test.ts", ".test.jsx", ".test.tsx",
                                        ".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx")):
                    paths.append(_backend_relative(normalized, self.config.backend_dir))
        seen: set[str] = set()
        unique: list[str] = []
        for item in paths:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    def _collect_test_context(self, allowed_paths: Sequence[str]) -> str:
        """读出本需求测试文件的内容，供实现阶段作为权威契约。"""
        chunks: list[str] = []
        budget = 12000
        for rel in allowed_paths:
            path = self.output_dir / rel
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if len(text) > budget:
                text = text[:budget] + "\n// ...（已截断）"
            chunks.append(f"// ---- {rel} ----\n{text}")
            budget -= len(text)
            if budget <= 0:
                break
        return "\n\n".join(chunks)

    # ---- B：依赖使用审计 ----

    def _record_impl_files(self, requirement: Requirement, files: list[GeneratedFile]) -> None:
        """记录本需求产出的**实现**文件（排除测试），供下游做依赖使用审计。"""
        impl_root = self.config.implementation_root.strip("/") + "/"
        self._impl_files[requirement.req_id] = [
            item.path.lstrip("./")
            for item in files
            if item.path.lstrip("./").startswith(impl_root)
        ]

    def _audit_dependencies(self, requirement: Requirement) -> DependencyAudit:
        """检查本需求是否**真实调用**了它声明的每一个上游。"""
        if not self.config.enforce_dependency_usage or not requirement.dependencies:
            return DependencyAudit()
        entry = self._plans.get(requirement.req_id)
        indirect = entry.indirect_map() if entry else {}
        return audit_requirement_dependencies(
            self.output_dir,
            downstream=requirement.req_id,
            declared_dependencies=requirement.dependencies,
            downstream_files=self._impl_files.get(requirement.req_id, []),
            produced_files=self._impl_files,
            indirect_dependencies=indirect,
        )

    def _audit_mocks(self, requirement: Requirement, allowed_paths: Sequence[str]) -> MockAudit:
        """方案 2-A：测试文件是否 mock 了未声明的上游。"""
        if not self.config.enforce_mock_check or not requirement.dependencies:
            return MockAudit()
        entry = self._plans.get(requirement.req_id)
        return audit_mocked_dependencies(
            self.output_dir,
            downstream=requirement.req_id,
            declared_dependencies=requirement.dependencies,
            test_files=list(allowed_paths),
            produced_files=self._impl_files,
            declared_mocks=entry.mocked_map() if entry else {},
        )

    def _audit_bypass(self, requirement: Requirement) -> InjectionAudit:
        """方案 2-B：实现层形参守卫式注入旁路（**警告级，不阻断**）。"""
        if not self.config.warn_injection_bypass or not requirement.dependencies:
            return InjectionAudit()
        return audit_injection_bypass(
            self.output_dir,
            downstream=requirement.req_id,
            declared_dependencies=requirement.dependencies,
            downstream_files=self._impl_files.get(requirement.req_id, []),
            produced_files=self._impl_files,
        )

    # ---- A：路径白名单 ----

    def _invalid_test_syntax(self, files: Sequence[GeneratedFile]) -> list[dict[str, Any]]:
        """④ 静态拦截非法测试写法 —— 在**跑 vitest 之前**。

        实测：模型在截断压力下会选择更短的 CommonJS 写法 `require('vitest')`，
        而 vitest 是纯 ESM 包，必然抛
        「Vitest cannot be imported in a CommonJS module using require()」。

        这跟语法错误一样属于「测试自身的问题」，但**不必跑一次 vitest 才知道**：
        静态扫一眼就能拦。实测每次收集调用要几十秒，静态拦截直接省掉一整轮
        TEST_BROKEN 往返，并让反馈在模型还「记得上下文」时立刻给出。

        只在 vitest 方言下生效——node 方言里 `require('node:test')` 是正确写法。
        """
        if getattr(self.runner, "dialect", "") != "vitest":
            return []
        violations: list[dict[str, Any]] = []
        for item in files:
            content = item.content or ""
            if _CJS_REQUIRE.search(content):
                violations.append(
                    {
                        "code": "INVALID_TEST_SYNTAX",
                        "path": item.path.lstrip("./"),
                        "detail": (
                            "vitest 是纯 ESM 包，禁止 require('vitest')；"
                            "改用 `import { describe, it, expect } from 'vitest';`。"
                            "引用实现同样用 import ... from '../src/...'，禁止 module.exports。"
                        ),
                    }
                )
        return violations

    def _enforce_test_whitelist(
        self,
        requirement: Requirement,
        files: list[GeneratedFile],
        allowed_paths: Sequence[str],
    ) -> tuple[list[GeneratedFile], list[dict[str, Any]]]:
        """只允许写计划内声明的测试路径；计划外的测试文件一律拒绝。

        被拒的记录为 UNAUTHORIZED_TEST_FILE。这是对"新增一个会失败的测试文件
        来绕过 RED 门禁"这条逃逸路径的封堵。

        ★再区分一类：**测试主题漂移**（TEST_FILE_DRIFT）。
        白名单堵的是「新增文件」；这里堵「把计划的文件整个换成另一个名字」。
        判据：本次提交的计划外测试文件存在，且**没有任何一个计划内路径被提交**
        ——说明计划的文件被整体替换了，而不是多写了一个。
        实测病理：计划的 req1-1-1.register.test.js 被换成 workbook.view.test.js，
        需求讲「注册」，测试却在测「工作簿视图」，此时后续所有断言检查都失去意义，
        必须给出可分辨的 verdict，而不是混在"计划外文件"里。
        """
        if not self.config.enforce_test_whitelist:
            # 对照臂：还原"无白名单"的原始行为
            return list(files), []
        test_prefix = f"{self.config.backend_test_dir.strip('/')}/"
        allowed = set(allowed_paths)
        accepted: list[GeneratedFile] = []
        out_of_plan: list[str] = []
        matched_planned = False
        for item in files:
            normalized = item.path.lstrip("./")
            if not normalized.startswith(test_prefix):
                accepted.append(item)
                continue
            if normalized in allowed:
                matched_planned = True
                accepted.append(item)
                continue
            out_of_plan.append(normalized)

        violations: list[dict[str, Any]] = []
        drift = bool(allowed) and not matched_planned and bool(out_of_plan)
        for path in out_of_plan:
            if drift:
                violations.append(
                    {
                        "code": "TEST_FILE_DRIFT",
                        "path": path,
                        "detail": (
                            f"计划内测试文件 {', '.join(sorted(allowed))} 一个都没提交，"
                            f"却提交了完全不同的文件名 {path}——测试主题已漂移"
                        ),
                    }
                )
                logger.warning(
                    "[白名单] %s 拒绝测试文件漂移: %s（计划内: %s）",
                    requirement.req_id,
                    path,
                    ", ".join(sorted(allowed)),
                )
            else:
                violations.append({"code": "UNAUTHORIZED_TEST_FILE", "path": path})
                logger.warning(
                    "[白名单] %s 拒绝计划外测试文件: %s（白名单: %s）",
                    requirement.req_id,
                    path,
                    ", ".join(sorted(allowed)) or "(空)",
                )
        return accepted, violations

    def _rejection_feedback(
        self, violations: Sequence[dict[str, Any]], allowed_paths: Sequence[str]
    ) -> str:
        """把"被白名单/基线守卫拒绝"翻译成可执行的下一步。

        初次写测试与重写两条路径共用此文案——实测两者都会踩同一个坑：
        只拒绝、不回传理由，模型就会原地重试同一种错法。
        """
        lines = ["你上一轮的改动被白名单/基线守卫**拒绝**了："]
        for item in violations:
            lines.append(
                f"  - [{item.get('code')}] {item.get('path')} {item.get('detail', '')}"
            )
        if allowed_paths:
            lines.append(
                "只允许写以下**计划内**文件，不要新增、重命名或拆分测试文件：\n  "
                + "\n  ".join(allowed_paths)
            )
        lines.append("请在计划内文件里写测试（必须 import 实现模块，并给出真实断言）。")
        return "\n".join(lines)

    def _meaningful_files(self, audit: ImportAudit) -> set[str]:
        """判定"有意义的测试文件" —— **复用 C 的审计产出，不引入新检测器**。

        标准是**两个信号的组合**：
          有意义   = 单独跑时失败（RED） + import 了真实实现
          无意义   = 单独跑通过         或  没有 import 实现

        `audit_imports()` 已经产出 import 信号；"单独跑失败"由逐文件单独执行补齐。
        两个都为真才认为该文件的断言值得保护。
        """
        meaningful: set[str] = set()
        if not self.config.enforce_test_whitelist and not self.config.audit_in_red_gate:
            return meaningful  # 对照臂：还原"无守卫"行为
        for item in audit.files:
            # ★ 与门禁共用同一判据（is_meaningful_import）。此前这里写死
            #   `item.verdict != V_IMPORTS`，而门禁走 WEAK_VERDICTS 成员判断——
            #   两者今天是互补的，但新增 verdict 时会静默分叉（一个放行一个阻断）。
            if not is_meaningful_import(item.verdict):
                continue
            rel = _backend_relative(item.path, self.config.backend_dir)
            if not self.runner.run([rel]).passed:
                meaningful.add(item.path)
        return meaningful

    def _enforce_no_weakening(
        self,
        requirement: Requirement,
        files: list[GeneratedFile],
        meaningful: set[str],
        result: RequirementResult,
        *,
        is_rewrite_phase: bool = False,
        broken_test: bool = False,
    ) -> list[GeneratedFile]:
        """拦住"把有意义的测试改弱"。

        只保护 `_meaningful_files()` 认定的文件（单独跑失败 + import 实现）：
          - 有意义的文件 -> 只允许追加/修改，不允许删除已有断言或用例
          - 无意义的文件 -> 允许整体重写（它的断言本就毫无价值，删掉正是所需）

        ★TEST_BROKEN 优先于弱化检查（`broken_test=True`）：
        文件已被判定「收集阶段就崩、0 个测试被发现」时，它的断言根本不会被执行，
        比较断言数**没有意义**——先修语法，再谈断言覆盖。
        实测病理：某文件 IMPORTS_IMPLEMENTATION 且单独跑失败，故被认定为「有意义」，
        但 vitest 因语法错误收集到 0 个测试；此时它 28 -> 14 的断言缩减被
        `ASSERTION_DELETION` 拒绝，把正当的「删掉坏断言以修好文件」误伤成弱化，
        重写预算全部耗在同一个坏文件上。

        注意放宽的**边界**：只对 TEST_BROKEN 放宽，不对 WEAK_TEST 放宽。
        WEAK_TEST 的文件断言是真的在跑，删断言正是这条守卫要堵的逃逸路径。
        （另：非「有意义」的文件本就走不到比较逻辑，所以 WEAK_TEST 文件
        本来也不受这里限制。）
        """
        kept: list[GeneratedFile] = []
        for item in files:
            normalized = item.path.lstrip("./")
            if item.mode != "write" or normalized not in meaningful:
                kept.append(item)
                continue
            target = self.output_dir / normalized
            if not target.is_file():
                kept.append(item)
                continue
            old_assertions, old_cases = measure_source(target.read_text(encoding="utf-8"))
            new_assertions, new_cases = measure_source(item.content)
            problem = None
            if new_cases < old_cases:
                problem = f"用例数减少 {old_cases} -> {new_cases}"
            elif new_assertions < old_assertions:
                problem = f"断言数减少 {old_assertions} -> {new_assertions}"
            if problem and broken_test:
                # 记录但不阻断：这是重写一个跑不起来的文件，缩减是合理代价
                record = {
                    "code": "ASSERTION_REDUCTION_ON_REWRITE",
                    "path": normalized,
                    "detail": f"{problem}（原文件 TEST_BROKEN，收集阶段即失败，"
                              "断言数不具可比性；放行并记录）",
                }
                result.weakening_violations.append(record)
                logger.warning(
                    "[基线] %s 重写放宽（TEST_BROKEN 优先）: %s %s",
                    requirement.req_id, normalized, problem,
                )
                kept.append(item)
                continue
            if problem:
                record = {"code": "ASSERTION_DELETION", "path": normalized, "detail": problem}
                result.weakening_violations.append(record)
                logger.warning("[基线] %s 拒绝弱化改动: %s %s", requirement.req_id, normalized, problem)
                continue
            kept.append(item)
        return kept

    def _guard_implementation_files(
        self,
        requirement: Requirement,
        files: list[GeneratedFile],
        result: RequirementResult | None = None,
    ) -> list[GeneratedFile]:
        """重写阶段的**修改边界**：默认只允许改实现（src/），测试文件不可修改。

        为什么需要这条边界：
          重写阶段若允许同时改实现和测试，回归检查的语义就会含糊——
          「测试从 PASSED 变 FAIL」既可能是实现回归，也可能是测试被改坏。
          边界收紧后，该信号**一定是实现回归**。

        合法出口：测试自身有 bug 必须改时，在测试计划里为该路径声明
          `test_rewrite_reasons`（含理由）-> 放行并记录；未声明则拦截并记录。

        实测病理（护栏存在的原始原因）：修复轮次里模型倾向于**新建另一个测试文件**
        而不是修实现，运行器只跑计划内测试，真正的失败就被绕过了。
        """
        test_prefix = f"{self.config.backend_test_dir.strip('/')}/"
        declared = self._plans.get(requirement.req_id)

        if not self.config.enforce_impl_only_rewrite:
            # 对照臂：允许改测试（边界关闭），仅记录以便对比
            if result is not None:
                for item in files:
                    normalized = item.path.lstrip("./")
                    if normalized.startswith(test_prefix):
                        result.test_rewrite_reasons.append(
                            {"path": normalized, "reason": "边界关闭（对照臂），放行"}
                        )
            return list(files)

        kept: list[GeneratedFile] = []
        blocked: list[str] = []
        for item in files:
            normalized = item.path.lstrip("./")
            if not normalized.startswith(test_prefix):
                kept.append(item)
                continue
            reason = declared.test_rewrite_reason(normalized) if declared else ""
            if reason:
                if result is not None:
                    result.test_rewrite_reasons.append(
                        {"path": normalized, "reason": reason, "channel": "declared"}
                    )
                logger.info("[边界] %s 允许按声明重写测试文件: %s", requirement.req_id, normalized)
                kept.append(item)
                continue
            blocked.append(normalized)
        if blocked:
            if result is not None:
                result.blocked_test_writes.extend(
                    {"path": path, "detail": "实现阶段默认不得改测试；如需修改请在测试计划声明 test_rewrite_reasons"}
                    for path in blocked
                )
            logger.warning(
                "[边界] %s 重写阶段试图写入测试文件，已拦截 %d 个: %s",
                requirement.req_id,
                len(blocked),
                ", ".join(blocked[:5]),
            )
        return kept

    # ---- 主流程 ----

    def run(self, requirement: Requirement) -> RequirementResult:
        req_id = requirement.req_id
        logger.info("=" * 72)
        logger.info("需求 %s: %s", req_id, requirement.name)
        logger.info("=" * 72)

        result = RequirementResult(req_id=req_id, state="PENDING")

        # ---- 阶段 1: 设计 ----
        self.store.design_started(req_id, f"{req_id} 设计开始")
        try:
            plan = self.generator.design(requirement)
        except Exception as exc:
            self.store.design_failed(req_id, f"{req_id} 设计失败: {exc}")
            result.state = "FAILED"
            result.note = f"设计失败: {exc}"
            logger.error(result.note)
            return result

        # 设计产物写入追溯矩阵（架构文档：数据面的统一对象模型）
        self.store.record_interfaces(plan.interfaces)
        # 节点契约与接口同源，在同一处落盘，避免两处漂移
        self.store.record_node_contract(req_id, plan.interfaces)
        self.store.record_tests(plan.tests)
        self.store.design_done(req_id, f"{req_id} 设计完成: {plan.summary}")
        self.store.commit(f"{req_id} (design): {requirement.name}")
        result.state = "DESIGNED"

        # ---- 阶段 2: 产出测试计划（A 的白名单来源） ----
        test_plan_entry = self._make_test_plan(requirement, plan)
        if test_plan_entry is None:
            result.state = "FAILED"
            result.note = "测试计划门禁未通过（计划不合理），未进入测试生成"
            self.store.design_failed(req_id, f"{req_id} {result.note}")
            logger.error("[计划门禁] %s BLOCK -> FAILED", req_id)
            return result
        allowed_paths = list(test_plan_entry.paths)
        self._persist_test_plan(requirement, test_plan_entry)
        logger.info(
            "[计划] %s 白名单 %d 个文件: %s",
            req_id,
            len(allowed_paths),
            ", ".join(allowed_paths),
        )

        # ---- 阶段 3: 写测试（受白名单约束；被拒或缺失则**带反馈重试**） ----
        #
        # 实测缺陷（3 根需求运行时暴露）：模型把测试写到了计划外的路径，
        # 白名单正确拒绝，但当时直接判"计划内测试文件未生成"就结束了——
        # 拒绝理由没有回传，模型没有机会改正。
        # 与重写循环同一个教训：只拦不说等于没修。
        test_files: list[GeneratedFile] = []
        missing: list[str] = list(allowed_paths)
        write_attempt = 0
        # 违规项以**字典**形式累积（含 code/path/detail），回传给生成器；
        # result.unauthorized_files 只存路径，两者不可混用。
        write_violations: list[dict[str, Any]] = []
        max_write_attempts = self.config.max_test_rewrites + 1
        for attempt in range(1, max_write_attempts + 1):
            try:
                test_files = self.generator.write_tests(
                    requirement,
                    plan,
                    weak_feedback=(
                        self._rejection_feedback(write_violations, allowed_paths)
                        if attempt > 1 and write_violations
                        else ""
                    ),
                    allowed_paths=allowed_paths,
                )
            except Exception as exc:
                self.store.design_failed(req_id, f"{req_id} 测试生成失败: {exc}")
                result.state = "FAILED"
                result.note = f"测试生成失败: {exc}"
                logger.error(result.note)
                return result

            test_files, violations = self._enforce_test_whitelist(
                requirement, test_files, allowed_paths
            )
            result.unauthorized_files.extend(v["path"] for v in violations)
            write_violations.extend(violations)

            # ④ 跑 vitest 之前的静态拦截：非法 CommonJS require('vitest')。
            # 命中则**不落盘**，让 missing 检查触发带反馈的重试——
            # 比跑一次收集、判 TEST_BROKEN、再回退省一整轮。
            syntax_violations = self._invalid_test_syntax(test_files)
            if syntax_violations:
                bad = {v["path"] for v in syntax_violations}
                write_violations.extend(syntax_violations)
                for v in syntax_violations:
                    logger.warning(
                        "[静态检查] %s 拒绝非法测试写法: %s —— %s",
                        req_id,
                        v["path"],
                        v["detail"],
                    )
                test_files = [f for f in test_files if f.path.lstrip("./") not in bad]

            apply_generated_files(self.output_dir, test_files)

            missing = [p for p in allowed_paths if not (self.output_dir / p).is_file()]
            write_attempt = attempt
            if not missing:
                break
            logger.warning(
                "[白名单] %s 第 %d/%d 次：计划内文件仍缺失 %s%s",
                req_id,
                attempt,
                max_write_attempts,
                missing,
                f"（另有 {len(violations)} 个计划外文件被拒）" if violations else "",
            )

        if missing:
            result.state = "FAILED"
            result.note = (
                f"计划内测试文件未生成（{max_write_attempts} 次尝试后仍缺失）: {', '.join(missing)}"
            )
            self.store.test_failed(req_id, result.note)
            logger.error(result.note)
            return result

        test_paths = self._run_paths(allowed_paths, test_files)

        # 记录基线（用于监控"只改断言""删断言""拆文件"等新逃逸路径）
        baselines = {
            p: b for p in allowed_paths if (b := record_baseline(self.output_dir, p)) is not None
        }

        # ---- 阶段 4: RED 门禁（逐文件判定 + 白名单 + 基线守卫） ----
        red = self.runner.run(test_paths)
        audit = self._audit(test_paths, plan)
        rewrites = 0
        last_violations: list[dict[str, Any]] = []
        broken_test = False
        # 0 个测试但成因是「实现尚未生产」-> 有效 RED，放行。
        # 必须显式记一条：否则日志里只看到 0 个测试，读的人会以为测试坏了。
        expected_red = self._expected_red_reason(red)
        if expected_red:
            logger.info("[门禁] %s %s", req_id, expected_red)
        while (self._is_weak(red, audit) or self._is_uncollectable(red)) and self.config.require_red_first:
            if rewrites >= self.config.max_test_rewrites:
                break
            rewrites += 1
            broken_test = self._is_uncollectable(red)
            logger.warning(
                "[门禁] %s %s（%s），回退到写测试阶段重写（%d/%d）",
                req_id,
                "TEST_BROKEN" if broken_test else "WEAK_TEST",
                (self._broken_test_reason(red)[:80] if broken_test
                 else self._weak_reason(red, audit)),
                rewrites,
                self.config.max_test_rewrites,
            )
            logger.info(
                "[审计] %s 逐文件判定: %s",
                req_id,
                "; ".join(f"{f.path}={f.verdict}" for f in audit.files) or "(无文件)",
            )
            if broken_test:
                # ④ 收集阶段诊断：只报「0 个测试被发现」等于让下一次继续猜。
                self._log_broken_test_diagnostics(test_paths, red)
            # 配对回退：状态退回设计/测试阶段
            self.store.set_state(req_id, "DESIGNING", "design")
            try:
                rewritten = self.generator.write_tests(
                    requirement,
                    plan,
                    weak_feedback=(
                        self._broken_test_feedback(red)
                        if self._is_uncollectable(red)
                        else self._weak_test_feedback(
                            red, test_paths, audit, last_violations, allowed_paths
                        )
                    ),
                    allowed_paths=allowed_paths,
                )
            except Exception as exc:
                logger.error("[门禁] %s 测试重写失败: %s", req_id, exc)
                break

            rewritten, violations = self._enforce_test_whitelist(
                requirement, rewritten, allowed_paths
            )
            result.unauthorized_files.extend(v["path"] for v in violations)
            meaningful = self._meaningful_files(audit)
            logger.info(
                "[守卫] %s 有意义的文件 %d/%d 个（单独跑失败 + import 实现）: %s",
                req_id,
                len(meaningful),
                len(audit.files),
                ", ".join(sorted(meaningful)) or "(无)",
            )
            before = len(result.weakening_violations)
            rewritten = self._enforce_no_weakening(
                requirement,
                rewritten,
                meaningful,
                result,
                is_rewrite_phase=True,
                broken_test=broken_test,
            )
            last_violations = violations + result.weakening_violations[before:]
            apply_generated_files(self.output_dir, rewritten)
            test_paths = self._run_paths(allowed_paths, rewritten)

            red = self.runner.run(test_paths)
            audit = self._audit(test_paths, plan)

        weak = self._is_weak(red, audit) or self._is_uncollectable(red)
        broken_test = self._is_uncollectable(red)
        result.red_first_ok = not weak
        result.broken_test = broken_test
        if weak:
            # 阻断：不进入实现阶段
            reason = self._broken_test_reason(red) if broken_test else self._weak_reason(red, audit)
            code = "TEST_BROKEN" if broken_test else "WEAK_TEST"
            note = f"{code}（{reason}）：重写 {rewrites} 次仍无效，未进入实现"
            logger.error("[门禁] %s BLOCK -> FAILED（%s）", req_id, code)
            self.store.test_failed(req_id, f"{req_id} {note}")
            self.store.set_state(req_id, "FAILED", "test")
            self.store.commit(f"{req_id} (blocked): {requirement.name} 空转测试")
            result.state = "FAILED"
            result.note = note
            result.attempts = rewrites
            result.test_rewrites = rewrites
            result.write_attempts = write_attempt if "write_attempt" in dir() else 0
            result.test_plan_files = allowed_paths
            return result
        logger.info("[门禁] %s RED 确认通过（逐文件审计 + 白名单 + 整组失败）", req_id)
        if rewrites:
            result.note = f"WEAK_TEST 经 {rewrites} 次重写后修复"
        result.test_rewrites = rewrites
        result.write_attempts = write_attempt if "write_attempt" in dir() else 0
        result.test_plan_files = allowed_paths
        result.test_plan_baselines = {p: b.to_dict() for p, b in baselines.items()}

        # ---- 阶段 4: 实现 + 修复循环 ----
        self.store.implement_started(req_id, f"{req_id} 实现开始")
        failures: list[str] = list(red.failures)
        outcome: TestOutcome = red
        attempts = 0

        # 把测试源码作为权威契约交给实现阶段
        test_context = self._collect_test_context(allowed_paths)
        dep_audit = DependencyAudit()
        mock_audit = MockAudit()
        bypass_audit = InjectionAudit()
        # 最近一次「测试通过」的实现，用于重写后回归回退
        last_good_impl: list[GeneratedFile] = []
        last_good_passed = False
        while attempts <= self.config.max_repairs:
            attempts += 1
            try:
                impl_files = self.generator.implement(
                    requirement, plan, failures, test_context=test_context
                )
            except Exception as exc:
                self.store.implement_failed(req_id, f"{req_id} 实现失败: {exc}")
                result.state = "FAILED"
                result.note = f"实现失败: {exc}"
                logger.error(result.note)
                result.attempts = attempts
                return result

            impl_files = self._guard_implementation_files(requirement, impl_files, result)
            apply_generated_files(self.output_dir, impl_files)
            self._record_impl_files(requirement, impl_files)

            outcome = self.runner.run(test_paths)
            if outcome.passed:
                last_good_impl = list(impl_files)
                last_good_passed = True
                # 测试通过还不够，三件事必须同时成立：
                #   方案1  声明了依赖就必须真实调用上游（模块层空转）
                #   方案2-A 测试不得 mock 未声明的上游（测试层空转）
                #   方案2-B 实现不得用形参守卫绕过上游（仅警告）
                dep_audit = self._audit_dependencies(requirement)
                mock_audit = self._audit_mocks(requirement, allowed_paths)
                bypass_audit = self._audit_bypass(requirement)
                if bypass_audit.findings:
                    result.dependency_injection_warnings = [
                        f.to_dict() for f in bypass_audit.findings
                    ]
                    logger.warning(
                        "[注入旁路·警告] %s %s",
                        req_id,
                        "; ".join(f"{f.upstream}:{f.parameter}" for f in bypass_audit.findings),
                    )
                reasons = []
                if not dep_audit.ok:
                    reasons.append(describe_dependencies(dep_audit))
                if not mock_audit.ok:
                    reasons.append(describe_mocked_dependencies(mock_audit))
                if bypass_audit.findings and self.config.block_injection_bypass:
                    reasons.append(describe_injection_bypass(bypass_audit))
                if not reasons:
                    break
                failures = reasons
                logger.warning(
                    "[依赖门禁] %s 测试通过，但依赖未被真实验证: %s",
                    req_id,
                    "; ".join(
                        f"{u.upstream}={u.verdict}" for u in dep_audit.violations
                    ) + (";" if dep_audit.violations and mock_audit.violations else "")
                    + "; ".join(f"{u.upstream}=UNVERIFIED_DEPENDENCY" for u in mock_audit.violations),
                )
                if attempts > self.config.max_repairs:
                    break
                continue

            # 重写后回归检查：曾通过、重写后失败 -> 回退到重写前版本
            if last_good_passed and last_good_impl:
                apply_generated_files(self.output_dir, last_good_impl)
                self._record_impl_files(requirement, last_good_impl)
                reverted = self.runner.run(test_paths)
                record = {
                    "code": "IMPLEMENTATION_REGRESSION",
                    "attempt": attempts,
                    "before": "PASSED",
                    "after": outcome.summary(),
                    "reverted_to": "上一轮通过版本",
                    "reverted_result": reverted.summary(),
                }
                result.regressions.append(record)
                logger.error(
                    "[回归] %s 重写把测试从 PASSED 改坏为 %s，已回退到重写前版本（回退后 %s）",
                    req_id, outcome.summary(), reverted.summary(),
                )
                outcome = reverted
                # 关键：回退后**继续**，而不是 break。
                # 早前这里用 break 会一口吃掉剩余的全部修复预算——
                # 实测 E2 给了 5 次预算却只用了 1 次重写，预算变量完全失效。
                # 同时把「上次重写把测试改坏了」这一信息回传给模型，
                # 否则它会重复同一种改法。
                if attempts > self.config.max_repairs:
                    break
                failures = [
                    "上一次重写把测试从通过改成了失败，已回退。"
                    f"失败的测试输出：\n{outcome.stderr[-1200:] or outcome.summary()}\n"
                    "请在不破坏现有测试契约的前提下修改实现："
                    "保持 buildReport 的返回结构（summary / movements 两个键）与调用签名不变，"
                    "只把「优先使用注入回调」的旁路去掉，改为直接调用上游导出。"
                ] + (
                    [describe_injection_bypass(bypass_audit)]
                    if bypass_audit.findings and self.config.block_injection_bypass else []
                )
                continue

            failures = list(outcome.failures) or [outcome.stderr[-2000:] or "测试失败（无结构化输出）"]
            if attempts <= self.config.max_repairs:
                logger.warning(
                    "[修复] %s 第 %d 次修复（剩余 %d 次）",
                    req_id,
                    attempts,
                    self.config.max_repairs - attempts + 1,
                )

        result.attempts = attempts
        result.dependency_violations = [u.to_dict() for u in dep_audit.violations]
        result.dependency_violations += [u.to_dict() for u in mock_audit.violations]
        result.dependency_uncertain = [u.to_dict() for u in dep_audit.uncertain]
        self._record_test_status(plan, outcome.passed)

        # ---- 阶段 5: 门禁判定 ----
        # 三段判定必须都看：漏看 mock_audit 会让「记了违规却仍 PASSED」
        # ——实测 bug：A 组对 mock_api 记录了 UNVERIFIED_DEPENDENCY 却判 PASSED。
        dep_ok_raw = dep_audit.ok or not self.config.enforce_dependency_usage
        mock_ok_raw = mock_audit.ok or not self.config.enforce_mock_check
        bypass_ok_raw = (
            bypass_audit.ok
            or not self.config.warn_injection_bypass
            or not self.config.block_injection_bypass   # 默认警告级，不阻断
        )
        dep_ok, mock_ok, bypass_ok = dep_ok_raw, mock_ok_raw, bypass_ok_raw
        dep_ok = dep_ok and mock_ok and bypass_ok
        result.gate_audits = {
            "dep_ok": dep_ok_raw, "mock_ok": mock_ok_raw, "bypass_ok": bypass_ok_raw,
            "combined_ok": dep_ok,
            "enforce_dependency_usage": self.config.enforce_dependency_usage,
            "enforce_mock_check": self.config.enforce_mock_check,
            "block_injection_bypass": self.config.block_injection_bypass,
        }
        if outcome.passed and dep_ok:
            self._record_interfaces_implemented(plan)
            # 补齐状态机缺口：此前 implement_done 从未被调用，导致文档定义的
            # IMPLEMENTED 状态一次都没写过（IMPLEMENTING 直接跳到 PASSED）。
            # mark_implementation_done 会同时写表 + 发 requirement_state 事件。
            self.store.implement_done(req_id, f"{req_id} 实现完成（{attempts} 次尝试）")
            self.store.test_passed(req_id, f"{req_id} 测试通过: {outcome.summary()}")
            self.store.set_state(req_id, "PASSED", "test")
            self.store.commit(f"{req_id} (implement): {requirement.name}")
            result.state = "PASSED"
            logger.info("[门禁] %s PASS -> PASSED", req_id)
        elif outcome.passed and not dep_ok:
            # 三段违规都要进 note，否则会出现"括号里是空的"这种误导性提示
            parts = [f"{u.upstream}={u.verdict}" for u in dep_audit.violations]
            parts += [f"{u.upstream}={u.verdict}" for u in mock_audit.violations]
            if (
                self.config.block_injection_bypass
                and self.config.warn_injection_bypass
                and not bypass_audit.ok
            ):
                parts += [
                    f"{f.upstream}=INJECTION_BYPASS({f.parameter})" for f in bypass_audit.findings
                ]
            reason = "; ".join(parts) or "依赖未通过使用审计"
            self.store.test_failed(req_id, f"{req_id} 依赖未真实验证: {reason}")
            self.store.set_state(req_id, "FAILED", "test")
            self.store.commit(f"{req_id} (blocked): {requirement.name} 依赖未验证")
            result.state = "FAILED"
            result.note = (
                f"测试通过但依赖未被真实验证（{reason}）："
                f"重写 {attempts} 次仍未满足依赖使用要求"
            )
            logger.error("[依赖门禁] %s BLOCK -> FAILED（%s）", req_id, reason)
        else:
            self.store.test_failed(
                req_id, f"{req_id} 测试失败（{attempts} 次尝试）: {outcome.summary()}"
            )
            self.store.set_state(req_id, "FAILED", "test")
            self.store.commit(f"{req_id} (wip): {requirement.name} 未通过")
            result.state = "FAILED"
            result.note = result.note or f"测试未通过: {outcome.summary()}"
            # 与 WEAK_TEST 显式区分：这条是「测试有效（真的引用了实现）但实现没通过」，
            # 属于正常失败；而 WEAK_TEST 是测试本身不可信。此前两者都只写
            # 「BLOCK -> FAILED」，日志里无法一眼分辨。
            logger.error(
                "[门禁] %s TEST_FAILED: 测试有效但实现未通过（%s，%d 次尝试）",
                req_id,
                outcome.summary(),
                attempts,
            )

        return result
