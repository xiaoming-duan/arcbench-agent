"""生成器：设计 / 写测试 / 实现。

两个实现：
  LLMGenerator  —— 生产路径，调用注入的 OpenAI 兼容模型
  StubGenerator —— 桩，fixture 驱动，用于在无模型凭据时打通全链路

桩生成器**不是**真正的代码生成器，它只负责证明流水线是通的：
需求 -> 设计 -> 红 -> 实现 -> 绿 -> traceability -> git。
真实生成能力由 LLMGenerator 提供。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, Sequence

from .llm import ModelClient
from .models import DesignPlan, GeneratedFile, InterfaceSpec, Requirement, TestSpec
from .testplan import (
    RequirementTestPlan,
    TestFileSpec,
    normalize_test_path,
    parse_requirement_plan,
    plan_json_schema_hint,
)

logger = logging.getLogger("factory.generator")

TEMPLATE_CONVENTIONS = """\
项目约定（必须遵守）：
- 工程根下有两个包：backend/（Express 5 + SQLite，CommonJS）与 frontend/（React 19 + Vite + Tailwind）。
- 后端路由挂载点：backend/src/app.js 中的注释 `// route modules imports` 之后 require 路由模块；
  在 `// register routes` 之后用 app.use('/api/...', router) 注册。
