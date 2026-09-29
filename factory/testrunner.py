"""正确面：测试执行器（可插拔后端）。

  vitest  —— 生产路径，模板自带（backend/vitest.config.js, npm run test）
  node    —— 降级/验证路径，Node >=18 内置 test runner，零依赖

两者都返回结构化的 `TestOutcome`，上层 TDD 循环不关心用哪种方言。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from .models import TestOutcome

logger = logging.getLogger("factory.testrunner")

VITEST_BIN = Path("node_modules") / ".bin" / "vitest"
_DEFAULT_OUTPUT_LIMIT = 20000


def _tail(text: str, limit: int = _DEFAULT_OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return "...(截断)...\n" + text[-limit:]


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _clean(text: str) -> str:
    """剥离 ANSI 颜色码。

    vitest 默认给 "Tests  1 failed | 1 passed (2)" 这类汇总行着色，
    不剥离会导致汇总正则匹配失败、计数解析不出来。
    """
    return _ANSI_RE.sub("", text or "")


# ---------------------------------------------------------------------------
# 输出解析
# ---------------------------------------------------------------------------


def _parse_vitest(stdout: str, stderr: str) -> tuple[int, int, list[str]]:
    total = failed = 0
    failures: list[str] = []
    combined = _clean(f"{stdout}\n{stderr}")

    # 形如: "Tests  1 failed | 3 passed (4)" 或 "Tests  4 passed (4)"
    for match in re.finditer(r"Tests\s+(?:(\d+)\s+failed\s*\|\s*)?(\d+)\s+passed\s*\((\d+)\)", combined):
        failed = int(match.group(1) or 0)
        total = int(match.group(3))
    if total == 0:
        for match in re.finditer(r"Tests\s+(\d+)\s+failed\s*\((\d+)\)", combined):
            failed = int(match.group(1))
            total = int(match.group(2))

    # 捕获失败用例及其断言详情（vitest 的 × 行 + 紧随的 AssertionError 块）
    lines = combined.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("×") or stripped.startswith("✗") or stripped.startswith("FAIL "):
            block = [stripped]
            look = index + 1
            while look < len(lines):
                nxt = lines[look]
                if not nxt.strip():
                    look += 1
                    continue
                if nxt.startswith(("×", "✗", "✓", "❯", "FAIL ", "Test Files", "Tests ", "Duration")):
                    break
                block.append(nxt.strip())
                look += 1
                if len(block) >= 14:
                    break
            failures.append("\n".join(block)[:1200])
            index = look
        else:
            index += 1

    if total and failed:
        return total, failed, failures
    if total == 0:
        # 无法解析计数时退回退出码语义
        return 0, 0, failures
    return total, failed, failures


def _parse_node_tap(stdout: str, stderr: str) -> tuple[int, int, list[str]]:
    combined = _clean(f"{stdout}\n{stderr}")
    total = failed = 0
    failures: list[str] = []

    match = re.search(r"^#\s*tests\s+(\d+)", combined, re.MULTILINE)
    if match:
        total = int(match.group(1))
    match = re.search(r"^#\s*fail\s+(\d+)", combined, re.MULTILINE)
    if match:
        failed = int(match.group(1))

    # TAP：捕获 not ok 行及其后的缩进诊断块（error / actual / expected / location）
    lines = combined.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("not ok"):
            block = [line.strip()]
            look = index + 1
            while look < len(lines):
                nxt = lines[look]
                if nxt.startswith(("ok ", "not ok ", "#", "1..")) or (nxt and not nxt[0].isspace()):
                    break
                if nxt.strip():
                    block.append(nxt.strip())
                look += 1
                if len(block) >= 20:
                    break
            failures.append("\n".join(block)[:1500])
            index = look
        else:
            index += 1
    return total, failed, failures


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------


class TestRunner:
    """测试执行器基类。"""

    dialect = "base"

    def __init__(self, output_dir: Path, *, timeout_s: int = 600, backend_dir: str = "backend") -> None:
        self.output_dir = output_dir
        self.backend_dir = backend_dir
        self.workdir = output_dir / backend_dir
        self.timeout_s = timeout_s

    def is_available(self) -> bool:
        raise NotImplementedError

    def _command(self, test_paths: Sequence[str]) -> list[str]:
        raise NotImplementedError

    def _parse(self, stdout: str, stderr: str) -> tuple[int, int, list[str]]:
        raise NotImplementedError

    def run(self, test_paths: Sequence[str]) -> TestOutcome:
        """运行指定测试文件（相对 backend/ 的路径）。"""
        if not self.workdir.is_dir():
            raise FileNotFoundError(f"后端目录不存在: {self.workdir}")
        command = self._command(test_paths)
        logger.info("[%s] 执行: %s (cwd=%s)", self.dialect, " ".join(command), self.workdir)
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                check=False,
                env={**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0"},
            )
            stdout, stderr = completed.stdout or "", completed.stderr or ""
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            return TestOutcome(
                passed=False,
                command=" ".join(command),
                exit_code=124,
                failures=[f"测试超时（>{self.timeout_s}s）"],
                stdout=_tail(_clean(stdout)),
                stderr=_tail(_clean(stderr)),
                dialect=self.dialect,
            )

        total, failed, failures = self._parse(stdout, stderr)
        # 退出码是权威门禁信号；解析出的计数仅用于报告
        passed = exit_code == 0 and failed == 0
        if total == 0 and not passed:
            total, failed = 0, max(failed, 1)
        outcome = TestOutcome(
            passed=passed,
            command=" ".join(command),
            exit_code=exit_code,
            total=total,
            failed=failed,
            failures=failures[:50],
            stdout=_tail(_clean(stdout)),
            stderr=_tail(_clean(stderr)),
            dialect=self.dialect,
        )
        logger.info("[%s] 结果: %s", self.dialect, outcome.summary())
        return outcome


class VitestRunner(TestRunner):
    """生产路径：模板自带的 vitest。"""

    dialect = "vitest"

    def is_available(self) -> bool:
        return (self.workdir / VITEST_BIN).exists()

    def _command(self, test_paths: Sequence[str]) -> list[str]:
        binary = f"./{VITEST_BIN.as_posix()}"
        return [binary, "run", "--reporter=default", *test_paths]

    def _parse(self, stdout: str, stderr: str) -> tuple[int, int, list[str]]:
        return _parse_vitest(stdout, stderr)


class NodeTestRunner(TestRunner):
    """降级/验证路径：Node 内置 test runner，零依赖。

    注意：本路径用于依赖不可安装时仍能真实验证 RED -> GREEN。
    """

    dialect = "node"

    def is_available(self) -> bool:
        return shutil.which("node") is not None

    def _command(self, test_paths: Sequence[str]) -> list[str]:
        return ["node", "--test", "--test-reporter=tap", *test_paths]

    def _parse(self, stdout: str, stderr: str) -> tuple[int, int, list[str]]:
        return _parse_node_tap(stdout, stderr)


def build_runner(dialect: str, output_dir: Path, *, timeout_s: int = 600) -> TestRunner:
    if dialect == "vitest":
        return VitestRunner(output_dir, timeout_s=timeout_s)
    return NodeTestRunner(output_dir, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# 依赖安装（可选）
# ---------------------------------------------------------------------------


def ensure_backend_dependencies(output_dir: Path, *, timeout_s: int = 900) -> bool:
    """尝试安装 backend 依赖。失败返回 False（调用方据此降级方言）。

    运行环境无外网时 npm 会挂起，因此这里用**短超时 + 失败即回退**策略。
    """
    backend = output_dir / "backend"
    if (backend / "node_modules").is_dir():
        return True
    if not shutil.which("npm"):
        logger.warning("未找到 npm，跳过依赖安装")
        return False
    logger.info("尝试安装 backend 依赖（超时 %ss）...", timeout_s)
    command = ["npm", "install", "--no-audit", "--no-fund"]
    # 受限环境（沙箱/只读 HOME）下 npm 默认缓存不可写，允许重定向
    cache_dir = os.environ.get("FACTORY_NPM_CACHE", "").strip()
    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        command += ["--cache", cache_dir]
        logger.info("npm 缓存目录: %s", cache_dir)
    try:
        completed = subprocess.run(
            command,
            cwd=str(backend),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("依赖安装超时，判定为无网络环境")
        return False
    if completed.returncode != 0:
        logger.warning("依赖安装失败: %s", (completed.stderr or "").strip()[:300])
        return False
    logger.info("backend 依赖安装完成")
    return True
