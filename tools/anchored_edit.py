"""锚点编辑断言：锚点未匹配就抛异常，绝不静默继续。

=====================  为什么需要它  =====================
本项目出现过一次真实事故：用脚本做 `text.replace(anchor, new)` 打补丁，
锚点没匹配上，`str.replace` **静默返回原文**，于是改动没落地却没有任何提示。
结果是 `RunReport.to_dict` 少了 5 个可观测字段，直到几轮之后核对报告才发现。

`str.replace` 的失败模式是"无声的"——没有断言就等于没有守卫。
本模块把断言固化成唯一入口：**锚点必须存在、且必须唯一**，否则带
文件路径 + 行号 + 上下文抛异常。

约定：所有脚本化的源码改动都必须经过 `replace_once()`，禁止裸用 `str.replace`。
=======================================================
"""

from __future__ import annotations

from pathlib import Path


class AnchorError(AssertionError):
    """锚点未匹配或不唯一。"""


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _context(text: str, index: int, *, before: int = 2, after: int = 2) -> str:
    lines = text.splitlines()
    line_no = _line_of(text, index)
    start = max(0, line_no - 1 - before)
    end = min(len(lines), line_no + after)
    return "\n".join(f"    {i + 1:>5} | {lines[i]}" for i in range(start, end))


def replace_once(
    path: str | Path,
    anchor: str,
    replacement: str,
    *,
    label: str = "",
    require_unique: bool = True,
) -> None:
    """在 `path` 中把 `anchor` 替换为 `replacement`。

    Raises:
        AnchorError: 锚点未找到，或（require_unique 时）出现多次。
        FileNotFoundError: 文件不存在。
    """
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"[锚点断言] 文件不存在: {target}")

    text = target.read_text(encoding="utf-8")
    count = text.count(anchor)
    where = f"{target}" + (f" ({label})" if label else "")

    if count == 0:
        raise AnchorError(
            f"[锚点断言] 锚点未匹配，改动未落地\n"
            f"  文件: {where}\n"
            f"  锚点({len(anchor)} 字符): {anchor[:160]!r}\n"
            f"  该文件共 {len(text.splitlines())} 行"
        )
    if count > 1 and require_unique:
        first = text.index(anchor)
        raise AnchorError(
            f"[锚点断言] 锚点不唯一（匹配 {count} 处），拒绝猜测替换位置\n"
            f"  文件: {where}\n"
            f"  锚点: {anchor[:160]!r}\n"
            f"  首个匹配位置:\n{_context(text, first)}"
        )

    new_text = text.replace(anchor, replacement, 1 if not require_unique else -1)
    if new_text == text:
        # 理论上不可达（count>0 且 replacement 不同才会改变）；留作最后一道防线
        raise AnchorError(f"[锚点断言] 替换后文本未变化，疑似静默失败: {where}")
    target.write_text(new_text, encoding="utf-8")


def assert_contains(path: str | Path, *needles: str) -> None:
    """断言文件包含全部片段；缺失则抛异常（用于改动后自检）。"""
    text = Path(path).read_text(encoding="utf-8")
    missing = [n for n in needles if n not in text]
    if missing:
        raise AnchorError(f"[锚点断言] {path} 缺少片段: {missing}")
