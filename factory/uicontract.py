"""从需求**正文**提取 UI 契约（粒度 A：元素清单 + 可访问名）。

═══════════════════════════════════════════════════════════════════════════
 为什么需要它（实测，不是推测）
═══════════════════════════════════════════════════════════════════════════
第五道 UI 门禁完全由 `ui_contracts` 字段驱动，但**平台从不发这个字段**：

    $ grep -rl "ui_contracts" arc-bench/webapp/*/requirements/
    （空 —— 6 个真实 app，0 命中）

真实需求的字段只有：
    id / name / type(FOLDER|ATOMIC) / description / dependencies / children / scenarios

于是真实输入永远走 `check_ui()` 的第一行：

    if not decls:
        return UICheck(ok=True, reason="NO_UI_CONTRACT_DECLARED")

**这道门在真实平台上从未活过** —— 之前所有 UI 验证都基于我们自己手写的合成
规格（`requirements_probe_ui`），验证的对象不是平台真实格式。

真实 UI 契约藏在两处：
  ① 描述正文，例如 REQ-1.1:
     "The page contains exactly one visible heading with accessible name `BookStack`."
  ② 隐藏的基准测试（arc-bench/webapp/*/tests/REQ-*.spec.ts，读不到）

本模块解决 ①：把契约来源从「字段」改为「正文提取」。
字段仍在时**优先用字段**（作为显式覆盖），行为不变。

═══════════════════════════════════════════════════════════════════════════
 粒度 A：只提取「元素 + 可访问名」
═══════════════════════════════════════════════════════════════════════════
粒度 B（+不变量）与 C（+交互流程）依赖更强的语义理解，稳定性差，本轮不做。

提取规则来自**真实语料统计**（6 个 app 的 requirements.yaml）：
    "with a visible heading X"        4 次
    "displays a visible heading X"    3 次
    "with the placeholder X"          多次
    "field labeled X" / "input fields labeled X"
    "`Save Page` button is visible" / "the \"History orders\" tab"

对齐依据：平台基准测试的定位方式统计（全部 app 的 helpers.ts）
    getByRole 180 次 / getByLabel 32 / getByText 17 / getByPlaceholder 17
→ **可访问名（role + name）是绝对主导的契约**，正是本模块提取的对象。
"""
from __future__ import annotations

import re
from typing import Any

from .models import UIContract, UIElement

#: 从需求描述里识别元素类型的关键词 -> 内部 type（与 uigate._TYPE_TO_ROLE 对齐）
#: 顺序无关：匹配时取**最长**关键词，避免 "input" 抢走 "password input"。
_TYPE_WORDS: tuple[tuple[str, str], ...] = (
    ("password input", "password"),
    ("password field", "password"),
    ("input field", "textbox"),
    ("input box", "textbox"),
    ("text area", "textarea"),
    ("textarea", "textarea"),
    ("multiline textbox", "textarea"),
    ("textbox", "textbox"),
    ("dropdown", "combobox"),
    ("combobox", "combobox"),
    ("checkbox", "checkbox"),
    ("radio button", "radio"),
    ("radio", "radio"),
    ("heading", "heading"),
    ("button", "button"),
    ("link", "link"),
    ("tab", "tab"),
    ("table", "table"),
    ("list", "list"),
    ("field", "textbox"),
    ("input", "textbox"),
    ("select", "combobox"),
)

_TYPE_ALT = "|".join(
    re.escape(word) for word, _ in sorted(_TYPE_WORDS, key=lambda kv: -len(kv[0]))
)
_TYPE_MAP = {word: kind for word, kind in _TYPE_WORDS}

#: 「名字引出语」——出现在元素名之前，表示后面那个引号串是它的可访问名/标签
_INTRODUCERS = (
    "with the accessible name", "accessible name",
    "with the visible text", "with the text", "with text",
    "whose placeholder is", "with the placeholder", "placeholder is",
    "placeholder", "labelled", "labeled", "named", "titled", "reading",
)
_INTRO_ALT = "|".join(sorted((re.escape(i) for i in _INTRODUCERS), key=len, reverse=True))

