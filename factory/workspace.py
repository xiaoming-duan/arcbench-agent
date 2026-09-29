"""工作区操作：模板复制、文件读写、生成结果落盘、需求文件加载。"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .models import GeneratedFile

logger = logging.getLogger("factory.workspace")


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------


def copy_template_contents(template_dir: Path, output_dir: Path) -> list[str]:
    """把 template/ 的**内容**复制进 output_dir，使 output_dir 成为工程根。"""
    if not template_dir.is_dir():
        raise FileNotFoundError(f"模板目录不存在: {template_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for source in sorted(template_dir.iterdir()):
        destination = output_dir / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        elif source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        copied.append(source.name)
    logger.info("已复制模板内容 %d 项 -> %s", len(copied), output_dir)
    return copied


# ---------------------------------------------------------------------------
# 需求文件加载
# ---------------------------------------------------------------------------


def load_requirements_raw(path: Path) -> dict[str, Any] | list[Any]:
    """加载需求文件。优先 YAML，缺失时退回 JSON。"""
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".json"}:
        return json.loads(text)
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        logger.warning("PyYAML 不可用，尝试按 JSON 解析需求文件")
        return json.loads(text)


# ---------------------------------------------------------------------------
# 生成结果落盘
# ---------------------------------------------------------------------------


def _relpath_for_log(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def apply_generated_files(output_dir: Path, files: list[GeneratedFile]) -> list[str]:
    """把生成的文件变更写入工作区，返回实际发生变化的相对路径列表。"""
    changed: list[str] = []
    for item in files:
        target = (output_dir / item.path).resolve()
        if not str(target).startswith(str(output_dir.resolve())):
            raise ValueError(f"生成路径越界，拒绝写入: {item.path}")
        target.parent.mkdir(parents=True, exist_ok=True)

        if item.mode == "write":
            new_text = item.content
            old_text = target.read_text(encoding="utf-8") if target.exists() else None
            if old_text != new_text:
                target.write_text(new_text, encoding="utf-8")
                changed.append(_relpath_for_log(target, output_dir))
                logger.debug("写入 %s (%d 字节)", item.path, len(new_text))
            continue

        # insert_after：幂等插入
        if not target.exists():
            raise FileNotFoundError(f"insert_after 目标文件不存在: {item.path}")
        text = target.read_text(encoding="utf-8")
        if item.content.strip() and item.content.strip() in text:
            logger.debug("跳过已存在的插入内容: %s", item.path)
            continue
        lines = text.splitlines(keepends=True)
        marker = (item.marker or "").strip()
        matches = [
            index for index, line in enumerate(lines)
            if line.strip() == marker or (marker and marker in line)
        ]
        if not matches:
            preview = "".join(f"      {i + 1:>4} | {lines[i]}" for i in range(min(len(lines), 40)))
            raise ValueError(
                f"[锚点断言] 插入标记未找到，改动未落地\n"
                f"  文件: {target}\n"
                f"  标记: {marker!r}\n"
                f"  该文件共 {len(lines)} 行，前 40 行如下（请确认标记拼写）:\n{preview}"
            )
        if len(matches) > 1:
            raise ValueError(
                f"[锚点断言] 插入标记不唯一（匹配 {len(matches)} 处），拒绝猜测插入位置\n"
                f"  文件: {target}\n"
                f"  标记: {marker!r}\n"
                f"  匹配行号: {[m + 1 for m in matches]}"
            )
        insert_at = matches[0] + 1
        block = item.content if item.content.endswith("\n") else item.content + "\n"
        lines.insert(insert_at, block)
        target.write_text("".join(lines), encoding="utf-8")
        changed.append(_relpath_for_log(target, output_dir))
        logger.debug("插入 %s @ %r", item.path, marker)

    return changed


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