- 后端单元/集成测试放在 backend/tests/，前端测试放在 frontend/tests/。
- 后端源码在 backend/src/ 下；不要修改 backend/src/app.js 之外的模板基础设施，除非确有必要。
- 所有 API 前缀必须是 /api。
"""


class Generator(Protocol):
    name: str

    def design(self, requirement: Requirement) -> DesignPlan: ...

    def plan_tests(
        self, requirement: Requirement, plan: DesignPlan, plan_feedback: str = ""
    ) -> RequirementTestPlan: ...

    def write_tests(
        self,
        requirement: Requirement,
        plan: DesignPlan,
        weak_feedback: str = "",
        allowed_paths: Sequence[str] = (),
    ) -> list[GeneratedFile]: ...

    def implement(
        self,
        requirement: Requirement,
        plan: DesignPlan,
        failures: Sequence[str],
        test_context: str = "",
    ) -> list[GeneratedFile]: ...


# ---------------------------------------------------------------------------
# 桩生成器：fixture 驱动
# ---------------------------------------------------------------------------


class StubGenerator:
    """fixture 驱动的桩生成器。

    目录约定（相对于需求目录）::

        fixtures/<REQ-ID>/tests/**   -> 写测试阶段原样落盘（路径镜像 output_dir）
        fixtures/<REQ-ID>/impl/**    -> 实现阶段原样落盘
        fixtures/<REQ-ID>/patches.yaml -> 实现阶段执行的插入式补丁

    测试先落盘、实现后落盘，因此可产生真实的 RED -> GREEN。
    """

    name = "stub"

    def __init__(self, fixture_root: Path) -> None:
        self.fixture_root = fixture_root

    # ---- 内部 ----

    def _fixture_dir(self, req_id: str) -> Path:
        return self.fixture_root / req_id

    def _collect(self, directory: Path, mode: str = "write") -> list[GeneratedFile]:
        if not directory.is_dir():
            return []
        files: list[GeneratedFile] = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(directory).as_posix()
            files.append(
                GeneratedFile(path=relative, content=path.read_text(encoding="utf-8"), mode=mode)
            )
        return files

    def _load_patches(self, req_id: str) -> list[GeneratedFile]:
        patch_file = self._fixture_dir(req_id) / "patches.yaml"
        if not patch_file.is_file():
            return []
        import yaml  # type: ignore

        payload = yaml.safe_load(patch_file.read_text(encoding="utf-8")) or []
        if not isinstance(payload, list):
            raise ValueError(f"{patch_file} 顶层必须是列表")
        out: list[GeneratedFile] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            out.append(
                GeneratedFile(
                    path=str(entry["path"]),
                    content=str(entry.get("content", "")),
                    mode=str(entry.get("mode", "insert_after")),
                    marker=entry.get("marker"),
                )
            )
        return out

    # ---- Generator 协议 ----

    def plan_tests(
        self, requirement: Requirement, plan: DesignPlan, plan_feedback: str = ""
    ) -> RequirementTestPlan:
        """桩计划：fixture 里有哪些测试文件，就声明哪些。"""
        if plan_feedback:
            logger.warning("[stub] fixture 固定，无法根据计划门禁反馈修正 %s 的计划", requirement.req_id)
        fixture_tests = self._fixture_dir(requirement.req_id) / "tests"
        scenarios = tuple(s.scenario_id for s in requirement.scenarios)
        specs: list[TestFileSpec] = []
        if fixture_tests.is_dir():
            for path in sorted(fixture_tests.rglob("*")):
                if path.is_file():
                    specs.append(
                        TestFileSpec(
                            path=path.relative_to(fixture_tests).as_posix(),
                            type="unit",
                            covers=scenarios,
                        )
                    )
        return RequirementTestPlan(
            req_id=requirement.req_id,
            test_files=tuple(specs),
            scenarios=scenarios,
            notes="[stub] 由 fixture 目录推导",
        )

    def design(self, requirement: Requirement) -> DesignPlan:
        """桩设计：直接采用需求中声明的接口与测试。"""
        return DesignPlan(
            req_id=requirement.req_id,
            summary=f"[stub] {requirement.name}",
            steps=("读取 fixture", "写测试", "跑红", "写实现", "跑绿"),
            interfaces=requirement.interfaces,
            tests=requirement.tests,
        )

    def write_tests(
        self,
        requirement: Requirement,
        plan: DesignPlan,
        weak_feedback: str = "",
        allowed_paths: Sequence[str] = (),
    ) -> list[GeneratedFile]:
        if weak_feedback:
            logger.warning(
                "[stub] fixture 是固定的，无法根据 WEAK_TEST 反馈重写 %s 的测试", requirement.req_id
            )
        files = self._collect(self._fixture_dir(requirement.req_id) / "tests")
        if not files:
            raise FileNotFoundError(
                f"[stub] 缺少测试 fixture: {self._fixture_dir(requirement.req_id) / 'tests'}。"
                "请补充 fixture，或配置 OPENAI_API_KEY 使用 LLMGenerator。"
            )
        logger.info("[stub] %s 写入 %d 个测试文件", requirement.req_id, len(files))
        return files

    def implement(
        self,
        requirement: Requirement,
        plan: DesignPlan,
        failures: Sequence[str],
        test_context: str = "",
    ) -> list[GeneratedFile]:
        files = self._collect(self._fixture_dir(requirement.req_id) / "impl")
        files.extend(self._load_patches(requirement.req_id))
        if not files:
            raise FileNotFoundError(
                f"[stub] 缺少实现 fixture: {self._fixture_dir(requirement.req_id) / 'impl'}。"
                "请补充 fixture，或配置 OPENAI_API_KEY 使用 LLMGenerator。"
            )
        logger.info("[stub] %s 写入 %d 个实现变更", requirement.req_id, len(files))
        return files


# ---------------------------------------------------------------------------
# LLM 生成器
# ---------------------------------------------------------------------------

# 所有要求 JSON 输出的 prompt 共用这一段。实测模型（HTTP 200）会返回 markdown
# 围栏、或前后带「好的，这是设计：」这类解释文字，导致解析失败并白耗一次重试。
# 与其只在解析端容忍，不如在提示词里先把格式锁死——两边都做才稳。
_JSON_CONTRACT = """

输出要求（违反会导致解析失败、整条需求作废）：
- 只输出 JSON，不要任何解释文字、不要任何前后缀。
- 不要使用 markdown 代码围栏（不要写 ```json，也不要写 ```）。
- 第一个字符必须是 {，最后一个字符必须是 }。
- 输出规模上限（实测超出会被 max_tokens 截断，半截 JSON 等于整条需求作废）：
  summary ≤ 200 字符；steps ≤ 5 条且每条 ≤ 150 字符；
  interfaces ≤ 3 个且 content ≤ 200 字符；tests ≤ 4 条且 intent ≤ 120 字符。
  宁可短而完整，不可长而被截断。
- 如果确实无法完成，输出 {"error": "原因"}，不要输出半截 JSON。"""


_DESIGN_SYSTEM = """你是一名架构师。针对单个需求，产出可验证的设计。
只输出 JSON 对象，结构：
{
  "summary": "一句话设计说明",
  "steps": ["实现步骤"],
  "interfaces": [{"interface_id":"REQ-1.API.X","type":"api|ui|db","content":"契约描述","file_path":"backend/src/..."}],
  "tests": [{"test_id":"REQ-1.TEST.X","type":"unit|integration|e2e","intent":"这条测试要验证什么","scenario_id":null,"interface_ids":["REQ-1.API.X"],"file_path":"backend/tests/..."}]
}
要求：
1. 每个接口至少一条测试；测试必须能在实现前失败（RED）、实现后通过（GREEN）。
2. 必须至少设计一个**服务层纯函数接口**（type 用 "db"，file_path 形如 backend/src/services/xxx.js），
   它承载该需求的可独立验证的业务逻辑，是测试直接 require 的目标。
3. 测试的 interface_ids 必须指向上述服务层接口，而不是 Express 路由接口。""" + _JSON_CONTRACT

_PLAN_SYSTEM = """你是一名测试架构师。针对单个需求产出测试计划。
只输出 JSON 对象，结构：
{
  "requirement": "REQ-1",
  "scenarios": ["场景ID"],
  "test_files": [{"path":"backend/tests/xxx.test.js","covers":["场景ID"],"type":"unit|integration|e2e"}],
  "notes": "可选"
}
计划中的 path 就是本次允许写测试的唯一白名单。""" + _JSON_CONTRACT

_FILES_SYSTEM = """你是一名工程师。只输出 JSON 对象，结构：
{"files":[{"path":"backend/src/...","content":"完整文件内容","mode":"write"}]}
mode 可用 write（整体覆盖）或 insert_after（需要同时给 marker 字段，在 marker 行后插入 content）。
不要输出 JSON 以外的任何内容。""" + _JSON_CONTRACT


class LLMGenerator:
    """生产路径：由注入的 OpenAI 兼容模型驱动。"""

    name = "llm"

    def __init__(self, client: ModelClient, test_dialect: str) -> None:
        self.client = client
        self.test_dialect = test_dialect

    # ---- 内部 ----

    def _dialect_note(self) -> str:
        if self.test_dialect == "vitest":
            return (
                "测试使用 vitest：`import { describe, it, expect } from 'vitest';`，"
                "文件放在 backend/tests/ 下（vitest.config.js 的 include 为 tests/**）。\n"
                "★ 硬约束：**禁止在 `vi.hoisted(() => ...)` 内引用顶层 `import` / `require` 的绑定**。\n"
                "  `vi.hoisted` 的回调在模块加载**之前**执行，访问不到顶层作用域，"
                "写了必然抛 `ReferenceError: Cannot access ... before initialization`；\n"
                "  这会让整个文件在**收集阶段**就失败、0 个测试被发现 —— "
                "测试文件连跑都跑不起来，实现阶段无论如何都修不好。\n"
                "  正确写法：在 `vi.hoisted` 回调**内部** `const Database = require('better-sqlite3')`，"
                "或改用 `vi.importActual`；\n"
                "  能用普通顶层变量 + `beforeEach` 完成的，就不要用 `vi.hoisted`。"
            )
        return (
            "测试使用 Node 内置测试运行器：`const test = require('node:test'); "
            "const assert = require('node:assert');`，文件放在 backend/tests/ 下。\n"
            "★ 关键约束：当前验证环境**未安装任何 npm 依赖**（express / sqlite3 / supertest 都不可用）。\n"
            "  - 测试**只能** require 纯 JavaScript 模块，绝对不能 require express / supertest / sqlite3；\n"
            "  - 必须把可验证的业务逻辑抽到 backend/src/services/ 下的纯函数模块，测试直接 require 它；\n"
            "  - Express 路由文件照常编写（供生产运行），但不要写加载 app.js 的测试。"
        )

    def _requirement_brief(self, requirement: Requirement) -> str:
        lines = [
            f"需求 ID: {requirement.req_id}",
            f"名称: {requirement.name}",
            f"描述: {requirement.description or '(未提供)'}",
        ]
        if requirement.dependencies:
            lines.append(f"依赖: {', '.join(requirement.dependencies)}")
        if requirement.acceptance:
            lines.append("验收标准:")
            lines.extend(f"  - {item}" for item in requirement.acceptance)
        if requirement.scenarios:
            lines.append("验收场景:")
            for scenario in requirement.scenarios:
                lines.append(f"  {scenario.scenario_id} {scenario.name}")
                for step in scenario.steps:
                    lines.append(f"    {step.get('keyword', '')} {step.get('content', '')}")
        if requirement.interfaces:
            lines.append("已声明接口:")
            lines.extend(
                f"  {i.interface_id} [{i.type}] {i.content}" for i in requirement.interfaces
            )
        if requirement.tests:
            lines.append("已声明测试:")
            lines.extend(f"  {t.test_id} [{t.type}] {t.intent}" for t in requirement.tests)

        # ---- 跨模块调用契约（可选字段；未声明时本段完全不出现在提示词里）----
        #
        # 为什么必须在提示词里：单模块测试无法暴露「同名不同语义」。
        # 实测 REQ-11：下游测试按 `updateQuantity(sku, quantity, from, to)` 调用，
        # 而上游 REQ-7 实现的是 `updateQuantity(sku, from, to)`（区间变更语义）。
        # 模型被告知「你没 import 上游」时，**无法推断出正确的参数语义** ——
        # 它只知道要调，不知道按什么调。
        if requirement.cross_module_calls:
            lines.append("")
            lines.append("★ 本需求对上游的**跨模块调用契约**（必须真实调用，不得用参数注入绕过）:")
            for call in requirement.cross_module_calls:
                head = f"  - {call.upstream} / {call.symbol}"
                if call.signature:
                    head += f"  签名: {call.signature}"
                lines.append(head)
                if call.semantics:
                    lines.append(f"    语义: {call.semantics}")
                if call.side_effects:
                    lines.append(f"    副作用: {'；'.join(call.side_effects)}")
            lines.append("  实现要求: ① require 上游模块；② 严格按上面的签名与语义调用；"
                         "③ 不得保留参数注入旁路。")
            lines.append("  测试要求: 断言上游被按上述签名调用后的**可观察后果**"
                         "（如副作用生效），而不是断言一个假的注入实现。")

        # ---- 我被别人依赖的签名（由适配层从全量需求推导，非 YAML 字段）----
        #
        # 只约束下游是不够的：上游若无此约束，仍会按自己的理解实现，
        # 契约在结构上无法满足。这段让上游知道「必须提供什么形状的接口」。
        if requirement.incoming_contracts:
            lines.append("")
            lines.append("★ 下游需求依赖本需求，**本需求必须提供**以下符号与签名"
                         "（实现时以此为准，不要自行改参数个数或语义）:")
            for call in requirement.incoming_contracts:
                head = f"  - 供 {call.symbol}"
                if call.signature:
                    head += f"  必须实现为: {call.signature}"
                lines.append(head)
                if call.semantics:
                    lines.append(f"    下游期望的语义: {call.semantics}")
                if call.side_effects:
                    lines.append(f"    下游期望的副作用: {'；'.join(call.side_effects)}")
        return "\n".join(lines)

    def _files_from_payload(self, payload: dict[str, Any]) -> list[GeneratedFile]:
        files: list[GeneratedFile] = []
        for entry in payload.get("files", []) or []:
            if not isinstance(entry, dict) or not entry.get("path"):
                continue
            files.append(
                GeneratedFile(
                    path=str(entry["path"]),
                    content=str(entry.get("content", "")),
                    mode=str(entry.get("mode", "write")),
                    marker=entry.get("marker"),
                )
            )
        return files

    def _request_files(self, *, system: str, user: str, requirement: Requirement) -> list[GeneratedFile]:
        payload = self.client.complete_json(system=system, user=user)
        files = self._files_from_payload(payload)
        if not files:
            raise ValueError(f"模型未为 {requirement.req_id} 产出任何文件变更")
        return files

    # ---- Generator 协议 ----

    def design(self, requirement: Requirement) -> DesignPlan:
        payload = self.client.complete_json(
            system=_DESIGN_SYSTEM,
            user=f"{TEMPLATE_CONVENTIONS}\n\n{self._requirement_brief(requirement)}",
        )
        interfaces = tuple(
            InterfaceSpec(
                interface_id=str(item.get("interface_id") or f"{requirement.req_id}.IF.{index}"),
                req_ids=(requirement.req_id,),
                type=str(item.get("type") or "api").lower(),
                content=str(item.get("content") or ""),
                file_path=item.get("file_path"),
            )
            for index, item in enumerate(payload.get("interfaces", []) or [], start=1)
            if isinstance(item, dict)
        )
        tests = tuple(
            TestSpec(
                test_id=str(item.get("test_id") or f"{requirement.req_id}.TEST.{index}"),
                req_id=requirement.req_id,
                type=str(item.get("type") or "unit"),
                intent=str(item.get("intent") or ""),
                scenario_id=item.get("scenario_id"),
                interface_ids=tuple(item.get("interface_ids") or ()),
                file_path=item.get("file_path"),
            )
            for index, item in enumerate(payload.get("tests", []) or [], start=1)
            if isinstance(item, dict)
        )
        return DesignPlan(
            req_id=requirement.req_id,
            summary=str(payload.get("summary") or requirement.name),
            steps=tuple(str(s) for s in (payload.get("steps") or [])),
            interfaces=interfaces or requirement.interfaces,
            tests=tests or requirement.tests,
        )

    def plan_tests(
        self, requirement: Requirement, plan: DesignPlan, plan_feedback: str = ""
    ) -> RequirementTestPlan:
        """产出测试计划 —— A 的白名单来源。计划先于测试文件生成。"""
        scenarios = [s.scenario_id for s in requirement.scenarios]
        user = (
            f"{TEMPLATE_CONVENTIONS}\n{self._dialect_note()}\n\n"
            f"{self._requirement_brief(requirement)}\n\n"
            f"设计说明: {plan.summary}\n\n"
            "请为该需求产出**测试计划**（先有计划，才有测试文件）。只输出 JSON 对象：\n"
            f"{plan_json_schema_hint()}\n\n"
            "硬性要求：\n"
            f"1. test_files 至少一个，path 必须位于 backend/tests/ 下；\n"
            f"2. 必须覆盖全部验收场景，期望场景 ID 为: {scenarios}；\n"
            "3. type 只能是 unit / integration / e2e；只有确实走真实进程或 HTTP 的才可为 e2e；\n"
            "4. 计划的路径就是白名单——后续只允许在这些文件里写测试。"
            + (f"\n\n{plan_feedback}" if plan_feedback else "")
        )
        payload = self.client.complete_json(system=_PLAN_SYSTEM, user=user)
        payload.setdefault("requirement", requirement.req_id)
        payload.setdefault("scenarios", scenarios)
        entry = parse_requirement_plan(payload)
        if not entry.req_id:
            entry = RequirementTestPlan(req_id=requirement.req_id, test_files=entry.test_files,
                                        scenarios=tuple(scenarios))
        return entry

    def _test_file_contract(self) -> str:
        """测试文件的硬性写法约束（**只用于写测试阶段**，不约束实现文件）。

        为什么单独抽出来：`_dialect_note()` 同时被 implement 使用，而实现文件
        必须跟随模板的 CommonJS 风格，不能被「测试必须 ESM」污染。
        """
        lines = [
            "★ 测试文件写法（硬性，违反会被静态检查拦下）：",
            "1) **相对路径深度**：测试在 backend/tests/ 下，实现在 backend/src/ 下，",
            "   所以引用实现是 `../src/...`（**一层** ..）。",
            "   写成 `../../src/...` 会退到项目根、解析不到实现，"
            "会被审计判成 NO_IMPLEMENTATION_IMPORT（实测就是这么错的）。",
            "2) **精简**：单个测试文件 ≤ 200 行；不要在每个测试文件里重建完整数据库 "
            "schema 或 seed 数据；需要数据时用最小内存结构，或复用已有的 fixture/helper。",
            "   输出越长越容易在 JSON 中途被 max_tokens 截断 —— 截断等于整条需求作废。",
        ]
        if self.test_dialect == "vitest":
            lines += [
                "3) **必须用 ESM 语法**：`import { describe, it, expect } from 'vitest';`",
                "   禁止 `require('vitest')` —— vitest 是纯 ESM 包，CJS require 必然抛",
                "   「Vitest cannot be imported in a CommonJS module using require()」；",
                "   禁止 `module.exports`；引用实现也必须用 `import ... from '../src/...'`。",
                "   即使项目源码是 CommonJS，**测试文件仍然必须是 ESM**（vitest 会转译测试）。",
            ]
        else:
            lines += [
                "3) 使用 Node 内置测试运行器：`const test = require('node:test');`",
                "   `const assert = require('node:assert');`",
            ]
        return "\n".join(lines)

    def write_tests(
        self,
        requirement: Requirement,
        plan: DesignPlan,
        weak_feedback: str = "",
        allowed_paths: Sequence[str] = (),
    ) -> list[GeneratedFile]:
        # ★ 关键：路径必须来自**测试计划**，而不是 design() 自选的 file_path。
        # 实测缺陷：两者由不同的模型调用产出，必然不一致 ——
        # 模型忠实写出 design 给的路径，白名单却按计划拒绝，12/12 需求首轮全被拒。
        paths = list(allowed_paths) or [
            (t.file_path or f"backend/tests/{t.test_id.lower()}.test.js") for t in plan.tests
        ]
        test_lines = "\n".join(
            f"- {t.test_id} [{t.type}] {t.intent} -> {paths[i] if i < len(paths) else paths[-1]}"
            for i, t in enumerate(plan.tests)
        )
        feedback_block = ""
        if weak_feedback:
            feedback_block = (
                "\n\n★★ 上一版测试被门禁判定为**空转测试（WEAK_TEST）**，必须重写：\n"
                f"{weak_feedback}\n"
                "常见错误写法：在测试文件里自己造一个 mock / 假数据 / 复刻一份实现，然后断言这份假数据。\n"
                "这种测试无论实现是否存在都会通过，等于没有测试。\n"
                "正确做法：从真实实现模块 import / require 被测函数，断言它的真实行为。\n"
                "在当前代码库上运行必须**失败**（因为实现还不存在），这是 RED 的唯一证明。"
            )
        user = (
            f"{TEMPLATE_CONVENTIONS}\n{self._dialect_note()}\n\n"
            f"{self._requirement_brief(requirement)}\n\n"
            f"设计说明: {plan.summary}\n"
            f"需要落地的测试:\n{test_lines}\n\n"
            "请只产出测试文件。测试必须针对尚未实现的接口，因此在当前代码库上运行必然失败（RED）。\n"
            "★ 路径约束（硬性）：只能写上面列出的这些路径，必须**逐字符一致**，"
            "不得新增、重命名或拆分任何测试文件。\n"
            f"{self._test_file_contract()}\n"
            "在当前代码库上运行必须失败（RED）——这是唯一的有效证明。"
            + feedback_block
        )
        return self._request_files(system=_FILES_SYSTEM, user=user, requirement=requirement)

    def implement(
        self,
        requirement: Requirement,
        plan: DesignPlan,
        failures: Sequence[str],
        test_context: str = "",
    ) -> list[GeneratedFile]:
        failure_block = ""
        if failures:
            failure_block = "\n\n当前测试失败信息（请针对性修复）:\n" + "\n".join(
                f"- {line}" for line in list(failures)[:30]
            )
        # 把**测试源码**直接给模型看。
        # 实测病理（REQ-7）：接口契约写 updateQuantity(sku, quantity)，
        # 而测试按仓储注入写成 updateQuantity(repository, sku, quantity)——
        # 两个契约互相矛盾，而提示词只说「测试是权威」却不给测试源码，
        # 模型只能猜签名，4 轮重写都没收敛。
        test_contract_block = ""
        if test_context:
            test_contract_block = (
                "\n\n★ 以下是本需求的**测试源码**，它就是权威契约。\n"
                "请以它的**精确调用签名、参数顺序、返回结构**为准 —— "
                "若与上面的「接口契约」描述不一致，以测试源码为准。\n"
                f"```javascript\n{test_context}\n```"
            )
        user = (
            f"{TEMPLATE_CONVENTIONS}\n{self._dialect_note()}\n\n"
            f"{self._requirement_brief(requirement)}\n\n"
            f"设计说明: {plan.summary}\n"
            f"接口契约（若与下面的测试源码冲突，以测试源码为准）:\n"
            + "\n".join(f"- {i.interface_id} [{i.type}] {i.content}" for i in plan.interfaces)
            + test_contract_block
            + "\n\n请产出实现代码，使上面声明的测试全部通过（GREEN）。"
            + "\n★ 硬性约束：只允许修改 backend/src/ 下的实现代码，"
            "绝对不要新增、删除或改写 backend/tests/ 下的任何测试文件。"
            "测试是权威信号，不允许通过改动测试来让它变绿。"
            "如果测试与已声明的接口契约冲突，请修改实现去满足测试。"
            + failure_block
        )
        return self._request_files(system=_FILES_SYSTEM, user=user, requirement=requirement)


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------


def build_generator(
    kind: str,
    *,
    fixture_root: Path,
    test_dialect: str,
    model_client: ModelClient | None = None,
) -> Generator:
    if kind == "llm":
        client = model_client or ModelClient()
        if not client.is_available():
            raise RuntimeError(
                "generator=llm 但模型不可用：请检查运行环境是否注入了 "
                "OPENAI_API_KEY / OPENAI_BASE_URL / MODEL 三个变量。"
            )
        logger.info("使用 LLMGenerator (model=%s)", client.model)
        return LLMGenerator(client, test_dialect)
    if test_dialect == "vitest":
        logger.warning(
            "StubGenerator 的 fixture 是 node 测试方言，vitest 环境下不会被执行。"
            "生产环境请配置 OPENAI_API_KEY 使用 LLMGenerator，或提供 vitest 方言的 fixture。"
        )
    logger.info("使用 StubGenerator (fixture=%s)", fixture_root)
    return StubGenerator(fixture_root)