#: 引号串。**必须同时支持全角引号** —— 实测 ctrip 通篇用 `“注册”` / `“登录”`，
#: 只认 ASCII 引号会让它 **0/133 个需求提取到元素**（整条门禁对该 app 完全失效）。
_QUOTED = re.compile(
    r'"(?P<dq>[^"\n]{1,80})"'
    r"|'(?P<sq>[^'\n]{1,80})'"
    r"|`(?P<bq>[^`\n]{1,80})`"
    r"|\u201c(?P<cq>[^\u201d\n]{1,80})\u201d"          # “ ”
    r"|\u2018(?P<ce>[^\u2019\n]{1,80})\u2019"          # ‘ ’
    r"|\u300c(?P<cj>[^\u300d\n]{1,80})\u300d"          # 「 」
    r"|\u300e(?P<ck>[^\u300f\n]{1,80})\u300f"          # 『 』
)
_QUOTED_GROUPS = ("dq", "sq", "bq", "cq", "ce", "cj", "ck")

#: 「点击 X」这类**动作动词 + 名字**的句式 —— 元素类型没写出来，但
#: 它明确指向一个可交互元素（ctrip 通篇都是这种写法）。
_ACTION_VERB = re.compile(
    r"\b(click|clicks|clicking|activate|activates|activating|press|presses|"
    r"tap|taps|select|selects|choose|chooses)\b",
    re.IGNORECASE,
)


def _quoted_name(match: re.Match[str]) -> str:
    for group in _QUOTED_GROUPS:
        value = match.group(group)
        if value:
            return value.strip()
    return ""

#: 句中的人称代词/无意义短串，不当作可访问名
_NOISE_NAMES = frozenset({"it", "them", "this", "that", "the", "a", "an"})

_TYPE_BEFORE = re.compile(rf"\b({_TYPE_ALT})\b", re.IGNORECASE)
_INTRO_BEFORE = re.compile(rf"\b({_INTRO_ALT})\b", re.IGNORECASE)
_QUANTIFIER = re.compile(
    r"\b(exactly one|only one|unique|exactly two|two|three)\b", re.IGNORECASE
)

#: 前后窗口（字符数）。**必须短**：窗口太长时，上一个元素的名字会被算进
#: 当前引号串的类型判定。实测 `...textbox \`Email address\`, enters \`Password123!\``
#: 里 `Password123!` 是**值**不是名字，90 字符窗口会把它误判成 textbox。
_BEFORE_WINDOW = 48
_AFTER_WINDOW = 20

#: 明显是「整句片段」而不是可访问名的特征：含分隔标点 / 过长 / 词数过多 /
#: 以非字母数字开头结尾。宁缺勿滥 —— 错名字会让门禁去要求一个不存在的元素。
_CLAUSE_CHARS = re.compile(r"[,;:()\[\]{}/\\]")


def _looks_like_name(name: str) -> bool:
    """这个引号串像不像「可访问名」，而不是被引号括起来的整句片段。"""
    text = name.strip()
    if not text or len(text) > 40:
        return False
    if _CLAUSE_CHARS.search(text):
        return False
    if len(text.split()) > 7:
        return False
    return bool(re.match(r"[\w\u4e00-\u9fff]", text))


def _sentences(text: str) -> list[str]:
    """按**换行/分号**切段。

    ★不能按 `.` 切：实测 bookstack REQ-2.2 的邮箱 `bookstack_user@example.com`
    里就有点号，按句点切会把邮箱劈成两半，劈出来的**悬空引号**会和后面的引号
    配对，产出 `', enters '` / `' in the uniquely labelled email textbox '`
    这种"名字"。版本号（`REQ-1.1`、`4.3.1`）同理。

    换行是天然的段落边界（adapter 把描述与每个场景步骤按行拼进来），
    配合下面的短窗口足以避免跨句污染。
    """
    return [s for s in re.split(r"[;\n]", str(text or "")) if s.strip()]


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")
    return slug[:40] or "x"


def _last_match(pattern: re.Pattern[str], window: str) -> re.Match[str] | None:
    found = None
    for found in pattern.finditer(window):  # noqa: B007 —— 取最后一个
        pass
    return found


