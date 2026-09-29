"""测试 import 审计（C）。

对每个**声明内**的测试文件做静态扫描，判断它是否真的引用了实现代码。

=====================  定位（重要）  =====================
本模块解决的不是"重写反馈"问题——A/B 实验已证明那种用法零增量
（见 EXPERIMENT.md 第 3 节）。

它解决的是 RED 门禁的**结构性漏洞**：
RED 的判定对象是"这一组测试整体有没有失败"，因此模型可以
**新增一个会失败的测试文件**，而把空转文件留在原地，整组失败即放行。
审计是唯一能看见"空转文件与正常文件并存"的手段——RED 在原理上看不见。
=======================================================
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# import ... from 'x' / import 'x'
_IMPORT_FROM = re.compile(r"""\bimport\s+(?:[\w*{},\s$]+\s+from\s+)?['"]([^'"]+)['"]""")
# require('x')
_REQUIRE = re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""")
# import('x')  —— 字面量动态 import
_DYNAMIC_LITERAL = re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""")
# import(  —— 非字面量（变量表达式），无法静态解析
_DYNAMIC_NON_LITERAL = re.compile(r"""\bimport\s*\(\s*(?!['"])""")

TEST_FILE_SUFFIXES = (
    ".test.js", ".test.ts", ".test.jsx", ".test.tsx",
    ".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx",
)

# 测试工具文件：不审计，但不能作为唯一的测试文件
HELPER_PATTERNS = (
    "/helpers/", "/fixtures/", "/__mocks__/", "/test-utils/",
    ".helper.", ".helpers.", ".fixture.", ".fixtures.", ".mock.", ".mocks.",
)
HELPER_EXACT = ("setup.js", "setup.ts", "setup.tsx", "test-setup.js", "vitest.setup.js")

# 判定
V_IMPORTS = "IMPORTS_IMPLEMENTATION"
V_NO_IMPORT = "NO_IMPLEMENTATION_IMPORT"
# 动态 import 必须分两种——语义完全相反，混成一个 verdict 会误杀合法引用：
#   LITERAL    路径是字面量，可静态解析 -> **算引用实现**
#   UNRESOLVED 路径拼接/变量传入，无法解析 -> **不算引用**（判 WEAK_TEST）
V_DYNAMIC_LITERAL = "DYNAMIC_IMPORT_LITERAL"
V_DYNAMIC_UNRESOLVED = "DYNAMIC_IMPORT_UNRESOLVED"
V_ALIAS = "ALIAS_UNRESOLVED"
V_MISSING = "MISSING_FILE"
V_E2E = "E2E_EXEMPT"
V_HELPER = "HELPER_EXEMPT"

# ★ 单一定义：什么算「引用了实现」。
#   门禁（判 WEAK_TEST）与守卫（判"有意义"）**只允许调下面两个函数**，
#   不许各处就地写 `verdict != X` —— 那样一旦新增 verdict，两边会静默分叉：
#   一个放行、一个阻断，而且不报错（这正是上一版 DYNAMIC_IMPORT 的坑）。
#   MEANINGFUL / WEAK / EXEMPT 三者互补，恰好划分全部 verdict。
MEANINGFUL_VERDICTS = frozenset({V_IMPORTS, V_DYNAMIC_LITERAL})
WEAK_VERDICTS = frozenset({V_NO_IMPORT, V_DYNAMIC_UNRESOLVED, V_ALIAS, V_MISSING})
EXEMPT_VERDICTS = frozenset({V_E2E, V_HELPER})


def is_meaningful_import(verdict: str) -> bool:
    """该 verdict 是否构成「引用了实现」的证据。门禁与守卫共用此判据。"""
    return verdict in MEANINGFUL_VERDICTS


def is_weak_verdict(verdict: str) -> bool:
    """该 verdict 是否应判 WEAK_TEST（未引用实现）。"""
    return verdict in WEAK_VERDICTS


def is_test_file(path: str) -> bool:
    return path.endswith(TEST_FILE_SUFFIXES)


def is_helper_file(path: str) -> bool:
    lowered = path.lower()
    if Path(lowered).name in HELPER_EXACT:
        return True
    return any(pattern in lowered for pattern in HELPER_PATTERNS)


def looks_like_alias(specifier: str) -> bool:
    """`@/x`、`~/x` 这类多半是路径别名；真外部包（express、vitest）不会这样开头。"""
    return specifier.startswith("@") or specifier.startswith("~")


def extract_specifiers(source: str) -> list[str]:
    """抽取源码里所有 import/require 目标（含字面量动态 import）。"""
    return [spec for spec, _ in extract_specifier_kinds(source)]


def extract_specifier_kinds(source: str) -> list[tuple[str, bool]]:
    """抽取 import/require 目标，并标注是否来自**字面量动态 import**。

    返回 [(specifier, is_dynamic_literal), ...]，按首次出现去重。
    区分来源是为了让「只有动态 import 指向实现」也能给出准确 verdict
    （DYNAMIC_IMPORT_LITERAL），而不是笼统混进 IMPORTS_IMPLEMENTATION。
    """
    found: list[tuple[str, bool]] = []
    for pattern, is_dynamic in ((_IMPORT_FROM, False), (_REQUIRE, False), (_DYNAMIC_LITERAL, True)):
        found.extend((item, is_dynamic) for item in pattern.findall(source))
    seen: set[str] = set()
    out: list[tuple[str, bool]] = []
    for spec, is_dynamic in found:
        if spec in seen:
            continue
        seen.add(spec)
        out.append((spec, is_dynamic))
    return out


def has_non_literal_dynamic_import(source: str) -> bool:
    """是否存在无法静态解析的动态 import（例如 `await import(name)`）。"""
    return bool(_DYNAMIC_NON_LITERAL.search(source))


@dataclass
class FileAudit:
    path: str
    verdict: str
    declared_types: tuple[str, ...] = ()
    specifiers: tuple[str, ...] = ()
    resolved_implementation: tuple[str, ...] = ()
    detail: str = ""

    @property
    def weak(self) -> bool:
        return is_weak_verdict(self.verdict)

    @property
    def meaningful(self) -> bool:
        """是否构成「引用了实现」的证据。与门禁共用同一判据。"""
        return is_meaningful_import(self.verdict)

    @property
    def exempt(self) -> bool:
        return self.verdict in EXEMPT_VERDICTS


@dataclass
class ImportAudit:
    files: list[FileAudit] = field(default_factory=list)

    @property
    def weak_files(self) -> list[FileAudit]:
        return [f for f in self.files if f.weak]

    @property
    def audited_files(self) -> list[FileAudit]:
        """真正参与判定的文件（排除 helper）。"""
        return [f for f in self.files if not f.exempt or f.verdict == V_E2E]

    @property
    def ok(self) -> bool:
        """所有被审计的文件都通过，且至少有一个真实测试文件。"""
        if not self.files:
            return False
        if self.weak_files:
            return False
        # 全是 helper => 没有真正的测试
        if all(f.verdict == V_HELPER for f in self.files):
            return False
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "files": [
                {
                    "path": f.path,
                    "verdict": f.verdict,
                    "declared_types": list(f.declared_types),
                    "specifiers": list(f.specifiers[:10]),
                    "resolved_implementation": list(f.resolved_implementation[:10]),
                    "detail": f.detail,
                }
                for f in self.files
            ],
            "weak_files": [f.path for f in self.weak_files],
        }

    def describe(self) -> str:
        return describe(self)


def audit_imports(
    output_dir: Path,
    test_paths: list[str],
    *,
    implementation_root: str = "backend/src",
    declared_types: dict[str, tuple[str, ...]] | None = None,
    aliases: dict[str, str] | None = None,
) -> ImportAudit:
    """审计每个测试文件是否引用了实现代码。

    Args:
        test_paths: 相对 output_dir 的测试文件路径（例如 backend/tests/a.test.js）
        implementation_root: 实现代码根目录（相对 output_dir）
        declared_types: 文件路径 -> 声明的测试类型，用于 e2e 豁免
        aliases: 路径别名前缀 -> 相对 output_dir 的目标前缀，例如 {"@/": "backend/src/"}
    """
    declared_types = declared_types or {}
    aliases = aliases or {}
    impl_root = (output_dir / implementation_root).resolve()
    audit = ImportAudit()

    for rel_path in test_paths:
        normalized = rel_path.lstrip("./")
        types = tuple(declared_types.get(normalized, ()))
        record = FileAudit(path=normalized, verdict=V_NO_IMPORT, declared_types=types)
        audit.files.append(record)

        if is_helper_file(normalized):
            record.verdict = V_HELPER
            record.detail = "测试工具文件，不参与 import 审计"
            continue

        path = output_dir / normalized
        if not path.is_file():
            record.verdict = V_MISSING
            record.detail = "文件不存在"
            continue

        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            record.verdict = V_MISSING
            record.detail = f"读取失败: {exc}"
            continue

        # 纯 E2E：显式声明 type: e2e 才允许不 import 实现
        if types and all(t.lower() == "e2e" for t in types):
            record.verdict = V_E2E
            record.detail = "声明为 e2e，豁免 import 要求"
            continue

        specifier_kinds = extract_specifier_kinds(source)
        record.specifiers = tuple(spec for spec, _ in specifier_kinds)
        unresolved_alias: str | None = None
        resolved_static: list[str] = []
        resolved_dynamic: list[str] = []

        for spec, is_dynamic in specifier_kinds:
            if looks_like_alias(spec):
                matched = next((p for p in aliases if spec.startswith(p)), None)
                if matched is None:
                    unresolved_alias = spec
                    continue
                target = (output_dir / aliases[matched] / spec[len(matched):]).resolve()
            elif spec.startswith((".", "/")):
                target = (path.parent / spec).resolve()
            else:
                continue  # 外部依赖（vitest / node:test / express ...）

            if target == impl_root or impl_root in target.parents:
                (resolved_dynamic if is_dynamic else resolved_static).append(spec)

        # 判定顺序：**先看有没有真的指向实现的引用**，再看无法解析的证据。
        # 只要存在可解析的实现引用，文件就是有意义的——无关的动态 import 或
        # 未配置别名都不应把它推翻。
        if resolved_static:
            record.verdict = V_IMPORTS
            record.resolved_implementation = tuple(resolved_static)
            record.detail = f"引用了实现模块: {', '.join(resolved_static[:3])}"
        elif resolved_dynamic:
            record.verdict = V_DYNAMIC_LITERAL
            record.resolved_implementation = tuple(resolved_dynamic)
            record.detail = (
                f"经字面量动态 import 引用实现模块: {', '.join(resolved_dynamic[:3])}"
            )
        elif unresolved_alias is not None:
            record.verdict = V_ALIAS
            record.detail = (
                f"路径别名 {unresolved_alias!r} 无法解析（未配置解析规则），"
                "不能证明它指向实现模块"
            )
        elif has_non_literal_dynamic_import(source):
            record.verdict = V_DYNAMIC_UNRESOLVED
            record.detail = (
                "存在无法静态解析的动态 import（路径拼接/变量传入），"
                "且没有任何可解析的实现引用，无法证明引用了实现，需人工复核"
            )
        elif not record.specifiers:
            record.verdict = V_NO_IMPORT
            record.detail = "源码中找不到任何 import/require"
        else:
            record.verdict = V_NO_IMPORT
            record.detail = (
                "没有任何 import/require 指向 " + implementation_root
                + "（相对路径或别名都没有），断言的对象很可能是测试自己造的数据"
            )

    return audit


def describe(audit: ImportAudit, *, implementation_root: str = "backend/src") -> str:
    """把审计结果转成可执行的诊断文本。"""
    if audit.ok:
        return f"import 审计通过：所有声明内的测试文件都引用了 {implementation_root} 下的实现模块。"

    lines = [f"import 审计未通过（实现根 {implementation_root}）："]
    for item in audit.weak_files:
        lines.append(f"  - {item.path} [{item.verdict}] {item.detail}")
    if audit.files and all(f.verdict == V_HELPER for f in audit.files):
        lines.append("  - 所有文件都是测试工具文件，没有真正的测试用例")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 向后兼容：早期实验脚本使用的接口
# ---------------------------------------------------------------------------


@dataclass
class TestAudit:
    """旧接口的返回结构（供 experiment / reanalyze 脚本继续使用）。"""

    files_scanned: int = 0
    relative_specifiers: list[str] = field(default_factory=list)
    external_specifiers: list[str] = field(default_factory=list)
    imports_implementation: bool = False
    missing_files: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.files_scanned == 0:
            return "NO_TEST_FILES"
        if not self.relative_specifiers:
            return "NO_RELATIVE_IMPORT"
        if not self.imports_implementation:
            return "NO_IMPLEMENTATION_IMPORT"
        return "IMPORTS_IMPLEMENTATION"


def audit_tests(
    output_dir: Path,
    test_paths: list[str],
    *,
    implementation_root: str = "backend/src",
) -> TestAudit:
    """旧接口：只看"有没有引用实现"，不区分 e2e / helper / 别名。"""
    audit = audit_imports(output_dir, test_paths, implementation_root=implementation_root)
    legacy = TestAudit()
    for item in audit.files:
        if item.verdict == V_MISSING:
            legacy.missing_files.append(item.path)
            continue
        legacy.files_scanned += 1
        for spec in item.specifiers:
            if spec.startswith((".", "/")):
                legacy.relative_specifiers.append(f"{item.path} -> {spec}")
            else:
                legacy.external_specifiers.append(spec)
        if item.verdict == V_IMPORTS:
            legacy.imports_implementation = True
    return legacy


# ===========================================================================
# B：依赖使用审计（静态调用检查）
# ===========================================================================
#
# 这是一类**新的空转**，比测试层空转高一层：
#
#   测试层：测试不 import 实现，自造 mock 断言自己   -> RED 门禁 + import 审计已覆盖
#   模块层：下游声明依赖上游，但不调用上游           -> 此前**无任何检测**
#
# `depends_on` 此前只用于拓扑排序与提示词里的一行文本，
# 没有任何机制校验下游是否真的调用了上游。
# 实测：REQ-12 在声明依赖 REQ-3 **完全未实现**的情况下通过了测试——
# 它把依赖做成了参数注入，测试里注入的是假实现。
# 这过了 RED 门禁，也过了 import 审计（它确实 import 了自己的 report.js）。

DEP_USED = "DEPENDENCY_USED"
DEP_NOT_USED = "DEPENDENCY_NOT_USED"
DEP_FAKE = "FAKE_DEPENDENCY"
DEP_UPSTREAM_MISSING = "UPSTREAM_MISSING"
DEP_SKIPPED_INDIRECT = "SKIPPED_INDIRECT"
DEP_UNCERTAIN = "DEPENDENCY_CHECK_UNCERTAIN"

# 判定为违规（阻断）的
DEP_VIOLATIONS = frozenset({DEP_NOT_USED, DEP_FAKE, DEP_UPSTREAM_MISSING})
# 记录下来、人工复核，但**不阻断**
DEP_UNCERTAIN_VERDICTS = frozenset({DEP_SKIPPED_INDIRECT, DEP_UNCERTAIN})

_EXPORT_OBJECT = re.compile(r"module\.exports\s*=\s*\{([^}]*)\}", re.S)
_EXPORT_ASSIGN = re.compile(r"(?:module\.)?exports\.([A-Za-z_$][\w$]*)\s*=")
_ESM_EXPORT_FN = re.compile(r"export\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")
_ESM_EXPORT_CONST = re.compile(r"export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)")
_ESM_EXPORT_LIST = re.compile(r"export\s*\{([^}]*)\}")
_REEXPORT = re.compile(r"module\.exports\s*=\s*require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")

_DESTRUCTURED_CJS = re.compile(r"(?:const|let|var)\s*\{([^}]*)\}\s*=\s*require\s*\(")
_NAMESPACE_CJS = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(")
_DESTRUCTURED_ESM = re.compile(r"import\s*\{([^}]*)\}\s*from")
_NAMESPACE_ESM = re.compile(r"import\s*\*\s*as\s+([A-Za-z_$][\w$]*)")
_DEFAULT_ESM = re.compile(r"import\s+([A-Za-z_$][\w$]*)\s*(?:,|from)")


def extract_exports(source: str) -> set[str]:
    """抽取一个实现模块导出的符号名。"""
    names: set[str] = set()
    for match in _EXPORT_OBJECT.finditer(source):
        for part in match.group(1).split(","):
            part = part.strip()
            if ":" in part:
                part = part.split(":")[0].strip()
            name = part.replace("...", "").strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                names.add(name)
    names |= set(_EXPORT_ASSIGN.findall(source))
    names |= set(_ESM_EXPORT_FN.findall(source))
    names |= set(_ESM_EXPORT_CONST.findall(source))
    for match in _ESM_EXPORT_LIST.finditer(source):
        for part in match.group(1).split(","):
            name = part.strip().split(" as ")[-1].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                names.add(name)
    return names


def _resolve_specifier(specifier: str, from_file: Path, output_dir: Path) -> Path | None:
    """把 import 说明符解析为绝对路径（复用 audit_imports 的相对路径语义）。"""
    if not specifier.startswith((".", "/")):
        return None
    return (from_file.parent / specifier).resolve()


def _same_file(candidate: Path, target: Path) -> bool:
    for suffix in ("", ".js", ".ts", ".mjs", ".cjs", "/index.js"):
        if Path(str(candidate) + suffix).resolve() == target.resolve():
            return True
    return False


def _bindings_in_line(line: str) -> tuple[list[str], list[str]]:
    """返回该行引入的 (解构名, 命名空间名)。"""
    destructured: list[str] = []
    namespaces: list[str] = []
    for pattern in (_DESTRUCTURED_CJS, _DESTRUCTURED_ESM):
        for match in pattern.finditer(line):
            for part in match.group(1).split(","):
                name = part.strip().split(":")[-1].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                    destructured.append(name)
    for pattern in (_NAMESPACE_CJS, _NAMESPACE_ESM, _DEFAULT_ESM):
        for match in pattern.finditer(line):
            name = match.group(1)
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                namespaces.append(name)
    return destructured, namespaces


@dataclass
class DependencyUsage:
    """一个「下游 -> 上游」声明依赖的使用情况。"""

    downstream: str
    upstream: str
    verdict: str
    detail: str = ""
    upstream_exports: tuple[str, ...] = ()
    referenced_symbols: tuple[str, ...] = ()
    called_symbols: tuple[str, ...] = ()
    unknown_members: tuple[str, ...] = ()
    upstream_files: tuple[str, ...] = ()
    indirect_reason: str = ""

    @property
    def violation(self) -> bool:
        return self.verdict in DEP_VIOLATIONS

    @property
    def uncertain(self) -> bool:
        return self.verdict in DEP_UNCERTAIN_VERDICTS

    def to_dict(self) -> dict[str, object]:
        return {
            "downstream": self.downstream,
            "upstream": self.upstream,
            "verdict": self.verdict,
            "detail": self.detail,
            "upstream_exports": list(self.upstream_exports[:20]),
            "referenced_symbols": list(self.referenced_symbols[:20]),
            "called_symbols": list(self.called_symbols[:20]),
            "unknown_members": list(self.unknown_members[:20]),
            "upstream_files": list(self.upstream_files[:10]),
            "indirect_reason": self.indirect_reason,
        }


@dataclass
class DependencyAudit:
    usages: list[DependencyUsage] = field(default_factory=list)

    @property
    def violations(self) -> list[DependencyUsage]:
        return [u for u in self.usages if u.violation]

    @property
    def uncertain(self) -> list[DependencyUsage]:
        return [u for u in self.usages if u.uncertain]

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "usages": [u.to_dict() for u in self.usages],
            "violations": [u.to_dict() for u in self.violations],
            "uncertain": [u.to_dict() for u in self.uncertain],
        }

    def describe(self) -> str:
        return describe_dependencies(self)


def audit_dependency_usage(
    output_dir: Path,
    *,
    downstream: str,
    upstream: str,
    downstream_files: Sequence[str],
    upstream_files: Sequence[str],
    indirect_reason: str = "",
) -> DependencyUsage:
    """检查下游实现是否真的调用了上游模块导出的函数/方法。

    Args:
        downstream_files: 下游需求产出的实现文件（output_dir 相对）
        upstream_files:   上游需求产出的实现文件（output_dir 相对）
        indirect_reason:  非空表示已在测试计划里显式声明为间接依赖 -> 跳过判定
    """
    if indirect_reason:
        return DependencyUsage(
            downstream=downstream, upstream=upstream, verdict=DEP_SKIPPED_INDIRECT,
            detail=f"已在测试计划里声明为间接依赖，跳过使用校验。理由：{indirect_reason}",
            indirect_reason=indirect_reason,
        )

    if not upstream_files:
        return DependencyUsage(
            downstream=downstream, upstream=upstream, verdict=DEP_UPSTREAM_MISSING,
            detail=(
                f"声明的上游 {upstream} 没有产出任何实现文件 —— "
                "下游无法真实使用该依赖（上游可能失败或未执行）"
            ),
            upstream_files=tuple(upstream_files),
        )

    # 上游导出清单（含一层 re-export 跟随，例如 movement.js -> movementsService.js）
    exports: set[str] = set()
    upstream_paths = [(output_dir / p).resolve() for p in upstream_files]
    for rel in upstream_files:
        path = output_dir / rel
        if not path.is_file():
            continue
        src = path.read_text(encoding="utf-8")
        found = extract_exports(src)
        if not found:
            reexport = _REEXPORT.search(src)
            if reexport:
                target = _resolve_specifier(reexport.group(1), path, output_dir)
                if target is not None:
                    for suffix in (".js", ".ts", ""):
                        candidate = Path(str(target) + suffix)
                        if candidate.is_file():
                            found = extract_exports(candidate.read_text(encoding="utf-8"))
                            break
        exports |= found

    if not exports:
        return DependencyUsage(
            downstream=downstream, upstream=upstream, verdict=DEP_UNCERTAIN,
            detail=f"上游 {upstream} 的实现文件里解析不出任何导出符号，无法判定（记录人工复核）",
        )

    # 下游是否 import 了上游、以及引用了哪些导出
    imported = False
    referenced: set[str] = set()
    called: set[str] = set()
    unknown: set[str] = set()

    for rel in downstream_files:
        path = output_dir / rel
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for index, line in enumerate(lines):
            specifiers = extract_specifiers(line)
            if not any(
                (resolved := _resolve_specifier(s, path, output_dir)) is not None
                and any(_same_file(resolved, up) for up in upstream_paths)
                for s in specifiers
            ):
                continue
            imported = True
            destructured, namespaces = _bindings_in_line(line)
            rest = "\n".join(lines[:index] + lines[index + 1:])
            for name in destructured:
                if re.search(rf"\b{re.escape(name)}\b", rest):
                    referenced.add(name)
                if re.search(rf"\b{re.escape(name)}\s*\(", rest):
                    called.add(name)
            for ns in namespaces:
                for member in set(re.findall(rf"\b{re.escape(ns)}\.([A-Za-z_$][\w$]*)", rest)):
                    if member in exports:
                        referenced.add(member)
                        if re.search(rf"\b{re.escape(ns)}\.{re.escape(member)}\s*\(", rest):
                            called.add(member)
                    else:
                        unknown.add(f"{ns}.{member}")

    if referenced:
        return DependencyUsage(
            downstream=downstream, upstream=upstream, verdict=DEP_USED,
            detail=f"真实引用了上游导出: {', '.join(sorted(referenced)[:5])}"
                   + (f"（其中被调用: {', '.join(sorted(called)[:5])}）" if called else ""),
            upstream_exports=tuple(sorted(exports)), referenced_symbols=tuple(sorted(referenced)),
            called_symbols=tuple(sorted(called)), unknown_members=tuple(sorted(unknown)),
            upstream_files=tuple(upstream_files),
        )

    if unknown:
        return DependencyUsage(
            downstream=downstream, upstream=upstream, verdict=DEP_FAKE,
            detail=(
                f"访问了上游模块，但成员不在其导出清单里: {', '.join(sorted(unknown)[:5])}；"
                f"上游实际导出: {', '.join(sorted(exports)[:5])}"
            ),
            upstream_exports=tuple(sorted(exports)), unknown_members=tuple(sorted(unknown)),
            upstream_files=tuple(upstream_files),
        )

    return DependencyUsage(
        downstream=downstream, upstream=upstream, verdict=DEP_NOT_USED,
        detail=(
            f"下游{'只 import 了上游但从未引用其导出' if imported else '完全没有 import 上游模块'}；"
            f"上游导出: {', '.join(sorted(exports)[:5])}"
        ),
        upstream_exports=tuple(sorted(exports)),
        upstream_files=tuple(upstream_files),
    )


def audit_requirement_dependencies(
    output_dir: Path,
    *,
    downstream: str,
    declared_dependencies: Sequence[str],
    downstream_files: Sequence[str],
    produced_files: dict[str, Sequence[str]],
    indirect_dependencies: dict[str, str] | None = None,
) -> DependencyAudit:
    """对一个需求的全部声明依赖做使用审计。

    Args:
        produced_files: 每个需求已产出的实现文件（用于查上游导出清单）
        indirect_dependencies: 测试计划里显式声明的间接依赖 -> 理由
    """
    indirect = indirect_dependencies or {}
    audit = DependencyAudit()
    for upstream in declared_dependencies:
        audit.usages.append(
            audit_dependency_usage(
                output_dir,
                downstream=downstream,
                upstream=upstream,
                downstream_files=downstream_files,
                upstream_files=list(produced_files.get(upstream, [])),
                indirect_reason=indirect.get(upstream, ""),
            )
        )
    return audit


def describe_dependencies(audit: DependencyAudit) -> str:
    """把依赖审计结果转成给模型看的、可执行的拒绝理由。"""
    if audit.ok:
        used = [u for u in audit.usages if u.verdict == DEP_USED]
        return (
            "依赖使用审计通过："
            + ("；".join(f"{u.downstream} -> {u.upstream} 已真实调用" for u in used) or "无声明依赖")
        )

    lines = ["依赖使用审计未通过："]
    for item in audit.violations:
        lines.append(f"  - [{item.verdict}] {item.downstream} 依赖 {item.upstream}：{item.detail}")
        if item.upstream_files:
            lines.append(f"    上游实现文件（请 require 这些路径）: {', '.join(item.upstream_files[:5])}")
        if item.upstream_exports:
            lines.append(f"    上游导出清单（请调用其中之一）: {', '.join(item.upstream_exports[:10])}")
    lines.append(
        "要求：下游实现必须**真实调用**上游模块导出的函数/方法"
        "（import 那个模块并使用它的导出），不能只声明依赖、也不能用参数注入绕过。"
    )
    lines.append(
        "如果你确实只能间接使用上游（例如经第三方转发、或由调用方注入），"
        "请在测试计划里为该上游声明 `indirect_dependencies` 并写明理由。"
    )
    return "\n".join(lines)


# ===========================================================================
# 方案 2-A：测试层 mock 上游检测
# ===========================================================================
#
# 方案 1（静态调用检查）能消除「完全不 import 上游」这一最粗形态，
# 但消除不了「运行时仍走 mock」—— 实测中模型加了名义 require 之后，
# 运行时仍优先使用注入进来的假回调。
#
# 本检查针对**测试层**：测试文件若 mock 了声明依赖的上游模块，且未在
# 测试计划里显式声明，则该依赖在运行时从未被真实验证 -> UNVERIFIED_DEPENDENCY。

UNVERIFIED_DEPENDENCY = "UNVERIFIED_DEPENDENCY"
MOCK_DECLARED = "MOCK_DECLARED_OK"

# 常见 mock API：vi.mock / jest.mock / mock.module（node:test）/ doMock / setMock
_MOCK_PATTERNS = (
    re.compile(r"""\bvi\.(?:do)?[mM]ock\s*\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\bjest\.(?:do)?[mM]ock\s*\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\bmock\.module\s*\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\bjest\.setMock\s*\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\bsinon\.stub\s*\(\s*[^,]+,\s*['"]([^'"]+)['"]"""),
)


def extract_mock_targets(source: str) -> list[str]:
    """抽取测试文件里所有被 mock 的模块路径。"""
    found: list[str] = []
    for pattern in _MOCK_PATTERNS:
        found.extend(pattern.findall(source))
    seen: set[str] = set()
    out: list[str] = []
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


@dataclass
class MockedDependency:
    downstream: str
    upstream: str
    mocked_specifier: str
    mocked_file: str
    verdict: str
    declared_reason: str = ""
    detail: str = ""
    upstream_exports: tuple[str, ...] = ()

    @property
    def violation(self) -> bool:
        return self.verdict == UNVERIFIED_DEPENDENCY

    def to_dict(self) -> dict[str, object]:
        return {
            "downstream": self.downstream, "upstream": self.upstream,
            "mocked_specifier": self.mocked_specifier, "mocked_file": self.mocked_file,
            "verdict": self.verdict, "declared_reason": self.declared_reason,
            "detail": self.detail, "upstream_exports": list(self.upstream_exports[:20]),
        }


@dataclass
class MockAudit:
    usages: list[MockedDependency] = field(default_factory=list)

    @property
    def violations(self) -> list[MockedDependency]:
        return [u for u in self.usages if u.violation]

    @property
    def declared(self) -> list[MockedDependency]:
        return [u for u in self.usages if u.verdict == MOCK_DECLARED]

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "usages": [u.to_dict() for u in self.usages],
                "violations": [u.to_dict() for u in self.violations]}

    def describe(self) -> str:
        return describe_mocked_dependencies(self)


def audit_mocked_dependencies(
    output_dir: Path,
    *,
    downstream: str,
    declared_dependencies: Sequence[str],
    test_files: Sequence[str],
    produced_files: dict[str, Sequence[str]],
    declared_mocks: dict[str, str] | None = None,
) -> MockAudit:
    """检查测试文件是否 mock 了声明依赖的上游，且未在测试计划里声明。

    Args:
        test_files: 该需求的测试文件（output_dir 相对）
        produced_files: 每个上游需求产出的实现文件
        declared_mocks: 测试计划里显式允许 mock 的上游（路径或 req_id）-> 理由
    """
    declared_mocks = declared_mocks or {}
    audit = MockAudit()

    upstream_files: dict[str, str] = {}
    for req_id in declared_dependencies:
        for rel in produced_files.get(req_id, []):
            upstream_files[str((output_dir / rel).resolve())] = req_id
    if not upstream_files:
        return audit

    for rel in test_files:
        path = output_dir / rel
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        for specifier in extract_mock_targets(source):
            resolved = _resolve_specifier(specifier, path, output_dir)
            if resolved is None:
                continue
            matched_req = next(
                (req for target, req in upstream_files.items()
                 if _same_file(resolved, Path(target))),
                None,
            )
            if matched_req is None:
                continue  # mock 的不是声明依赖的上游 -> 与本检查无关

            matched_file = next(
                (rel for rel in produced_files.get(matched_req, [])
                 if _same_file(resolved, (output_dir / rel).resolve())), specifier
            )
            reason = declared_mocks.get(matched_req) or declared_mocks.get(matched_file) or ""
            exports = extract_exports((output_dir / matched_file).read_text(encoding="utf-8")) \
                if (output_dir / matched_file).is_file() else set()

            if reason:
                audit.usages.append(MockedDependency(
                    downstream=downstream, upstream=matched_req,
                    mocked_specifier=specifier, mocked_file=matched_file,
                    verdict=MOCK_DECLARED, declared_reason=reason,
                    detail=f"已在测试计划里声明允许 mock。理由：{reason}",
                    upstream_exports=tuple(sorted(exports)),
                ))
            else:
                audit.usages.append(MockedDependency(
                    downstream=downstream, upstream=matched_req,
                    mocked_specifier=specifier, mocked_file=matched_file,
                    verdict=UNVERIFIED_DEPENDENCY,
                    detail=(
                        f"测试文件 mock 了声明依赖的上游模块 {matched_file}，"
                        "该依赖在运行时从未被真实验证"
                    ),
                    upstream_exports=tuple(sorted(exports)),
                ))
    return audit


def describe_mocked_dependencies(audit: MockAudit) -> str:
    """拒绝理由：必须含（1）违规码（2）被 mock 的上游路径（3）上游导出清单（4）修复指引与合法出口。"""
    if audit.ok:
        return "测试层 mock 检查通过：没有未声明的上游 mock。"
    lines = ["测试层 mock 检查未通过："]
    for item in audit.violations:
        lines.append(f"  - [{item.verdict}] {item.downstream} 依赖 {item.upstream}：{item.detail}")
        lines.append(f"    被 mock 的模块路径: {item.mocked_file}")
        lines.append(f"    mock 说明符: {item.mocked_specifier}")
        if item.upstream_exports:
            lines.append(f"    上游导出清单（请改为真实调用）: {', '.join(item.upstream_exports[:10])}")
    lines.append(
        "请改为**真实调用上游模块**：删掉 mock，直接 require 上游并断言它的真实行为。"
    )
    lines.append(
        "如果你确实必须 mock（例如上游依赖外部服务），请在测试计划里为该上游声明 "
        "`mocked_dependencies` 并写明理由。"
    )
    return "\n".join(lines)


# ===========================================================================
# 方案 2-B：实现层注入旁路检测（第一轮为警告，不阻断）
# ===========================================================================
#
# 实测逃逸形态（B+feedback 组的真实产物）：
#   const { summarizeItems: upstreamSummarizeItems } = require('./summary');   // 名义调用
#   function buildReport(items, movements, summarizeItems) {                  // 仍保留形参
#     if (typeof summarizeItems === 'function') { ... }                       // 仍优先用注入
# 静态调用检查看到 require 就放行，但运行时走的仍是注入进来的假实现。
#
# 检测：函数形参 + `typeof 形参 === 'function'` 守卫，且该参数与上游导出同名/同用途
#       -> DEPENDENCY_INJECTION_BYPASS（第一轮只警告）

INJECTION_BYPASS = "DEPENDENCY_INJECTION_BYPASS"

_FUNCTION_PARAMS = re.compile(
    r"(?:function\s+[A-Za-z_$][\w$]*\s*\(([^)]*)\))"      # function name(a, b)
    r"|(?:\(([^)]*)\)\s*=>)"                              # (a, b) =>
)
_TYPEOF_FUNCTION = re.compile(r"typeof\s+([A-Za-z_$][\w$]*)\s*===?\s*['\"]function['\"]")


@dataclass
class InjectionBypass:
    downstream: str
    upstream: str
    parameter: str
    file: str
    detail: str = ""
    upstream_exports: tuple[str, ...] = ()
    # 从下游文件到上游文件的**相对 require 路径**（如 './summary'）。
    # 不给这个，模型会自己猜路径——实测它写了 require('./items')，模块不存在，
    # 重写因此在语义正确的情况下依然把测试改坏。
    require_path: str = ""
    upstream_file: str = ""

    @property
    def verdict(self) -> str:
        return INJECTION_BYPASS

    def to_dict(self) -> dict[str, object]:
        return {"downstream": self.downstream, "upstream": self.upstream,
                "parameter": self.parameter, "file": self.file, "verdict": self.verdict,
                "detail": self.detail, "upstream_exports": list(self.upstream_exports[:20]),
                "require_path": self.require_path, "upstream_file": self.upstream_file}


@dataclass
class InjectionAudit:
    findings: list[InjectionBypass] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "findings": [f.to_dict() for f in self.findings]}

    def describe(self) -> str:
        return describe_injection_bypass(self)


