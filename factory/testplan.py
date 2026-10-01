"""测试计划（A 的前提）：结构、校验、基线。

A 的核心问题是"什么叫计划内声明的路径"。答案是：**在测试生成之前先产出计划**，
由计划决定允许写哪些测试文件，而不是由模型的输出决定。

契约见 schemas/test_plan.schema.yaml。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

# 断言与用例计数（用于检测"删除已有断言"这一逃逸路径）
#
# 实测漏洞：只认 `assert.` / `expect(` 会漏掉**别名断言**——
#   const a = require('node:assert');  a.equal(x, 1)
# 这种情况下断言数恒为 0，"删除断言"永远检测不到（弱化守卫形同虚设）。
# 因此同时按**断言方法名**计数，覆盖别名、chai、jest/vitest 风格。
_ASSERTION_METHODS = (
    "equal", "strictEqual", "deepEqual", "deepStrictEqual",
    "notEqual", "notStrictEqual", "notDeepEqual", "notDeepStrictEqual",
    "ok", "fail", "throws", "rejects", "doesNotThrow", "doesNotReject",
    "match", "doesNotMatch", "ifError",
    "toBe", "toEqual", "toStrictEqual", "notToBe", "notToEqual",
    "toHaveProperty", "toHaveLength", "toBeDefined", "toBeUndefined",
    "toBeNull", "toBeTruthy", "toBeFalsy", "toBeNaN",
    "toBeGreaterThan", "toBeGreaterThanOrEqual",
    "toBeLessThan", "toBeLessThanOrEqual", "toBeInstanceOf",
    "toContain", "toMatch", "toThrow", "toHaveBeenCalled", "toHaveBeenCalledWith",
)
_ASSERTION_RE = re.compile(
    r"\b(?:assert|expect)\s*[.(]"
    + "".join(rf"|\.{name}\s*\(" for name in _ASSERTION_METHODS)
)
_TESTCASE_RE = re.compile(r"\b(?:test|it)\s*\(")


def normalize_test_path(path: str, backend_dir: str = "backend") -> str:
    """把测试计划里的路径规范化为 output_dir 相对路径。

    允许写 `tests/x.test.js`（backend 相对）或 `backend/tests/x.test.js`。
    """
    cleaned = str(path or "").strip().lstrip("./")
    prefix = f"{backend_dir}/"
    return cleaned if cleaned.startswith(prefix) else f"{prefix}{cleaned}"


def backend_relative(path: str, backend_dir: str = "backend") -> str:
    prefix = f"{backend_dir}/"
    return path[len(prefix):] if path.startswith(prefix) else path


def measure_source(source: str) -> tuple[int, int]:
    """返回 (断言数, 用例数)。"""
    return len(_ASSERTION_RE.findall(source)), len(_TESTCASE_RE.findall(source))


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestFileSpec:
    path: str                      # output_dir 相对，例如 backend/tests/req1.test.js
    type: str = "unit"             # unit | integration | e2e
    covers: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class RequirementTestPlan:
    req_id: str
    test_files: tuple[TestFileSpec, ...] = ()
    scenarios: tuple[str, ...] = ()
    notes: str = ""
    # 显式声明的**间接依赖**：上游 req_id -> 理由。
    # 声明后跳过 DEPENDENCY_NOT_USED 判定，记入 dependency_check_uncertain 供人工复核。
    # 存在的理由：有些模块合法地只 import 上游而不调用（类型定义、常量），
    # 有些经 A->B->C 间接调用，有些经回调/事件被调用——静态分析看不见。
    indirect_dependencies: tuple[tuple[str, str], ...] = ()
    # 显式声明**允许 mock** 的上游：req_id 或实现文件路径 -> 理由。
    # 未声明的上游 mock 判 UNVERIFIED_DEPENDENCY（阻断）。
    mocked_dependencies: tuple[tuple[str, str], ...] = ()
    # 显式声明**允许在重写阶段修改**的测试文件：路径 -> 理由。
    # 实现阶段默认不得改测试；若测试自身有 bug 必须改，走这个显式通道并记录。
    test_rewrite_reasons: tuple[tuple[str, str], ...] = ()

    def test_rewrite_reason(self, path: str) -> str:
        for name, reason in self.test_rewrite_reasons:
            if name == path:
                return reason
        return ""

    def mocked_reason(self, key: str) -> str:
        for name, reason in self.mocked_dependencies:
            if name == key:
                return reason
        return ""

    def mocked_map(self) -> dict[str, str]:
        return {name: reason for name, reason in self.mocked_dependencies}

    def indirect_reason(self, upstream: str) -> str:
        for req_id, reason in self.indirect_dependencies:
            if req_id == upstream:
                return reason
        return ""

    def indirect_map(self) -> dict[str, str]:
        return {req_id: reason for req_id, reason in self.indirect_dependencies}

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(spec.path for spec in self.test_files)

    def type_of(self, path: str) -> tuple[str, ...]:
        return tuple(spec.type for spec in self.test_files if spec.path == path)


@dataclass
class TestPlan:
    schema_version: str = "1.0"
    entries: dict[str, RequirementTestPlan] = field(default_factory=dict)

    def for_requirement(self, req_id: str) -> RequirementTestPlan | None:
        return self.entries.get(req_id)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "test_plan": [
                {
                    "requirement": entry.req_id,
                    "scenarios": list(entry.scenarios),
                    "test_files": [
                        {
                            "path": spec.path,
                            "covers": list(spec.covers),
                            "type": spec.type,
                            **({"notes": spec.notes} if spec.notes else {}),
                        }
                        for spec in entry.test_files
                    ],
                    **(
                        {
                            "indirect_dependencies": [
                                {"requires": req_id, "reason": reason}
                                for req_id, reason in entry.indirect_dependencies
                            ]
                        }
                        if entry.indirect_dependencies
                        else {}
                    ),
                    **(
                        {
                            "mocked_dependencies": [
                                {"target": name, "reason": reason}
                                for name, reason in entry.mocked_dependencies
                            ]
                        }
                        if entry.mocked_dependencies
                        else {}
                    ),
                    **({"notes": entry.notes} if entry.notes else {}),
                }
                for entry in self.entries.values()
            ],
        }


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_requirement_plan(payload: dict[str, Any], *, backend_dir: str = "backend") -> RequirementTestPlan:
    """从 LLM / 桩产出的单需求计划 JSON 构造结构。"""
    req_id = str(payload.get("requirement") or payload.get("req_id") or "").strip()
    scenarios = tuple(str(s).strip() for s in (payload.get("scenarios") or []) if str(s).strip())
    files: list[TestFileSpec] = []
    for item in payload.get("test_files") or []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        files.append(
            TestFileSpec(
                path=normalize_test_path(str(item["path"]), backend_dir),
                type=str(item.get("type") or "unit").strip().lower(),
                covers=tuple(str(c).strip() for c in (item.get("covers") or []) if str(c).strip()),
                notes=str(item.get("notes") or ""),
            )
        )
    indirect: list[tuple[str, str]] = []
    raw_indirect = payload.get("indirect_dependencies") or []
    if isinstance(raw_indirect, dict):
        raw_indirect = [{"requires": k, "reason": v} for k, v in raw_indirect.items()]
    for item in raw_indirect:
        if not isinstance(item, dict):
            continue
        requires = str(item.get("requires") or item.get("dependency") or "").strip()
        if requires:
            indirect.append((requires, str(item.get("reason") or "").strip()))

    mocked: list[tuple[str, str]] = []
    raw_mocked = payload.get("mocked_dependencies") or []
    if isinstance(raw_mocked, dict):
        raw_mocked = [{"target": k, "reason": v} for k, v in raw_mocked.items()]
    for item in raw_mocked:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or item.get("requires") or item.get("path") or "").strip()
        if target:
            mocked.append((target, str(item.get("reason") or "").strip()))

    rewrites: list[tuple[str, str]] = []
    raw_rewrites = payload.get("test_rewrite_reasons") or []
    if isinstance(raw_rewrites, dict):
        raw_rewrites = [{"path": k, "reason": v} for k, v in raw_rewrites.items()]
    for item in raw_rewrites:
        if not isinstance(item, dict):
            continue
        target = str(item.get("path") or item.get("target") or "").strip()
        if target:
            rewrites.append((normalize_test_path(target), str(item.get("reason") or "").strip()))

    return RequirementTestPlan(
        req_id=req_id,
        test_files=tuple(files),
        scenarios=scenarios,
        notes=str(payload.get("notes") or ""),
        indirect_dependencies=tuple(indirect),
        mocked_dependencies=tuple(mocked),
        test_rewrite_reasons=tuple(rewrites),
    )


def load_test_plan(path: Path, *, backend_dir: str = "backend") -> TestPlan:
    import yaml  # type: ignore

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    plan = TestPlan(schema_version=str(payload.get("schema_version") or "1.0"))
    for item in payload.get("test_plan") or []:
        if not isinstance(item, dict):
            continue
        entry = parse_requirement_plan(item, backend_dir=backend_dir)
        if entry.req_id:
            plan.entries[entry.req_id] = entry
    return plan


def write_test_plan(path: Path, plan: TestPlan) -> None:
    import yaml  # type: ignore

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(plan.to_payload(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 校验（计划本身也走门禁）
# ---------------------------------------------------------------------------

_TEST_SUFFIXES = (".test.js", ".test.ts", ".test.jsx", ".test.tsx",
                  ".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx")


def validate_requirement_plan(
    entry: RequirementTestPlan,
    *,
    requirement_scenarios: Iterable[str] = (),
    requirement_dependencies: Iterable[str] = (),
    backend_dir: str = "backend",
) -> list[str]:
    """返回违规列表；空列表表示通过。"""
    violations: list[str] = []
    # 路径白名单**按测试类型分流**：
    #   unit / integration -> backend/tests/
    #   **e2e             -> backend/test-e2e/**（Playwright 的 testDir，见
    #                        template/backend/playwright.config.js）
    # 分流的理由：vitest 的 include 是 `tests/**`，而 playwright 的 testDir 是
    # `test-e2e/` —— 两者是不同的运行器与目录约定。此前白名单只认
    # `backend/tests/`，于是 E2E 计划**必然被计划门禁拒绝** ——
    # UI 需求连测试计划都过不了，自然「从未进入生成路径」。
    unit_prefix = f"{backend_dir}/tests/"
    e2e_prefix = f"{backend_dir}/test-e2e/"

    def _expected_prefix(spec_type: str) -> str:
        return e2e_prefix if spec_type == "e2e" else unit_prefix

    if not entry.test_files:
        violations.append("test_files 为空：计划必须至少声明一个测试文件")

    seen: set[str] = set()
    for spec in entry.test_files:
        expect_prefix = _expected_prefix(spec.type)
        if not spec.path.startswith(expect_prefix):
            violations.append(
                f"{spec.path}: type={spec.type} 的测试必须位于 {expect_prefix} 下"
            )
        if not spec.path.endswith(_TEST_SUFFIXES):
            violations.append(f"{spec.path}: 不是测试文件后缀")
        if spec.path in seen:
            violations.append(f"{spec.path}: 路径重复")
        seen.add(spec.path)
        if spec.type not in {"unit", "integration", "e2e"}:
            violations.append(f"{spec.path}: type 非法（{spec.type}），应为 unit/integration/e2e")

    declared = set(requirement_scenarios)
    if declared:
        covered: set[str] = set()
        for spec in entry.test_files:
            unknown = set(spec.covers) - declared
            if unknown:
                violations.append(f"{spec.path}: covers 含未声明场景 {sorted(unknown)}")
            covered |= set(spec.covers)
        missing = declared - covered
        if missing:
            violations.append(f"以下场景没有被任何测试文件覆盖: {sorted(missing)}")

    allowed = set(requirement_dependencies)
    if allowed:
        for req_id, reason in entry.indirect_dependencies:
            if req_id not in allowed:
                violations.append(
                    f"indirect_dependencies 声明了 {req_id}，但它不是本需求的声明依赖"
                )
            if not reason.strip():
                violations.append(f"indirect_dependencies 里的 {req_id} 缺少理由")

    for name, reason in entry.mocked_dependencies:
        if not reason.strip():
            violations.append(f"mocked_dependencies 里的 {name} 缺少理由")
    for path, reason in entry.test_rewrite_reasons:
        if not reason.strip():
            violations.append(f"test_rewrite_reasons 里的 {path} 缺少理由")
        if path not in {spec.path for spec in entry.test_files}:
            violations.append(f"test_rewrite_reasons 里的 {path} 不是计划内测试文件")

    return violations


# ---------------------------------------------------------------------------
# 基线：监控"改断言 / 拆分文件 / 删断言"这些新逃逸路径
# ---------------------------------------------------------------------------


@dataclass
class TestFileBaseline:
    path: str
    sha256: str
    assertions: int
    test_cases: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def record_baseline(output_dir: Path, path: str) -> TestFileBaseline | None:
    target = output_dir / path
    if not target.is_file():
        return None
    text = target.read_text(encoding="utf-8")
    assertions, cases = measure_source(text)
    return TestFileBaseline(path=path, sha256=sha256_of(text), assertions=assertions, test_cases=cases)


def check_baseline(
    output_dir: Path, baseline: TestFileBaseline
) -> tuple[str, str] | None:
    """对比基线。返回 (违规码, 说明)；None 表示通过。

    允许修改内容（重写本来就要改），但**不允许减少**已有断言或用例数量。
    """
    target = output_dir / baseline.path
    if not target.is_file():
        return "TEST_FILE_REMOVED", f"{baseline.path} 被删除"
    text = target.read_text(encoding="utf-8")
    assertions, cases = measure_source(text)
    if cases < baseline.test_cases:
        return (
            "ASSERTION_DELETION",
            f"{baseline.path} 用例数减少 {baseline.test_cases} -> {cases}",
        )
    if assertions < baseline.assertions:
        return (
            "ASSERTION_DELETION",
            f"{baseline.path} 断言数减少 {baseline.assertions} -> {assertions}",
        )
    return None


# ═══════════════════════════════════════════════════════════════════════════
# 根节点测试用例数上限（骨架，默认关闭）
# ═══════════════════════════════════════════════════════════════════════════
#
# 【假说】根节点（无上游依赖）无集成约束，模型倾向把场景铺开写；
#         深层节点围绕上游契约写、更聚焦。用例数越多，实现难度越高。
#
# 【实测 n=1】run2 的 REQ-5（根节点）= 6 用例 -> FAILED(4 轮)
#             run1/run3 的 REQ-5        = 2 用例 -> PASSED(1 轮)
#
# 【诚实限定】失败样本只有 1 个。不能排除「那次恰好实现较弱」。
#             本骨架**默认关闭**，等基线样本补齐后做对照实验再决定。
#
# 【实现要点 —— 与最初设想的差异，必须记录】
# 最初设想是 `test_cases[:LIMIT]` 切片。**在本代码库里这不成立**：
#   `TestFileSpec` 只有 path/type/covers/notes，**没有用例数字段**；
#   用例数（`it(` 块个数）是在**写测试阶段**由模型产出源码时才确定的，
#   由 `measure_source()` 事后数出来。
# 真正能起作用的杠杆是**提示词**：在计划与写测试之前告诉模型"这是根节点，
# 用例数不超过 N"。切片只能作用于计划里的**文件清单**（通常只有 1 个文件，
# 上限 3-4 不会咬合），所以切片是**辅助**、提示词是**主机制**。
#
# 可观测：`RequirementResult.test_case_count` 记录每个需求实际写出的用例数。


def is_root_requirement(requirement: Any) -> bool:
    """根节点 = 未声明任何上游依赖。"""
    return not tuple(getattr(requirement, "dependencies", ()) or ())


def root_test_limit(default: int = 0) -> int:
    """读取当前上限。0 表示关闭。

    优先用环境变量（便于实验时不改配置对象），否则用传入的默认值。
    """
    import os as _os
    raw = _os.environ.get("FACTORY_ROOT_TEST_LIMIT")
    if raw is None:
        return max(0, int(default or 0))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return max(0, int(default or 0))


def apply_root_test_limit(
    requirement: Any,
    test_files: tuple[str, ...],
    *,
    limit: int | None = None,
) -> tuple[str, ...]:
    """按上限裁剪**根节点**的计划内测试文件清单。

    关闭（limit == 0）时**原样返回** —— 既有行为逐位不变。
    非根节点原样返回（只约束根节点）。

    注意：本函数作用在**文件清单**上；用例数上限由
    `root_case_limit_prompt()` 通过提示词施加（见上文实现要点）。
    """
    eff = root_test_limit() if limit is None else max(0, int(limit))
    if eff == 0:
        return test_files
    if not is_root_requirement(requirement):
        return test_files
    return tuple(test_files[:eff])


def root_case_limit_prompt(requirement: Any, *, limit: int | None = None) -> str:
    """给根节点注入用例数上限指令；关闭或非根节点时返回空串。

    返回空串时调用方不应在提示词里留下任何痕迹 —— 保证关闭状态下
    提示词与从前逐字一致。
    """
    eff = root_test_limit() if limit is None else max(0, int(limit))
    if eff == 0 or not is_root_requirement(requirement):
        return ""
    # 提示词必须**可执行**：具体数字 + 理由 + 示例。
    # 只说「不超过 N」是抽象约束，模型无从判断"哪几个该留"——
    # 这与「拒绝理由必须可执行」是同一条教训：
    #   把理由写到能让对方知道**下一步做什么**，而不只是知道**做错了**。
    return (
        f"\n★ 本需求是**根节点**（无上游依赖）。\n"
        f"  测试用例数**不超过 {eff} 个**。\n"
        "\n"
        "  理由：根节点没有上游契约约束，容易把场景铺开写；\n"
        "  而每个用例都会抬高实现难度 —— 用例数与通过率**不成正比**。\n"
        "  实测：同一需求写 6 个用例时实现 4 轮未通过；写 2 个用例时 1 轮通过。\n"
        "\n"
        f"  示例：{eff} 个之内覆盖核心场景即可 —— "
        "1 个正常路径 + 1 个边界/异常 + 1 个副作用验证。\n"
        "  把最关键的行为写**扎实**（断言具体值），而不是把场景写**全**。\n"
    )


def count_test_cases(source: str) -> int:
    """数出源码里的用例块个数（`it(` / `test(`）。

    与 `measure_source` 的用例计数口径一致 —— 不另立第二套口径。
    """
    return measure_source(source)[1]


def plan_json_schema_hint() -> str:
    """给 LLM 的回报格式提示。"""
    return json.dumps(
        {
            "requirement": "REQ-1",
            "scenarios": ["REQ-1-SCN-1"],
            "test_files": [
                {
                    "path": "backend/tests/req1.register.test.js",
                    "covers": ["REQ-1-SCN-1"],
                    "type": "unit",
                }
            ],
        },
        ensure_ascii=False,
    )
