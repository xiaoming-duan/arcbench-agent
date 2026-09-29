from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from pathlib import Path


# --- 防御性 SDK 引导 -------------------------------------------------------
# 平台契约要求调用内置 arcbench_agent_runtime。本包同时自带一份 vendored 副本
# （arcbench-agent-runtime/src），而 requirements.txt 里的相对路径依赖在不同 CWD
# 下会让 pip 直接失败 —— 那样模块级 import 会在进程启动瞬间抛 ImportError，
# 后端读到的是 0 事件、约 0s 的静默退出，看起来就像"Agent 没跑"。
# 这里在 import 之前先把 vendored 路径挂上，使 SDK 导入不再依赖 pip 是否成功。
def _bootstrap_sdk() -> None:
    try:
        import arcbench_agent_runtime  # noqa: F401

        return  # 平台已注入 / 已安装 —— 不覆盖，避免遮蔽内置版本
    except ImportError:
        pass
    vendored = Path(__file__).resolve().parent / "arcbench-agent-runtime" / "src"
    if vendored.is_dir() and str(vendored) not in sys.path:
        sys.path.insert(0, str(vendored))


_bootstrap_sdk()

from arcbench_agent_runtime import AgentRuntime  # noqa: E402

from factory import FactoryConfig, run_factory  # noqa: E402

LOGGER = logging.getLogger("factory.main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the ARC-Bench software-factory agent.")
    parser.add_argument(
        "requirement_path",
        nargs="?",
        default=os.environ.get("ARCBENCH_TASK_DIR", "requirements"),
        help="Requirement directory containing requirements.yaml.",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("ARCBENCH_OUTPUT_DIR", "."),
        help="Output workspace directory.",
    )
    parser.add_argument(
        "--type",
        dest="task_type",
        default=os.environ.get("ARCBENCH_TASK_TYPE", "web"),
        help="Task type supplied by ARC-Bench (web, cli, or android).",
    )
    # 工厂可选开关（都有环境变量等价物）
    parser.add_argument("--requirements-file", default=None, help="显式指定需求文件名。")
    parser.add_argument(
        "--generator",
        default=None,
        choices=["auto", "llm", "stub"],
        help="生成器选择。auto: 有凭据走 llm，否则走 stub。",
    )
    parser.add_argument(
        "--test-dialect",
        default=None,
        choices=["auto", "vitest", "node"],
        help="测试执行后端。auto: 有 vitest 用 vitest，否则用 node 内置 runner。",
    )
    parser.add_argument("--max-repairs", type=int, default=None, help="每个需求的最大修复轮次。")
    parser.add_argument(
        "--install-deps",
        default=None,
        choices=["auto", "always", "never"],
        help="是否尝试安装 backend 依赖。",
    )
    parser.add_argument("--dry-run", action="store_true", help="只做解析与入库，不执行生成。")
    parser.add_argument("--quiet", action="store_true", help="降低日志级别。")
    return parser.parse_args()


def configure_logging(quiet: bool) -> None:
    logging.basicConfig(
        level=logging.WARNING if quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def resolve_requirements_dir(path: str) -> Path:
    return Path(path).resolve()


def resolve_output_dir(path: str) -> Path:
    return Path(path).resolve()


def resolve_starter_template_dir() -> Path:
    return Path(__file__).resolve().parent / "template"


def _fallback_output_dir() -> Path:
    """在 argparse 跑完之前就崩溃时，也要尽力定位 --output-dir。

    平台按固定形态启动：python3 main.py <requirements> --output-dir <out> --type web
    """
    env = os.environ.get("ARCBENCH_OUTPUT_DIR")
    if env:
        return Path(env).resolve()
    argv = sys.argv[1:]
    if "--output-dir" in argv:
        idx = argv.index("--output-dir")
        if idx + 1 < len(argv):
            return Path(argv[idx + 1]).resolve()
    return Path(".").resolve()


def _report_failure(runtime: AgentRuntime | None, message: str) -> None:
    """尽最大努力把失败态上报给平台。

    只用 SDK 高层方法（events.mark_run_failed），不自己构造 event payload。
    上报本身失败绝不能掩盖原始异常，所以整体吞掉。
    """
    try:
        if runtime is None:
            runtime = AgentRuntime.from_env(project_dir=str(_fallback_output_dir()))
        runtime.events.mark_run_failed(message[:4000])
    except Exception as exc:  # pragma: no cover - 仅在平台侧不可用时走到
        print(f"[arcbench] 上报 agent.failed 失败: {exc}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    configure_logging(args.quiet)

    requirements_dir = resolve_requirements_dir(args.requirement_path)
    output_dir = resolve_output_dir(args.output_dir)

    # 关键：runtime 的项目根必须是 output_dir，否则 .arc/ 会写到 CWD
    runtime = AgentRuntime.from_env(project_dir=str(output_dir))

    config = None
    try:
        config = FactoryConfig.from_env(
            requirements_filename=args.requirements_file,
            task_type=args.task_type,
            generator=args.generator,
            test_dialect=args.test_dialect,
            max_repairs=args.max_repairs,
            install_deps=args.install_deps,
            dry_run=args.dry_run or None,
            verbose=not args.quiet,
        )
    except Exception as exc:
        # 这一步在 run_factory 之外，pipeline 的兜底覆盖不到，必须自己上报
        LOGGER.error("配置失败: %s", exc)
        _report_failure(runtime, f"配置失败: {exc}\n{traceback.format_exc()}")
        return 1

    try:
        report = run_factory(
            runtime,
            requirements_dir,
            output_dir,
            config=config,
            template_dir=resolve_starter_template_dir(),
        )
    except Exception as exc:
        # pipeline 内部已调用 store.fail_run 上报失败态，这里不再重复上报；
        # 真正的兜底交给 __main__ 的防御性入口。
        LOGGER.error("运行失败: %s", exc)
        return 1

    if report.cost:
        LOGGER.info(
            "成本: token %s（prompt %s + completion %s，推理 %s）/ 调用 %s 次 / 网关重试 %s 次",
            report.cost.get("total_tokens"),
            report.cost.get("prompt_tokens"),
            report.cost.get("completion_tokens"),
            report.cost.get("reasoning_tokens"),
            report.cost.get("calls"),
            report.cost.get("gateway_retries"),
        )
    LOGGER.info(
        "结束: %s（通过 %d/%d）",
        "OK" if report.ok else "FAILED",
        sum(1 for item in report.results if item.state == "PASSED"),
        report.requirements_total,
    )
    return 0 if report.ok else 2


def _entrypoint() -> int:
    """防御性入口：崩溃后若不上报 agent.failed，后端看到的只是静默退出。

    SystemExit 不是 Exception，正常退出码（0/1/2）会原样透传。
    """
    try:
        return main()
    except Exception:
        detail = traceback.format_exc()
        print(detail, file=sys.stderr)
        _report_failure(None, f"agent 崩溃: {detail}")
        return 1


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