def audit_injection_bypass(
    output_dir: Path,
    *,
    downstream: str,
    declared_dependencies: Sequence[str],
    downstream_files: Sequence[str],
    produced_files: dict[str, Sequence[str]],
) -> InjectionAudit:
    """检测「实现层参数注入旁路」：形参 + typeof 守卫 + 优先使用注入值。"""
    audit = InjectionAudit()
    upstream_exports: dict[str, set[str]] = {}
    for req_id in declared_dependencies:
        names: set[str] = set()
        for rel in produced_files.get(req_id, []):
            path = output_dir / rel
            if path.is_file():
                names |= extract_exports(path.read_text(encoding="utf-8"))
        upstream_exports[req_id] = names
    if not any(upstream_exports.values()):
        return audit

    for rel in downstream_files:
        path = output_dir / rel
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        params: set[str] = set()
        for match in _FUNCTION_PARAMS.finditer(source):
            raw = match.group(1) or match.group(2) or ""
            for part in raw.split(","):
                name = part.split("=")[0].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                    params.add(name)
        if not params:
            continue
        guarded = set(_TYPEOF_FUNCTION.findall(source))
        for param in sorted(params & guarded):
            # 该形参与某个上游的导出同名 -> 明确是旁路；否则记为不确定用途
            for req_id, names in upstream_exports.items():
                if not names:
                    continue
                # 找出上游实现文件，并算出从本文件过去的相对 require 路径
                up_file = next(
                    (rel for rel in produced_files.get(req_id, []) if (output_dir / rel).is_file()),
                    "",
                )
                require_path = ""
                if up_file:
                    try:
                        rel_target = os.path.relpath(
                            (output_dir / up_file).resolve(), path.parent.resolve())
                        require_path = rel_target if rel_target.startswith(".") else f"./{rel_target}"
                        require_path = re.sub(r"\.(js|ts|mjs|cjs)$", "", require_path)
                    except ValueError:
                        require_path = ""
                audit.findings.append(InjectionBypass(
                    downstream=downstream, upstream=req_id, parameter=param, file=rel,
                    detail=(
                        f"函数形参 `{param}` 被 `typeof {param} === 'function'` 守卫，"
                        "实现会优先使用注入进来的值；即使同时 import 了上游，"
                        "运行时仍可能走注入路径"
                        + ("（与上游导出同名，旁路嫌疑明确）" if param in names else "")
                    ),
                    upstream_exports=tuple(sorted(names)),
                    require_path=require_path,
                    upstream_file=up_file,
                ))
                break
    return audit


def describe_injection_bypass(audit: InjectionAudit) -> str:
    if audit.ok:
        return "实现层注入旁路检查通过：未发现形参守卫式的依赖旁路。"
    lines = ["实现层注入旁路检查（**警告级**，本轮不阻断）："]
    for item in audit.findings:
        lines.append(
            f"  - [{item.verdict}] {item.downstream} -> {item.upstream}"
            f"（文件 {item.file}，形参 `{item.parameter}`）"
        )
        lines.append(f"    {item.detail}")
        if item.require_path:
            lines.append(
                f"    上游实现文件: {item.upstream_file}"
                f"（在本文件里应当写 require('{item.require_path}')）"
            )
        if item.upstream_exports:
            lines.append(f"    上游导出清单: {', '.join(item.upstream_exports[:10])}")
    lines.append(
        "修改要求（三条都必须满足）：\n"
        "  1. 直接 require 上游模块并使用它的导出，**不要**保留「调用方注入优先」的旁路；\n"
        "  2. require 的路径必须用上面给出的值，不要自己推断文件名；\n"
        "  3. **不得改变**函数签名与返回结构——现有测试依赖它们，改了会把测试改坏。"
    )
    return "\n".join(lines)
