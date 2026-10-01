"""UI 门禁（P0-3）：**第五道门**，与依赖 / mock / 注入旁路 / 合同并列。

═══════════════════════════════════════════════════════════════════════════
 为什么 UI 需要单独一道门
═══════════════════════════════════════════════════════════════════════════
四道既有门禁全部面向**后端逻辑**：

    依赖使用门禁   下游有没有真实调用上游模块
    mock 门禁      测试有没有 mock 未声明的上游
    注入旁路门禁   实现有没有用形参守卫绕过上游
    合同门禁       跨模块调用签名是否符合契约

**UI 无门禁** —— 于是即使契约声明了页面上必须有什么，
也没有任何东西检查「测试是否真的覆盖了这些元素」。

实测根因（本需求的由来）：UI 不是「生成失败」，而是
**从未真正进入生成路径** —— 系统把它当后端 API 处理，
UI 部分被模型自由发挥，发挥不好就失败。

本模块把 UI 从「自由发挥」纳入**可判定**：声明了 `ui_contracts`
就要有 E2E 测试、且测试必须覆盖到每个元素与每条错误消息。

═══════════════════════════════════════════════════════════════════════════
 四个判定
═══════════════════════════════════════════════════════════════════════════
    UI_TEST_MISSING           未生成 E2E 测试（计划里没有 type=e2e，或文件不存在）
    UI_TEST_WEAK              E2E 测试在 RED 阶段**没有失败** —— 它测不出东西
    UI_ELEMENT_MISSING        需求声明的 UI 元素未出现在测试里
    UI_ERROR_MESSAGE_MISMATCH 需求声明的错误消息文本未出现在测试里

**可选语义**：未声明 `ui_contracts` 的需求不做任何检查（返回 ok）——
与既有四道门的「字段可选」约定一致。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .models import Requirement

logger = logging.getLogger("factory.uigate")

# ---- 判定名 ----
UI_TEST_MISSING = "UI_TEST_MISSING"
UI_TEST_WEAK = "UI_TEST_WEAK"
UI_ELEMENT_MISSING = "UI_ELEMENT_MISSING"
UI_ERROR_MESSAGE_MISMATCH = "UI_ERROR_MESSAGE_MISMATCH"

UI_VIOLATIONS = frozenset({UI_TEST_MISSING, UI_TEST_WEAK,
                           UI_ELEMENT_MISSING, UI_ERROR_MESSAGE_MISMATCH})


@dataclass(frozen=True)
class UIViolation:
    verdict: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "detail": self.detail}


@dataclass
class UICheck:
    """单个需求的 UI 门禁结论。"""

    req_id: str
    ok: bool
    reason: str = ""
    violations: list[UIViolation] = field(default_factory=list)
    #: 元素覆盖映射：element_id -> 是否在测试里被引用
    element_coverage: dict[str, bool] = field(default_factory=dict)
    #: 错误消息覆盖映射：消息文本 -> 是否在测试里出现
    message_coverage: dict[str, bool] = field(default_factory=dict)
    e2e_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "req_id": self.req_id, "ok": self.ok, "reason": self.reason,
            "violations": [v.to_dict() for v in self.violations],
            "element_coverage": dict(self.element_coverage),
            "message_coverage": dict(self.message_coverage),
            "e2e_files": list(self.e2e_files),
        }


# ---------------------------------------------------------------------------
# 覆盖判定
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


#: UI 元素 type -> Playwright 角色。占位符型 label 靠角色兜底匹配。
_TYPE_TO_ROLE: dict[str, tuple[str, ...]] = {
    "link": ("link",), "button": ("button",), "submit": ("button",),
    "checkbox": ("checkbox",), "radio": ("radio",), "textbox": ("textbox",),
    # `text` 是宽泛类型：既可能是 textbox，也可能是 heading/paragraph 里的文本。
    # 都列上 —— 漏列会让占位符型 label 的元素**永远无法被判定为已覆盖**。
    "text": ("textbox", "text", "heading", "paragraph"),
    "password": ("textbox",),
    "list": ("list",), "tabs": ("tab",), "tab": ("tab",),
    "grid": ("grid",), "table": ("table", "grid"),
}


def _label_core(label: str) -> str:
    """label 去掉 `<占位符>` 后的可字面匹配部分。全占位符则返回空串。"""
    core = re.sub(r"<[^>]*>", " ", _norm(label))
    return re.sub(r"\s+", " ", core).strip()


def element_mentioned(source_norm: str, element_id: str, label: str,
                      element_type: str = "") -> bool:
    """测试源码里是否提到了这个元素。

    判定按**由强到弱**，命中任一即算覆盖：
      ① 元素 id 原样出现（`last-updated`）
      ② id 的连字符/下划线/驼峰变体（`lastUpdated` / `last_updated`）
      ③ 可访问名 label 的**可字面部分**（`getByLabel('Last updated:')`）
      ④ **占位符型 label**（如 `<workbook name>`，可访问名在运行时才确定）
         回退到按**角色**匹配（`getByRole('link'` / `getByRole('button'`）

    多级都试的理由：Playwright 常用 `getByLabel` 走**可访问名**而不是 id；
    而可访问名可能是**动态的**（`<workbook name>`），测试不可能硬编码它。
    只认字面 label 会误报 —— **误报会让人关掉门禁，那比没有门禁更糟**。
    """
    eid = str(element_id or "").strip()
    if not eid:
        return False
    candidates = {_norm(eid)}
    candidates.add(_norm(eid.replace("-", " ")))
    candidates.add(_norm(eid.replace("-", "")))
    candidates.add(_norm(re.sub(r"[-_]([a-z0-9])", lambda m: m.group(1).upper(), eid)))
    candidates.add(_norm(eid.replace("-", "_")))
    if any(c and c in source_norm for c in candidates):
        return True
    # ②b `getByTestId('a-b')` / `data-testid="a-b"` 这类写法已被 ① 覆盖
    #     （id 字符串原样出现），无需另判 —— 留此注释避免后人误加重复分支

    core = _label_core(label)
    if core and len(core) >= 4 and core in source_norm:
        return True

    # ④ 占位符型 label：按角色匹配
    if not core:
        role_hit = any(
            f"getbyrole('{r}'" in source_norm or f'getbyrole("{r}"' in source_norm
            for r in _TYPE_TO_ROLE.get(_norm(element_type), ())
        )
        if role_hit:
            return True
    return False


def message_mentioned(source_norm: str, message: str) -> bool:
    """错误消息文本是否出现在测试里（**逐字**比对）。"""
    msg = _norm(message)
    if not msg:
        return True
    return msg in source_norm


# ---------------------------------------------------------------------------
# 主检查
# ---------------------------------------------------------------------------


def check_ui(
    requirement: Requirement,
    *,
    e2e_sources: Sequence[tuple[str, str]] = (),
    planned_e2e: bool = True,
    red_failed: bool | None = None,
) -> UICheck:
    """检查 UI 契约的测试覆盖。

    参数
    ----
    e2e_sources : [(相对路径, 源码)] —— 计划里 type=e2e 且**实际存在**的测试文件
    planned_e2e : 测试计划里是否声明了 type=e2e 的文件
    red_failed  : E2E 在 RED 阶段是否失败。None 表示未观测（不做 WEAK 判定）

    未声明 `ui_contracts` -> 直接 ok（可选能力的默认关闭语义）。
    """
    decls = requirement.ui_contracts
    if not decls:
        return UICheck(req_id=requirement.req_id, ok=True, reason="NO_UI_CONTRACT_DECLARED")

    violations: list[UIViolation] = []
    files = tuple(p for p, _ in e2e_sources)

    # ① 有没有 E2E 测试
    if not planned_e2e or not e2e_sources:
        violations.append(UIViolation(
            UI_TEST_MISSING,
            "需求声明了 ui_contracts，但测试计划中没有 type=e2e 的测试文件"
            if not planned_e2e else
            "计划声明了 type=e2e，但对应测试文件不存在或为空",
        ))
        return UICheck(req_id=requirement.req_id, ok=False, reason=UI_TEST_MISSING,
                       violations=violations, e2e_files=files)

    # ② RED 阶段是否失败（测不出东西的测试不算覆盖）
    if red_failed is False:
        violations.append(UIViolation(
            UI_TEST_WEAK,
            "E2E 测试在 RED 阶段**没有失败** —— 应用尚未实现却能通过，"
            "说明它没有真正断言任何 UI 行为（空断言 / 只 import / 恒真）",
        ))

    joined = _norm("\n".join(src for _, src in e2e_sources))

    # ③ 元素覆盖
    element_coverage: dict[str, bool] = {}
    missing_elements: list[str] = []
    for contract in decls:
        for el in contract.elements:
            hit = element_mentioned(joined, el.element_id, el.label, el.type)
            element_coverage[el.element_id] = hit
            if not hit:
                missing_elements.append(el.element_id)
    if missing_elements:
        violations.append(UIViolation(
            UI_ELEMENT_MISSING,
            "以下 ui_contracts 元素未出现在 E2E 测试中: " + ", ".join(sorted(set(missing_elements))),
        ))

    # ④ 错误消息覆盖
    message_coverage: dict[str, bool] = {}
    missing_messages: list[str] = []
    for contract in decls:
        for el in contract.elements:
            for _key, msg in el.error_messages:
                if not str(msg).strip():
                    continue
                hit = message_mentioned(joined, msg)
                message_coverage[str(msg)] = hit
                if not hit:
                    missing_messages.append(str(msg))
    if missing_messages:
        violations.append(UIViolation(
            UI_ERROR_MESSAGE_MISMATCH,
            "以下需求声明的错误消息文本未出现在 E2E 测试中（应逐字断言）: "
            + " | ".join(sorted(set(missing_messages))),
        ))

    ok = not violations
    return UICheck(
        req_id=requirement.req_id, ok=ok,
        reason="UI_COVERAGE_OK" if ok else violations[0].verdict,
        violations=violations,
        element_coverage=element_coverage,
        message_coverage=message_coverage,
        e2e_files=files,
    )


def e2e_sources_of(output_dir: Path, plan: Any) -> tuple[list[tuple[str, str]], bool]:
    """从测试计划里取出 type=e2e 的文件及其源码。

    返回 (sources, planned_e2e)。
    """
    if plan is None:
        return [], False
    specs = list(getattr(plan, "test_files", ()) or ())
    e2e_specs = [s for s in specs if str(getattr(s, "type", "")).lower() == "e2e"]
    sources: list[tuple[str, str]] = []
    for spec in e2e_specs:
        rel = str(getattr(spec, "path", "") or "")
        if not rel:
            continue
        path = Path(output_dir) / rel
        try:
            if path.is_file() and path.stat().st_size > 0:
                sources.append((rel, path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return sources, bool(e2e_specs)


def describe(checks: Sequence[UICheck]) -> str:
    """把 UI 门禁违规渲染成**可执行**的理由。"""
    bad = [c for c in checks if not c.ok]
    if not bad:
        return ""
    lines = ["UI 契约未被测试覆盖（需求声明了 ui_contracts）:"]
    for c in bad:
        for v in c.violations:
            lines.append(f"  - [{v.verdict}] {c.req_id}: {v.detail}")
    lines.append("  **修正指令**：让 E2E 测试覆盖 ui_contracts 里的每个元素与每条错误消息。")
    lines.append("    ① 每个元素至少被引用一次（推荐 `getByLabel('<可访问名>')` 或 `getByRole`）；")
    lines.append("    ② 每条 error_messages 的文本必须**逐字**出现在断言里；")
    lines.append("    ③ 不变量（含「不得出现」这类否定式）要有对应断言；")
    lines.append("    ④ E2E 在 RED 阶段应失败 —— 不要写空断言让它提前通过。")
    return "\n".join(lines)