def extract_elements(text: str) -> list[dict[str, Any]]:
    """从一段正文提取元素（粒度 A）。

    返回 [{"type","name","count","how"}, ...]，按出现顺序去重。
    规则（**先看名字前面，再看名字后面**）：
      1. 名字**前面**最近的元素类型词 + 引出语  -> 如 `textbox labeled "Username"`
      2. 名字**前面**最近的元素类型词（无引出语）-> 如 `visible heading "BookStack"`
      3. 名字**后面**紧跟元素类型词             -> 如 "`Save Page` button is visible"
    没有类型词的引号串**不提取** —— 宁缺勿滥，避免把随机引用当成 UI 元素。
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for sentence in _sentences(text):
        count = 1

        for quoted in _QUOTED.finditer(sentence):
            name = _quoted_name(quoted)
            if not name or name.lower() in _NOISE_NAMES:
                continue
            if not _looks_like_name(name):
                continue
            before = sentence[max(0, quoted.start() - _BEFORE_WINDOW):quoted.start()]
            after = sentence[quoted.end():quoted.end() + _AFTER_WINDOW]

            type_match = _last_match(_TYPE_BEFORE, before)
            intro_match = _last_match(_INTRO_BEFORE, before)
            kind = ""
            how = ""
            if type_match and intro_match:
                kind, how = _TYPE_MAP[type_match.group(1).lower()], "前置类型+引出语"
            elif type_match:
                kind, how = _TYPE_MAP[type_match.group(1).lower()], "前置类型"
            else:
                after_match = _TYPE_BEFORE.match(after.strip()[:20]) or (
                    _TYPE_BEFORE.search(after[:12])
                )
                if after_match:
                    kind, how = _TYPE_MAP[after_match.group(1).lower()], "后置类型"
                elif _ACTION_VERB.search(before):
                    # 「Click “注册”」——类型没写，但明确是可交互元素。
                    # 归为 button（点击类动词里最常见的落点）；不追求精确类型，
                    # 因为门禁只用它做**可访问名覆盖**判定。
                    kind, how = "button", "动作动词"
            if not kind:
                continue

            # 计数只在**类型词之前的一小段**里找，避免把无关的 "three banner images"
            # 算到某个元素头上（实测 12306 REQ-1.1 就出现 count=3 的误判）。
            local = before[-30:]
            quantifier = _QUANTIFIER.search(local) or _QUANTIFIER.search(after)
            local_count = 1
            if quantifier:
                local_count = {"two": 2, "exactly two": 2, "three": 3}.get(
                    quantifier.group(1).lower(), 1)

            key = (kind, name.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({"type": kind, "name": name, "count": local_count, "how": how})
    return out


def contracts_from_text(text: str, *, req_id: str = "", title: str = "") -> tuple[UIContract, ...]:
    """把一段正文（描述 + 场景）转成 `ui_contracts` 形状。提取不到则返回空元组。"""
    elements = extract_elements(text)
    if not elements:
        return ()
    seen_ids: set[str] = set()
    ui_elements: list[UIElement] = []
    for item in elements:
        base = f"{item['type']}--{_slug(item['name'])}"
        element_id = base
        suffix = 2
        while element_id in seen_ids:
            element_id = f"{base}-{suffix}"
            suffix += 1
        seen_ids.add(element_id)
        quantifier = "exactly one" if item["count"] == 1 else f"exactly {item['count']}"
        ui_elements.append(
            UIElement(
                element_id=element_id,
                type=item["type"],
                label=item["name"],
                # 计数写进 validation：模型应以「恰好一个」的语义实现，
                # 而 UIElement 没有独立字段（不改模型契约，控制影响面）。
                validation=f"从需求正文提取（{item['how']}）：{quantifier} 个可访问名为 "
                           f"{item['name']!r} 的 {item['type']}",
            )
        )
    return (
        UIContract(
            page="(从需求正文提取)",
            title=title or req_id,
            elements=tuple(ui_elements),
        ),
    )


def requirement_text(requirement: Any) -> str:
    """把需求的**正文相关字段**拼成待提取文本（描述 + 场景步骤）。

    只取正文类字段：不碰 `ui_contracts`（那是显式覆盖），
    也不碰 acceptance（平台真实格式里没有这个字段）。
    """
    parts: list[str] = [str(getattr(requirement, "description", "") or "")]
    for scenario in getattr(requirement, "scenarios", ()) or ():
        for step in getattr(scenario, "steps", ()) or ():
            if isinstance(step, dict):
                parts.append(str(step.get("content") or ""))
            else:
                parts.append(str(getattr(step, "content", "") or ""))
    return "\n".join(p for p in parts if p)
