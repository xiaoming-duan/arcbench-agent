from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# 需求文件名候选：真实格式到位后可通过 --requirements-file 覆盖，无需改代码
DEFAULT_REQUIREMENTS_FILENAMES = (
    "requirements.yaml",
    "requirements.yml",
    "requirements.json",
)

BACKEND_VITEST_BIN = Path("backend") / "node_modules" / "vitest"


def _parse_aliases(raw: str) -> dict:
    """解析 FACTORY_IMPORT_ALIASES，形如 '{"@/": "backend/src/"}'。"""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


@dataclass
class FactoryConfig:
    """工厂运行配置。全部可由环境变量或命令行覆盖。"""

    # --- 输入 ---
    requirements_filename: str | None = None
    task_type: str = "web"

    # --- 生成器 ---
    # auto: 有 OPENAI_API_KEY 且 openai 可导入则用 llm，否则退回 stub
    generator: str = "auto"
    model_temperature: float = 0.2
    # 网关延迟方差很大（实测同一调用 36s~>600s），必须给单次调用设上界
    model_timeout_s: float = 180.0
    # 实测：max_tokens 过大时服务端会把时间烧在长输出上（3000 会超时，1200 约 36s）
    model_max_tokens: int = 1500

    # --- 测试执行 ---
    # auto: backend/node_modules/vitest 存在则 vitest，否则 node(内置 test runner)
    test_dialect: str = "auto"
    test_timeout_s: int = 600
    backend_dir: str = "backend"
    backend_test_dir: str = "backend/tests"
    # 实现代码根目录，供测试 import 审计使用
    implementation_root: str = "backend/src"
    # 路径别名解析规则：{"@/": "backend/src/"}。未配置的别名一律判 WEAK_TEST。
    import_aliases: dict = field(default_factory=dict)

    # --- TDD 循环 ---
    max_repairs: int = 2
    require_red_first: bool = True
    # WEAK_TEST（测试在实现前就通过）时，回退到"写测试"阶段重写的次数上限。
    # 重写仍无效 => 该需求直接判 FAILED，不进入实现。
    max_test_rewrites: int = 4
    # A：是否强制测试路径白名单（计划外测试文件一律拒绝）
    enforce_test_whitelist: bool = True
    # C：是否把 import 审计纳入 RED 门禁（逐文件判定，而非只看整组退出码）
    audit_in_red_gate: bool = True
    # 方案1：是否强制「声明依赖必须被真实调用」（模块层空转检测）
    enforce_dependency_usage: bool = True
    # 方案2-A：是否禁止 mock 未声明的上游（测试层空转检测）
    enforce_mock_check: bool = True
    # 方案2-B：是否检测实现层注入旁路（**仅警告，不阻断**）
    warn_injection_bypass: bool = True
    # 根节点测试用例数上限（**默认 0 = 关闭**，实验时开启为 3 或 4）。
    #
    # 假说：根节点无上游约束，模型倾向写更多场景，导致实现难度上升。
    #   实测（closure6，n=1）：run2 的 REQ-5（根节点）写了 **6** 个用例 -> FAILED(4 轮)；
    #   而 run1/run3 的 REQ-5 写了 **2** 个用例 -> PASSED(1 轮)。3 倍差距。
    # 注意这是**未确证**的假说 —— 失败样本 n=1，需对照实验区分
    #   「测试过度生成导致难度上升」vs「那次恰好实现较弱」。
    #
    # 默认关闭，保证既有行为逐位不变。
    root_test_limit: int = 0

    # ---- 分类重试预算（P0-2）----
    # 四类错误**各自独立计数**，互不挤占。
    # 关键：环境错误只给 1 次 —— 网关/DNS/配额问题重试再多也不会成功，
    # 让它占用实现预算等于用一个不可控因素压低模型能力的评估。
    # 默认值见 factory/errors.DEFAULT_BUDGETS。
    repair_budget_design: int = 2
    repair_budget_implementation: int = 3
    repair_budget_test: int = 2
    repair_budget_environment: int = 1
    # 跨模块调用契约的**签名校验**（仅在需求声明了 cross_module_calls 时生效）。
    # 关闭后仍会注入提示词，但不按声明比对调用形状 —— 用于对照实验区分
    # 「提示词的效果」与「门禁的效果」。
    enforce_contract_signature: bool = True
    # 上游失败传播：声明依赖的上游未通过时，下游不进入 TDD 循环
    enforce_upstream_gate: bool = True
    # 重写阶段是否强制「只改实现」：测试文件不可修改（默认 True）。
    # 关闭后允许改测试，用于对照实验比较两种边界的收敛差异。
    enforce_impl_only_rewrite: bool = True
    # 方案2-B 是否升级为阻断。默认 False：第一轮只观察发生率。
    # 实验里用 True 回答"若升级为阻断，模型能否修好"这个假设性问题。
    block_injection_bypass: bool = False
    # 重写反馈的形态。basic = 只说"测试通过了"；import_aware = 附上静态 import 诊断。
    # 保留 basic 是为了能对二者做 A/B 度量（见 tools/experiment_weak_feedback.py）。
    weak_feedback_mode: str = "import_aware"

    # --- 依赖安装 ---
    # auto: 仅在缺失 node_modules 且能联网时尝试
    install_deps: str = "auto"
    install_timeout_s: int = 900

    # --- 其他 ---
    dry_run: bool = False
    verbose: bool = True

    @classmethod
    def from_env(cls, **overrides: object) -> "FactoryConfig":
        cfg = cls(
            requirements_filename=os.environ.get("FACTORY_REQUIREMENTS_FILE") or None,
            task_type=os.environ.get("ARCBENCH_TASK_TYPE", "web"),
            generator=os.environ.get("FACTORY_GENERATOR", "auto"),
            model_temperature=float(os.environ.get("FACTORY_TEMPERATURE", "0.2")),
            model_timeout_s=float(os.environ.get("FACTORY_MODEL_TIMEOUT", "180")),
            model_max_tokens=int(os.environ.get("FACTORY_MAX_TOKENS", "1500")),
            test_dialect=os.environ.get("FACTORY_TEST_DIALECT", "auto"),
            test_timeout_s=int(os.environ.get("FACTORY_TEST_TIMEOUT", "600")),
            backend_dir=os.environ.get("FACTORY_BACKEND_DIR", "backend"),
            backend_test_dir=os.environ.get("FACTORY_BACKEND_TEST_DIR", "backend/tests"),
            implementation_root=os.environ.get("FACTORY_IMPL_ROOT", "backend/src"),
            import_aliases=_parse_aliases(os.environ.get("FACTORY_IMPORT_ALIASES", "")),
            max_repairs=int(os.environ.get("FACTORY_MAX_REPAIRS", "2")),
            max_test_rewrites=int(os.environ.get("FACTORY_MAX_TEST_REWRITES", "4")),
            weak_feedback_mode=os.environ.get("FACTORY_WEAK_FEEDBACK", "import_aware"),
            enforce_test_whitelist=os.environ.get("FACTORY_WHITELIST", "1") not in {"0", "false", "False"},
            audit_in_red_gate=os.environ.get("FACTORY_AUDIT_GATE", "1") not in {"0", "false", "False"},
            enforce_dependency_usage=os.environ.get("FACTORY_DEP_USAGE", "1") not in {"0", "false", "False"},
            enforce_mock_check=os.environ.get("FACTORY_MOCK_CHECK", "1") not in {"0", "false", "False"},
            warn_injection_bypass=os.environ.get("FACTORY_BYPASS_WARN", "1") not in {"0", "false", "False"},
            block_injection_bypass=os.environ.get("FACTORY_BYPASS_BLOCK", "0") in {"1", "true", "True"},
            enforce_impl_only_rewrite=os.environ.get("FACTORY_IMPL_ONLY", "1") not in {"0", "false", "False"},
            enforce_upstream_gate=os.environ.get("FACTORY_UPSTREAM_GATE", "1") not in {"0", "false", "False"},
            enforce_contract_signature=os.environ.get("FACTORY_CONTRACT_SIG", "1") not in {"0", "false", "False"},
            root_test_limit=max(0, int(os.environ.get("FACTORY_ROOT_TEST_LIMIT", "0") or 0)),
            repair_budget_design=int(os.environ.get("FACTORY_BUDGET_DESIGN", "2")),
            repair_budget_implementation=int(os.environ.get("FACTORY_BUDGET_IMPL", "3")),
            repair_budget_test=int(os.environ.get("FACTORY_BUDGET_TEST", "2")),
            repair_budget_environment=int(os.environ.get("FACTORY_BUDGET_ENV", "1")),
            require_red_first=os.environ.get("FACTORY_REQUIRE_RED_FIRST", "1") not in {"0", "false", "False"},
            install_deps=os.environ.get("FACTORY_INSTALL_DEPS", "auto"),
            install_timeout_s=int(os.environ.get("FACTORY_INSTALL_TIMEOUT", "900")),
            dry_run=os.environ.get("FACTORY_DRY_RUN", "0") in {"1", "true", "True"},
            verbose=os.environ.get("FACTORY_VERBOSE", "1") not in {"0", "false", "False"},
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        return cfg

    # ---- 派生决策 ----

    def resolve_requirements_file(self, requirements_dir: Path) -> Path:
        """定位需求文件。显式指定优先，否则按候选名探测。"""
        if self.requirements_filename:
            candidate = requirements_dir / self.requirements_filename
            if not candidate.is_file():
                raise FileNotFoundError(f"指定的需求文件不存在: {candidate}")
            return candidate
        for name in DEFAULT_REQUIREMENTS_FILENAMES:
            candidate = requirements_dir / name
            if candidate.is_file():
                return candidate
        raise FileNotFoundError(
            f"在 {requirements_dir} 下未找到需求文件，候选名: {', '.join(DEFAULT_REQUIREMENTS_FILENAMES)}"
        )

    def has_model_credentials(self) -> bool:
        return bool(os.environ.get("OPENAI_API_KEY", "").strip())

    def resolve_generator(self) -> str:
        if self.generator in {"llm", "stub"}:
            return self.generator
        return "llm" if self.has_model_credentials() else "stub"

    def resolve_test_dialect(self, output_dir: Path) -> str:
        """auto: 有 vitest 就用 vitest，否则退回 node 内置测试运行器。

        注意：node 方言是**降级验证路径**，用于依赖不可安装时仍能跑通
        RED -> GREEN。生产路径应为 vitest。
        """
        if self.test_dialect in {"vitest", "node"}:
            return self.test_dialect
        if (output_dir / BACKEND_VITEST_BIN).exists():
            return "vitest"
        return "node"

    def describe(self) -> str:
        return (
            f"generator={self.resolve_generator()} "
            f"test_dialect={self.test_dialect}(auto->运行时判定) "
            f"max_repairs={self.max_repairs} "
            f"max_test_rewrites={self.max_test_rewrites} "
            f"weak_feedback={self.weak_feedback_mode} "
            f"require_red_first={self.require_red_first} "
            f"install_deps={self.install_deps}"
        )
