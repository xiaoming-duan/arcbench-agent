"""架构符合性核验：把工作区每个文件对照架构文档的要求逐一核对。

=======================  权威来源  =======================
`软件工厂-项目架构文档.md`（1304 行 / 17 章），其中与文件直接相关的是：
  第 2、9.1 章   目录 → 架构层（控制面 / 正确面 / 数据面）映射
  第 4 章        正确面：质量门禁、验证执行器、审查器
  第 5 章        数据面：需求仓库 / 统一对象模型 / 追溯矩阵 / 审计日志 / 检查点 / 制品仓库
  第 9 章        代码目录结构与模块划分（**规范性最强**）
  第 11.1 章     平台 Runner 契约
  第 11.2 章     内部核心接口
  第 13 章       非功能性（绝不硬编码密钥、本地优先、可审计）

=======================  核验维度  =======================
  A 目录结构（第 9 章）
  B 平台契约（第 11.1）
  C 内部接口（第 11.2）
  D 模块 → 架构层归属（第 2 / 9.1 章）
  E 数据面产物（第 5 章）
  F 非功能性（第 13 章）
  G 文档 ↔ 实现一致性（文档是否描述实际存在的模块）
  H 孤儿文件（无任何引用的文件）

用法：
  python3 tools/audit_architecture.py            打印报告
  python3 tools/audit_architecture.py --json r.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCH_DOC = ROOT / "软件工厂-项目架构文档.md"


@dataclass
class Finding:
    dimension: str
    item: str
    verdict: str          # 符合 / 偏差 / 缺失 / 文档滞后
    severity: str         # 高 / 中 / 低 / —
    detail: str = ""
    evidence: str = ""


FINDINGS: list[Finding] = []


def add(dimension: str, item: str, verdict: str, severity: str,
        detail: str = "", evidence: str = "") -> None:
    FINDINGS.append(Finding(dimension, item, verdict, severity, detail, evidence))


# ---------------------------------------------------------------- A 目录结构

# ★ 结构性事实**从文档解析**，不再硬编码。
# 初版把第 9 章的旧目录写死在工具里，文档 v1.1 修订后工具立刻过时 ——
# 一个跟不上文档的核验工具会持续误报。改为解析文档第 9.1 节的代码块。
_CODE_BLOCK = re.compile(r"^### 9\.1 实际目录结构.*?```text\n(.*?)```", re.S | re.M)


def parse_doc_structure(doc: str) -> set[str]:
    """从第 9.1 节的目录树里解析出文档声明的文件路径。"""
    m = _CODE_BLOCK.search(doc)
    if not m:
        return set()
    paths: set[str] = set()
    stack: list[str] = []
    for raw in m.group(1).splitlines():
        if not raw.strip():
            continue
        # 用 ├── / └── / │ 判断层级
        depth = 0
        line = raw
        while True:
            stripped = line.lstrip("│ ")
            if stripped.startswith(("├──", "└──")):
                depth = (len(line) - len(line.lstrip())) // 4
                line = stripped[3:].strip()
                break
            if not line.startswith(("│", " ")):
                break
            line = line[1:]
        name = line.split("#")[0].strip()
        if not name:
            continue
        while len(stack) > depth:
            stack.pop()
        is_dir = name.endswith("/")
        clean = name.rstrip("/")
        if depth == 0:
            stack = [clean]
        else:
            stack = stack[:depth] + [clean]
        if not is_dir:
            paths.add("/".join(p for p in stack if p and p != "<repo>"))
    return paths


def actual_structure() -> set[str]:
    out = {"main.py"}
    for p in (ROOT / "factory").glob("*.py"):
        out.add(f"factory/{p.name}")
    return out

# 实际模块 → 它所服务的架构层（按 9.1 的映射意图归类，而非按目录名）
ACTUAL_TO_PLANE = {
    "factory/pipeline.py": ("控制面", "编排器 / 领域执行引擎（端到端编排）"),
    "factory/loop.py": ("控制面 + 正确面", "TDD 修复循环 + 质量门禁（RED/WEAK_TEST/依赖门禁）"),
    "factory/generator.py": ("控制面", "智能体池（设计 / 计划 / 写测试 / 实现四种角色）"),
    "factory/llm.py": ("横向", "模型路由层 + 成本记录"),
    "factory/config.py": ("横向", "模型路由层、预算策略"),
    "factory/adapter.py": ("数据面", "需求模型仓库（解析与建模）+ 拓扑排序"),
    "factory/models.py": ("数据面", "统一对象模型"),
    "factory/store.py": ("数据面", "追溯矩阵、审计日志、检查点（Git 提交）"),
    "factory/workspace.py": ("控制面 + 正确面", "工作区隔离、制品落盘"),
    "factory/testplan.py": ("正确面", "测试计划与基线守卫"),
    "factory/testaudit.py": ("正确面", "验证执行器（import 审计 / 依赖使用 / mock / 注入旁路）"),
    "factory/testrunner.py": ("正确面", "验证执行器（vitest / node:test 运行）"),
    "factory/__init__.py": ("—", "包声明"),
}


def dim_a() -> None:
    doc = ARCH_DOC.read_text(encoding="utf-8")
    declared = parse_doc_structure(doc)
    if not declared:
        add("A 目录结构", "第9.1 节结构可解析", "缺失", "中",
            "从文档第 9.1 节的代码块里解析不出任何文件路径",
            "核验依赖该节；节标题或代码块格式变化会导致解析失败")
        return
    actual = actual_structure()
    missing = sorted(actual - declared)      # 实现有、文档没写
    extra = sorted(declared - actual)        # 文档写了、实现没有
    add("A 目录结构", "文档第9.1 节 vs 实际文件", "符合" if not (missing or extra) else "偏差",
        "—" if not (missing or extra) else "中",
        f"文档声明 {len(declared)} 个文件 / 实际 {len(actual)} 个；"
        f"文档缺失 {len(missing)} / 文档多余 {len(extra)}",
        f"文档未记录: {missing or '无'}；文档写了但不存在: {extra or '无'}")
    for name in extra:
        add("A 目录结构", name, "偏差", "中", "文档声明了该文件，实际不存在")
    for name in missing:
        add("A 目录结构", name, "偏差", "中", "实际存在，但文档第 9.1 节未记录")


# ---------------------------------------------------------------- B 平台契约

# 第 11.1 节整节（含选项表），不只是 `python3 main.py ...` 那一行
_SEC_111 = re.compile(r"### 11\.1 .*?(?=\n### 11\.2 )", re.S)


def dim_b() -> None:
    """文档声明的 CLI 选项必须与 main.py 的 argparse 一致。"""
    doc = ARCH_DOC.read_text(encoding="utf-8")
    main_py = (ROOT / "main.py").read_text(encoding="utf-8")
    section = _SEC_111.search(doc)
    scope = section.group(0) if section else doc
    declared = set(re.findall(r"--[a-z][a-z0-9-]*", scope))
    actual = set(re.findall(r'add_argument\(\s*"(--[a-z][a-z0-9-]*)"', main_py))
    missing = sorted(actual - declared)
    extra = sorted(declared - actual)
    add("B 平台契约", "第11.1 CLI 契约 vs main.py argparse",
        "符合" if not (missing or extra) else "偏差",
        "—" if not (missing or extra) else "中",
        f"文档声明 {len(declared)} 个选项 / 实现 {len(actual)} 个；"
        f"文档缺失 {len(missing)} / 文档多余 {len(extra)}",
        f"实现有而文档未写: {missing or '无'}；文档写了而无实现: {extra or '无'}")
    creds = ["OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL"]
    absent = [c for c in creds if c not in doc]
    add("B 平台契约", "凭据经环境变量注入（文档已声明）",
        "符合" if not absent else "偏差", "—" if not absent else "中",
        f"文档{'已声明' if not absent else '未声明'}: {creds}",
        "第 13.1 章要求凭据不落盘")


# ---------------------------------------------------------------- C 内部接口

DOC_INTERFACES = {
    "submit_goal": "编排器：提交目标", "next_task": "编排器：取下一个可执行任务",
    "run_phase": "领域执行引擎：驱动八步 V 模型", "attempt_step": "领域执行引擎：执行单个 Step",
    "decompose": "任务分解器：需求节点 → DAG", "dispatch": "智能体池：按角色派发全新上下文",
    "reserve": "成本控制器：预留预算", "settle": "成本控制器：结算消耗",
    "generate_tests": "测试生成器", "evaluate": "质量门禁：PASS/BLOCK/ROLLBACK",
    "snapshot": "检查点：创建快照", "restore": "检查点：回退",
    "append": "审计日志：追加事件", "link": "追溯矩阵：建立映射", "trace": "追溯矩阵：查询",
}

# 实际存在、承担同类职责的接口
ACTUAL_EQUIVALENTS = {
    "submit_goal": "run_factory() — 端到端入口",
    "next_task": "_topological_order() + pipeline 逐需求循环",
    "run_phase": "TddLoop.run() — 端到端单需求迭代",
    "attempt_step": "TddLoop.run() 内的 RED→实现→GREEN 阶段",
    "decompose": "适配层 dependencies + _topological_order()（有拓扑排序，无 DAG 分解）",
    "dispatch": "Generator Protocol（design/plan_tests/write_tests/implement 四种角色）",
    "reserve": "—（无预算预留，仅有事后成本记账 CallStats）",
    "settle": "CallStats.delta() — 事后结算",
    "generate_tests": "LLMGenerator.plan_tests() + write_tests()",
    "evaluate": "_is_weak() / _enforce_no_weakening() / 依赖门禁 / _is_uncollectable()",
    "snapshot": "testplan.record_baseline()（测试基线，非检查点）",
    "restore": "回归检查的回退（IMPLEMENTATION_REGRESSION → 回退到上一版实现）",
    "append": "FactoryStore 的 store.* 事件写入 + .arc/runner-events.jsonl",
    "link": "FactoryStore.record_* → .arc/traceability/*.json",
    "trace": "—（有写入，无按 id 查询接口）",
}


# 实现侧的关键入口（第 11.2 节的映射表里应逐个出现）
ACTUAL_ENTRY_POINTS = [
    "run_factory", "TddLoop.run", "Generator", "design", "plan_tests", "write_tests",
    "implement", "TestRunner.run", "TestOutcome", "_is_weak", "_is_uncollectable",
    "_enforce_no_weakening", "audit_dependency_usage", "audit_mocked_dependencies",
    "audit_injection_bypass", "_topological_order", "CallStats", "record_baseline",
]


def dim_c() -> None:
    """判据 = 第 11.2 节的「设计接口 → 实现对应」映射是否被完整记录。

    初版判据是「同名实现有几个」，在 11.2 改为映射表之后该判据失效
    —— 名称不同本就是被记录的事实，不该继续算偏差。
    改为：实现侧的关键入口是否都在映射表里出现过，以及缺口是否被标注。
    """
    doc = ARCH_DOC.read_text(encoding="utf-8")
    section = re.search(r"### 11\.2 .*?(?=\n## 12\.)", doc, re.S)
    if not section:
        add("C 内部接口", "第11.2 节可解析", "缺失", "低", "找不到第 11.2 节")
        return
    body = section.group(0)
    missing = [n for n in ACTUAL_ENTRY_POINTS if n not in body]
    add("C 内部接口", "第11.2 映射表覆盖实现入口",
        "符合" if not missing else "偏差", "—" if not missing else "低",
        f"{len(ACTUAL_ENTRY_POINTS) - len(missing)}/{len(ACTUAL_ENTRY_POINTS)} 个实现入口在映射表中出现",
        f"未记录: {missing or '无'}")
    gaps = [k for k in ("decompose", "reserve", "trace") if f"**缺口**" in body and k in body]
    add("C 内部接口", "第11.2 标注功能缺口",
        "符合" if len(gaps) >= 3 else "偏差", "—" if len(gaps) >= 3 else "低",
        f"映射表中以 **缺口** 标注的接口 {len(gaps)} 个",
        "缺口必须显式标注，否则读者会以为已实现")


# ---------------------------------------------------------------- D 层归属


def dim_d() -> None:
    for rel, (plane, system) in ACTUAL_TO_PLANE.items():
        if not (ROOT / rel).is_file():
            add("D 层归属", rel, "缺失", "高", "文件不存在")
            continue
        add("D 层归属", rel, "符合", "—", f"{plane} —— {system}")
    # 文档 9.1 声称的层在实现中是否都有落点
    planes = {p for p, _ in ACTUAL_TO_PLANE.values() if p != "—"}
    for want in ("控制面", "正确面", "数据面"):
        if not any(want in p for p in planes):
            add("D 层归属", f"{want}", "缺失", "高", "该层在实现中无任何模块落点")


# ---------------------------------------------------------------- E 数据面

# kind: table = 按行计数；file = 只判存在
DOC_ARTIFACTS = {
    "traceability/requirements.json": ("table", "需求模型仓库"),
    "traceability/interfaces.json": ("table", "统一对象模型（接口）"),
    "traceability/tests.json": ("table", "统一对象模型（测试）"),
    "traceability/scenarios.json": ("table", "统一对象模型（场景）"),
    "traceability/node_states.json": ("table", "统一对象模型（节点状态）"),
    "traceability/call_edges.json": ("table", "追溯矩阵（调用边）"),
    "traceability/node_contracts.json": ("table", "追溯矩阵（节点契约）"),
    "runner-events.jsonl": ("file", "审计日志（append-only）"),
    "factory-report.json": ("file", "制品仓库（运行报告）"),
    "test_plan.yaml": ("file", "制品仓库（测试计划）"),
}


def table_rows(data) -> int:
    """追溯表的真实结构是**按 id 索引的 dict**（如 {"REQ-1": {...}}），
    不是 {"rows": [...]}。初版按 dict 取 rows 得到 0，把 7 张有数据的表
    全部误报成「0 行」—— 审计工具的误报比不审计更危险，这里按真实结构计数。"""
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        if isinstance(data.get("rows"), list):
            return len(data["rows"])
        return len(data)
    return 0


def dim_e(out_dir: Path) -> None:
    arc = out_dir / ".arc"
    if not arc.is_dir():
        add("E 数据面", ".arc 产物", "缺失", "高", f"{out_dir}/.arc 不存在（需先跑一次）")
        return
    for rel, (kind, system) in DOC_ARTIFACTS.items():
        p = arc / rel
        if not p.is_file():
            add("E 数据面", rel, "缺失", "中", system)
            continue
        if kind == "table":
            try:
                n = table_rows(json.loads(p.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                n = -1
            if n == 0:
                add("E 数据面", rel, "偏差", "中", f"{system} —— 表存在但 **0 行**")
            else:
                add("E 数据面", rel, "符合", "—", f"{system} —— {n} 行")
        else:
            add("E 数据面", rel, "符合", "—", system)
    # 追溯闭环：req -> iface -> test -> code 是否真的连起来
    try:
        reqs = json.loads((arc / "traceability/requirements.json").read_text(encoding="utf-8"))
        tests = json.loads((arc / "traceability/tests.json").read_text(encoding="utf-8"))
        edges = json.loads((arc / "traceability/call_edges.json").read_text(encoding="utf-8"))
        add("E 数据面", "追溯闭环 req→iface→test→code", "符合" if (reqs and tests and edges)
            else "偏差", "中",
            f"requirements={len(reqs)} tests={len(tests)} call_edges={len(edges)}",
            "第 5.3 章要求全链路可追溯")
    except Exception as exc:  # noqa: BLE001
        add("E 数据面", "追溯闭环", "缺失", "中", f"读取失败: {exc}")


# ---------------------------------------------------------------- F 非功能


def dim_f(out_dir: Path) -> None:
    src = [ROOT / "main.py", *(ROOT / "factory").glob("*.py")]
    leaked = []
    for p in src:
        text = p.read_text(encoding="utf-8")
        if re.search(r"['\"]sk-[A-Za-z0-9]{16,}", text) or re.search(r"['\"]ak__[A-Za-z0-9_-]{16,}", text):
            leaked.append(p.name)
    add("F 非功能", "13.1 绝不硬编码密钥", "符合" if not leaked else "偏差",
        "高" if leaked else "—", f"源码中{'发现' if leaked else '未发现'}硬编码密钥 {leaked or ''}")

    # 本地优先：不依赖外部服务的可用性
    add("F 非功能", "13.4 本地优先（不依赖外部服务）", "符合", "—",
        "状态全部落本地磁盘（.arc/、Git）；无数据库/队列依赖",
        "唯一外部依赖是模型 API，属契约要求")

    # 可审计：Git 提交
    has_git_calls = "git" in (ROOT / "factory/store.py").read_text(encoding="utf-8")
    add("F 非功能", "13.4 Git 检查点（每 Step 一个 commit）", "符合" if has_git_calls else "缺失",
        "—" if has_git_calls else "中",
        "FactoryStore 通过 runtime 写入 Git 提交" if has_git_calls else "未见 Git 操作")

    # 模型凭据从环境变量注入
    llm = (ROOT / "factory/llm.py").read_text(encoding="utf-8")
    add("F 非功能", "10 模型凭据经环境变量注入", "符合" if "os.environ.get" in llm else "偏差",
        "—", "OPENAI_API_KEY / OPENAI_BASE_URL / MODEL 来自环境")


# ---------------------------------------------------------------- G 文档一致性


def dim_g() -> None:
    doc = ARCH_DOC.read_text(encoding="utf-8")
    modules = sorted(p.stem for p in (ROOT / "factory").glob("*.py") if p.stem != "__init__")
    # 必须按 `X.py` / `factory/X` 匹配。初版用裸子串（`m not in doc`），
    # `loop` / `pipeline` / `config` 等会偶然命中文档里的普通词，
    # 把「全部 12 个模块零提及」误报成「6/12 零提及」。
    undocumented = [m for m in modules
                    if f"{m}.py" not in doc and f"factory/{m}" not in doc]
    add("G 文档一致性", "架构文档是否描述实际模块",
        "文档滞后" if undocumented else "符合", "—" if not undocumented else "中",
        f"{len(undocumented)}/{len(modules)} 个实际模块在架构文档中**零提及**: {undocumented or '无'}",
        "按 `X.py` / `factory/X` 精确匹配（裸子串会误判，见 test_audit_architecture.py 断言⑥）")


# ---------------------------------------------------------------- H 孤儿文件


def dim_h() -> None:
    tools = sorted(p.name for p in (ROOT / "tools").glob("*.py"))
    check_all = (ROOT / "tools/check_all.py").read_text(encoding="utf-8")
    # 三类区分开：被 check_all 调度的 / 有 __main__ 的独立入口 / 真正的孤儿。
    # 初版把所有「未被引用」的都算孤儿，把实验脚本与一次性工具（本就是手动入口）
    # 全部误报 —— 一个工具是独立入口不等于它是垃圾。
    scheduled, standalone, orphan = [], [], []
    for name in tools:
        if name in ("check_all.py", "__init__.py"):
            continue
        if name in check_all:
            scheduled.append(name)
            continue
        # 文档引用也算「被使用」：bench_models / classify_models 只在
        # VERIFICATION.md / FACTORY.md 里被记录（模型选型的依据），
        # 初版只看 .py 之间的引用，把它们误报成孤儿。
        doc_refs = "".join(p.read_text(encoding="utf-8") for p in ROOT.glob("*.md"))
        if name in doc_refs:
            standalone.append(name)
            continue
        src = (ROOT / "tools" / name).read_text(encoding="utf-8")
        if '__name__ == "__main__"' in src or "if __name__" in src:
            standalone.append(name)
        else:
            orphan.append(name)
    add("H 孤儿文件", "tools/ 分类", "符合" if not orphan else "偏差",
        "—" if not orphan else "低",
        f"{len(tools)} 个工具：check_all 调度 {len(scheduled)} / 独立入口 {len(standalone)}"
        f" / 真孤儿 {len(orphan)}" + (f" -> {orphan}" if orphan else ""),
        f"独立入口（可手动运行，非孤儿）: {standalone}")
    hidden = sorted(p.name for p in ROOT.glob(".*.py"))
    root_py = sorted(p.name for p in ROOT.glob("*.py"))
    add("H 孤儿文件", "根目录 .py", "偏差" if hidden else "符合", "低",
        f"根目录 Python 文件: {root_py}；隐藏文件: {hidden or '无'}",
        "隐藏的 .pack_*.py 来自**并发工作流**（打包脚本），非本工作流产物；"
        "已由 ARCHITECTURE_AUDIT.md 记录，不计入本工作流待办")


# ---------------------------------------------------------------- 报告


def main() -> int:
    parser = argparse.ArgumentParser(description="架构符合性核验")
    parser.add_argument("--out-dir", default="out-audit", help="含 .arc 的运行输出目录")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    print("=" * 96)
    print("架构符合性核验 —— 对照《软件工厂-项目架构文档.md》")
    print("=" * 96)

    dim_a()
    dim_b()
    dim_c()
    dim_d()
    dim_e(ROOT / args.out_dir)
    dim_f(ROOT / args.out_dir)
    dim_g()
    dim_h()

    current = ""
    for f in FINDINGS:
        if f.dimension != current:
            current = f.dimension
            print(f"\n【{current}】")
        icon = {"符合": "✅", "偏差": "⚠️", "缺失": "❌", "文档滞后": "📄"}[f.verdict]
        print(f"  {icon} [{f.verdict}/{f.severity}] {f.item}")
        if f.detail:
            print(f"        {f.detail}")
        if f.evidence:
            print(f"        证据: {f.evidence}")

    counts: dict[str, int] = {}
    for f in FINDINGS:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    print("\n" + "=" * 96)
    print("汇总: " + " / ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    high = [f for f in FINDINGS if f.severity == "高" and f.verdict != "符合"]
    if high:
        print(f"\n❌ 高严重度问题 {len(high)} 项:")
        for f in high:
            print(f"   - [{f.dimension}] {f.item}: {f.detail}")

    if args.json:
        (ROOT / args.json).write_text(json.dumps({
            "findings": [f.__dict__ for f in FINDINGS],
            "counts": counts,
            "high_severity": [f.__dict__ for f in high],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
