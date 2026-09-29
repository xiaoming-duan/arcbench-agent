"""代码版本锚点：记录每一次运行**跑在哪份代码上**。

======================  为什么必须有它  ======================
这个工作区长期没有版本控制，代价已经付过三次：

  1. `RunReport.to_dict` 少输出 5 个字段 —— patch 静默失败，没人发现
  2. `measure_source` 不认别名断言 —— 弱化守卫从未真正生效
  3. 并发写入：一次 v2 测量**运行到一半**时 `factory/loop.py`、
     `factory/testaudit.py` 被另一条工作流改动（见 tools/workstream.py 的 docstring）

三次的共同点：**「验证代码本身没有被验证」，而报告上不留任何痕迹。**
只要每次运行都写下一个版本锚点，这三次都能当场定位。

======================  关键约定  ======================
**脏工作区 = 本次运行不可信**（`trustworthy=False`）。

这不是「失败」，而是标注「测量基础可疑」——避免把一份说不清的代码跑出来的
数据当成干净数据使用。同理，**取不到 git 信息时也是不可信**（unknown ≠ clean）。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("factory.version")

# 取不到版本信息时的占位符
UNKNOWN = "unknown"


def git_state(repo_dir: Path | str | None = None) -> dict[str, Any]:
    """读取当前代码版本状态。

    返回::

        {
          "head": "95e8469",        # 短 SHA；取不到则为 "unknown"
          "branch": "main",
          "dirty_count": 0,         # 未提交改动数；取不到则为 -1
          "dirty_files": [...],     # 前若干条，便于直接定位
          "trustworthy": True,      # 仅当 dirty_count == 0 时为真
        }
    """
    cwd = str(repo_dir) if repo_dir else None

    def _run(args: list[str]) -> str | None:
        try:
            proc = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    head = _run(["git", "rev-parse", "--short", "HEAD"])
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    status = _run(["git", "status", "--porcelain"])

    if head is None or status is None:
        return {
            "head": UNKNOWN,
            "branch": UNKNOWN,
            "dirty_count": -1,
            "dirty_files": [],
            "trustworthy": False,
            "note": "取不到 git 信息（不是仓库 / 未安装 git / 命令失败）",
        }

    dirty_files = [line.strip() for line in status.splitlines() if line.strip()]
    return {
        "head": head,
        "branch": branch or UNKNOWN,
        "dirty_count": len(dirty_files),
        "dirty_files": dirty_files[:10],
        "trustworthy": not dirty_files,
    }


def describe(state: dict[str, Any]) -> str:
    """把版本状态变成一行可读文本。"""
    head = state.get("head", UNKNOWN)
    dirty = state.get("dirty_count", -1)
    if dirty < 0:
        return f"代码版本: {head}（无法判定是否干净 —— 本次测量不可信）"
    if dirty == 0:
        return f"代码版本: {head}（工作区干净）"
    return f"代码版本: {head}（**{dirty} 个未提交改动 —— 本次测量基础可疑**）"


def log_state(state: dict[str, Any], *, label: str = "版本") -> None:
    """记录版本状态；不可信时用 WARNING 让它无法被忽略。"""
    text = describe(state)
    if state.get("trustworthy"):
        logger.info("[%s] %s", label, text)
        return
    logger.warning("[%s] %s", label, text)
    for item in state.get("dirty_files") or []:
        logger.warning("[%s]   未提交: %s", label, item)
