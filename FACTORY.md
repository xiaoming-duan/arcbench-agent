# 软件工厂 MVP —— 实施说明

在 **ARC-Bench 空白 Agent 模板**（`agent-blank-based (1).zip`）上实现的软件工厂**最小可用闭环**。

- 设计依据：[软件工厂-项目架构文档.md](软件工厂-项目架构文档.md)
- 真实验证证据与缺陷清单：[VERIFICATION.md](VERIFICATION.md)
- 重写反馈 A/B 度量实验（含门禁漏洞发现）：[EXPERIMENT.md](EXPERIMENT.md)
- 门禁修订实验 A/C 三组对照：[EXPERIMENT2.md](EXPERIMENT2.md)
- 架构文档第 17 章记录了本轮的全部过程发现与对第 4/7 章的修订：
  [软件工厂-项目架构文档.md](软件工厂-项目架构文档.md#17-过程发现门禁的度量与修订)

---

## 1. 快速开始

```bash
pip install -r requirements.txt

# A. 桩模式：不调模型，用 fixture 打通链路（确定性，适合 CI）
python3 main.py requirements_sample --output-dir out --type web --install-deps never

# B. 模型模式：接真实模型（需 runner 注入凭据）
export OPENAI_API_KEY=...        # 必填
export OPENAI_BASE_URL=...       # 必填（OpenAI 兼容端点，含 /v1）
export MODEL=...                 # 必填
python3 main.py requirements_sample --output-dir out --type web --generator llm

# 只看解析与入库
python3 main.py requirements_sample --output-dir out --dry-run
```

平台契约不变：

```bash
python3 main.py /path/to/requirements --output-dir /path/to/output --type web
```

---

## 2. 流水线（对应架构文档"五个车间"）

```text
需求目录 requirements.yaml
   │
   ├─[车间1 需求解析]  adapter._adapt()          → RequirementSet（已拓扑排序）
   │
   ├─[集成准备]        workspace.copy_template   → output_dir 成为工程根
   │                   store.ensure_repo         → git init + .gitignore
   │                   store.record_requirement_tree → .arc/traceability
   │
   └─ 逐需求循环（控制面 loop.TddLoop）
        ├─ 设计        generator.design()        → 接口/测试入库 → DESIGNED → commit
        ├─ ★计划门禁    generator.plan_tests()   → 白名单 → .arc/test_plan.yaml
        ├─ 写测试      generator.write_tests()   → 白名单过滤后落盘
        ├─ ★RED 门禁   逐文件判定：整组通过 或 任一文件未引用实现 → WEAK_TEST
        ├─ 实现        generator.implement()     → 落盘（护栏拦截测试文件写入）
        ├─ ★GREEN 门禁 跑测试，通过则 PASSED
        └─ 修复循环    ≤ max_repairs 轮，带结构化失败反馈
```

---

## 3. 目录结构

```text
main.py                       平台 Runner 契约入口（CLI）

factory/                      扁平模块，无子包（13 个）
├── __init__.py
├── config.py       运行配置（全部可用环境变量 / 命令行覆盖）
├── models.py       内部规范化模型（统一对象模型）
├── adapter.py      ★ 需求适配层 —— 唯一与平台原始格式耦合的地方 + 拓扑排序
├── llm.py          模型接入（SDK / 标准库双后端 + 重试降级）+ 成本记账 CallStats
├── generator.py    生成器：LLMGenerator（生产）/ StubGenerator（桩）；四角色 Protocol
├── workspace.py    模板复制、文件落盘、需求加载
├── store.py        数据面桥接（复用 ARC-Bench SDK）
├── testplan.py     正确面：测试计划、基线守卫、间接/被 mock 依赖声明
├── testaudit.py    正确面：验证执行器（import 审计 / 依赖使用 / mock / 注入旁路）
├── testrunner.py   正确面：测试执行器（vitest / node 可插拔）
├── loop.py         控制面：单需求 TDD 循环 + 门禁 + 篡改护栏（最大模块）
└── pipeline.py     顶层编排

requirements_sample/          合成样例需求（推断格式）
└── fixtures/<REQ-ID>/
    ├── tests/**              写测试阶段落盘
    ├── impl/**               实现阶段落盘
    └── patches.yaml          插入式补丁（幂等）

tools/                        验证、实验与运维工具（21 个）
├── check_all.py              ★ 统一断言入口（23 类 / 547 项），关键改动后必跑
│                              注：初稿写「8 类 / 216 项」（2026-09-30 值）
├── anchored_edit.py          ★ 锚点式安全编辑 replace_once()（所有代码改动的落地通道）
├── verify_changes.py         声称的改动是否真的在代码里
├── test_gates.py             门禁行为端到端断言
├── test_measures.py          度量函数审计（measure_source / audit_imports / …）
├── test_dependency_audit.py  依赖使用审计断言
├── verify_d10.py             D10 修复验证（可证伪：复算旧判定）
├── test_call_edges.py        call edge 方向 + node contract 落盘断言
├── test_workstream.py        工作流隔离断言
├── test_audit_architecture.py 架构核验工具断言
├── audit_architecture.py     ★ 架构符合性核验（对照架构文档逐维度检查）
├── workstream.py             ★ 工作流隔离（冻结快照 / 漂移检测 / guard 受控运行）
├── closure.py                传递闭包分析（需求列表 / 拓扑深度 / 枢纽 / 悬空依赖）
├── pre_run_check.py          跑前检查（网关健康 / 字段可记录 / 拓扑 / 悬空依赖）
└── experiment_*.py           四组配对对照实验（gates / weak_feedback / dependency / plan2 / e13）
```

> **`anchored_edit.py` 是全部代码改动的落地通道**：本项目的改动一律经
> `replace_once(file, old, new)` 执行，锚点不匹配或匹配多处即抛 `AnchorError`，
> 杜绝「patch 静默失败」。它没有 `__main__`，由改动脚本 import 使用。

---

## 4. ★ 适配层替换契约（最重要）

真实 `requirements.yaml` 格式到位后，**只改 `factory/adapter.py` 的 `_adapt()` 一个函数**：

```python
def _adapt(raw, *, source: Path) -> RequirementSet:
    ...
```

- 输入：平台需求文件解析后的原始结构（dict / list）
- 输出：`RequirementSet`（见 `factory/models.py`）
- **下游全部不动**：generator / loop / store / testrunner / pipeline 只认 `RequirementSet`

适配层已内置容错：字段别名表（`id`/`req_id`/`requirement_id`、`title`/`name`、`depends_on`/`dependencies` 等自动归一）、
根结构三种形态、需求树嵌套/扁平两种、依赖自动拓扑排序（有环告警）。

修改建议：先跑 `--dry-run`，日志会打印解析出的需求数与项目名。

---

## 5. 模型接入

### 5.1 双后端

| 后端 | 触发条件 | 说明 |
|---|---|---|
| `openai-sdk` | `openai` 可导入 | 优先 |
| `stdlib-http` | 否则 | 仅用标准库 `urllib` 调 OpenAI 兼容 `/chat/completions` |

**Agent 运行环境不保证能装上 `openai` 包**，因此模型路径不应依赖它。实测在无 SDK 环境下模型链路完全可用。

### 5.2 已内置的服务端差异降级

真实网关的兼容性差异都已自动处理，不需要改代码：

| 服务端行为 | 客户端应对 |
|---|---|
| 不支持 `response_format` | 去掉后重试 |
| 不支持 `max_tokens` | 改 `max_completion_tokens` 重试 |
| 只接受 `temperature=1`（如 `kimi-*`） | 自动改为 1 重试 |
| 推理占满预算、正文为空（`finish_reason=length`） | **自动翻倍 `max_tokens`（上限 8000）重试** |
| 网络错误 / 5xx / 429 | 重试 3 次，指数退避 |
| JSON 被截断或带围栏 | 剥离围栏 + 重试 |

### 5.3 调参经验（实测）

- **`max_tokens` 是延迟主因**：同一调用 1200 约 36s，3000 会 >300s 超时。默认已降到 1500。
- **但调太小会空响应**：复杂任务下模型长推理会吃光预算 → 已用自适应放大兜住。
- **网关延迟方差极大**（36s ~ >600s）且会直接断连，单次调用超时必须设上界（`FACTORY_MODEL_TIMEOUT`）。
- 选型请先跑 `tools/classify_models.py` 与 `tools/bench_models.py`，不要凭模型名猜。

---

## 6. 测试方言

| 方言 | 命令 | 用途 |
|---|---|---|
| `vitest` | `backend/node_modules/.bin/vitest run <files>` | **生产路径**，模板自带 |
| `node` | `node --test --test-reporter=tap <files>` | **降级路径**，Node ≥18 内置，零依赖 |

`--test-dialect auto`（默认）：有 vitest 用 vitest，否则尝试安装依赖，仍失败则降级到 node。

两条路径**都已实跑验证通过**（vitest 见 [VERIFICATION.md](VERIFICATION.md) 第 4 节）。

> 沙箱/只读 HOME 环境下 npm 默认缓存不可写、`node-gyp` 也会失败。
> 用 `FACTORY_NPM_CACHE` 重定向缓存目录，并把 `XDG_CACHE_HOME` 指向可写位置。

---

## 7. 生成器：LLM vs 桩

| | LLMGenerator | StubGenerator |
|---|---|---|
| 触发 | `OPENAI_API_KEY` 存在（`--generator llm`） | 无凭据时的默认 |
| 能力 | 真实设计 / 写测试 / 写实现，支持修复轮次 | **不做生成**，从 `fixtures/` 原样落盘 |
| 用途 | 生产 | 证明流水线连通（确定性、可 CI） |

桩生成器**不是**代码生成器。它存在的唯一理由是：在无模型凭据、无网络的环境下，
仍能真实验证 RED→GREEN、traceability 与 git 提交这三件事是真的在工作。

> 桩的 fixture 是 `node` 测试方言；`vitest` 环境下会打印警告且不会被执行。

---

## 8. 全部真实生效的门禁

> 本节于 v1.1 扩充。原题为「两道真实生效的门禁」（RED + 白名单），
> 此后陆续加入了测试可执行性、弱化守卫、依赖使用、mock 与注入旁路等判定，
> 门禁已是 **8 道**，见下表。

### 8.0 门禁总览

| # | 门禁 | 位置 | 判定 | 阻断后 |
|---|---|---|---|---|
| 1 | **RED 门禁**（防假绿） | `loop._is_weak` | 有意义文件是否真的先失败 | 需求 FAILED |
| 2 | **测试可执行性**（TEST_BROKEN） | `loop._is_uncollectable` | 0 个测试被发现 = 测试自己坏了 | **配对回退到写测试阶段** |
| 3 | **路径白名单**（A） | `loop._enforce_test_whitelist` | 只允许计划内测试文件 | 拒收并回传理由 |
| 4 | **弱化守卫** | `loop._enforce_no_weakening` | 有意义文件的断言/用例数不得减少 | 拒收并回传理由 |
| 5 | **测试计划门禁** | `testplan.validate_requirement_plan` | 计划自身 7 条规则 | 重出计划 |
| 6 | **依赖使用门禁**（方案1） | `testaudit.audit_dependency_usage` | 声明依赖必须被**真实调用** | 拒收并回传理由 |
| 7 | **未声明 mock 门禁**（方案2-A） | `testaudit.audit_mocked_dependencies` | 测试不得 mock 未声明的上游 | 拒收并回传理由 |
| 8 | **注入旁路检测**（方案2-B） | `testaudit.audit_injection_bypass` | 形参守卫式旁路 | **仅警告**（`FACTORY_BYPASS_BLOCK=1` 可升为阻断） |

另有两条**非门禁的护栏**：实现阶段禁止改测试（`_guard_implementation_files`）、
重写后回归检查（`IMPLEMENTATION_REGRESSION` → 回退上一版实现并继续）。
以及一条**调度层**规则：上游未通过则下游 `UPSTREAM_FAILED`，不进 TDD 循环、不计入通过率分母。

### 8.1 RED 门禁（防假绿）

实现前先跑测试，必须失败。不失败说明测试没覆盖新行为 → 判 `WEAK_TEST`，
先**回退到写测试阶段重写**（≤ `max_test_rewrites` 次），仍无效则该需求直接 `FAILED`、
**不进入实现**。并计入总判定：`ok = (failed == 0 and passed > 0 and weak == 0)`。

这不是形式主义。实测中 LLM 生成过这样的测试：**在测试文件内自造 mock 并断言自己的假数据，
从不 import 真实实现**——无论实现是否存在都会通过。

**判定口径（已修订）**：不再只看整组退出码，而是

```
整组通过              -> WEAK_TEST
否则任一文件未引用实现  -> WEAK_TEST   （C：audit_imports 逐文件审计）
```

修复前的漏洞是"整组有没有失败"——模型会**新增一个会失败的测试文件**而把空转文件
留在原地（实测 15/15 次走这条路，种子 0/15 被修好）。修订后：

| 处理组 | 严格口径通过 | 静默假通过 |
|---|---|---|
| 修订前（control） | 0/8 | **8/8** |
| A（白名单） | **7/8** | **0/8** |
| A + C | **7/8** | **0/8** |

细节见 [EXPERIMENT2.md](EXPERIMENT2.md)。

### 8.2 测试计划门禁 + 路径白名单（A）

**先有计划，才有测试文件**。计划契约见 [schemas/test_plan.schema.yaml](schemas/test_plan.schema.yaml)，
在写测试之前产出并校验（7 条规则）；校验不过则带反馈重试，耗尽即阻断。

计划里的 `test_files[].path` 就是白名单：

- 写测试 / 重写阶段**只允许写这些路径**，计划外的测试文件一律拒绝，记 `UNAUTHORIZED_TEST_FILE`；
- 运行集合由计划派生，不再由模型的输出决定。

同步监控的其他逃逸路径：拆分文件（被白名单拒）、删除/弱化断言。

**弱化守卫的判定标准（双信号合取）**：

```text
有意义 = 单独跑时失败(RED)  AND  import 了真实实现
无意义 = 单独跑通过          OR   没有 import 实现

有意义的文件 -> 只允许追加/修改，断言数/用例数下降即拒（ASSERTION_DELETION）
无意义的文件 -> 允许整体重写（其断言本就无价值，删掉正是所需）
```

判定**完全复用 C 的 `audit_imports()` 产出**，不引入新检测器：
import 信号来自审计，RED 信号由逐文件单独执行补齐。

> 为什么必须是合取：只 import 实现但单独跑就通过的文件（什么也没断言住）
> 不应被保护，否则修复它时会被守卫自己拦住。

> **关键经验**：白名单只做拒绝是**无效的**。必须把拒绝理由回传给模型
> （"你新增的文件被拒了，只允许改这些文件"），模型才会转为原地修复。
> 补上这个闭环后严格口径从 0/8 升到 7/8。

### 8.3 成本记账：环境失败不能混进逻辑失败

网关延迟方差极大（同一调用 36s ~ >600s，且会直接断连）。若把网关重试混进
"重写轮次"，就会把**环境失败误判为逻辑失败**。因此分开计数：

| 计数器 | 位置 | 含义 |
|---|---|---|
| `calls` / `prompt_tokens` / `completion_tokens` / `total_tokens` / `reasoning_tokens` | `llm.py` `CallStats` | 每轮模型调用累计，写入 `.arc/factory-report.json` 的 `cost` 字段 |
| `gateway_retries` | 同上 | 网络错误、5xx/429、超时、服务端差异降级、空正文重试 |
| `rewrite_rounds` | `RequirementResult.test_rewrites` | 门禁判定触发的**逻辑**重写，与上面完全独立 |

### 8.4 实现篡改护栏（防绕过）

实现阶段如果产出 `backend/tests/**` 下的文件，一律拦截并告警。
实测中模型确实试图在修复轮次新建测试文件来回避失败，护栏生效。

---

## 9. 已实现 vs 未实现（对照架构文档）

### 已实现

| 层 | 系统 | 落地位置 |
|---|---|---|
| 控制面 | 编排器（顺序派发、DAG 顺序） | `pipeline.py` |
| 控制面 | 任务分解 / 依赖解析 | `adapter._topological_order` |
| 控制面 | 智能体池（角色映射到生成器方法） | `generator.py` |
| 正确面 | 测试生成 | `generator.write_tests` |
| 正确面 | 验证执行器（结构化失败反馈） | `testrunner.py` |
| 正确面 | **质量门禁（RED/GREEN 不可跳过 + WEAK_TEST 判定）** | `loop.py` |
| 数据面 | 审计日志 | SDK `events` → `.arc/runner-events.jsonl` |
| 数据面 | 统一对象模型 + 追溯矩阵 | SDK `traceability` → `.arc/traceability/*.json` |
| 数据面 | 检查点与版本管理 | SDK `git` → 每阶段一个 commit |

### 未实现（后续迭代）

- Phase 八步 V 模型（当前是精简的 设计→测试→实现 三步）
- **Ticket 系统**（门禁失败只记 FAILED，未生成 Ticket 与配对回退）
- **独立的测试正确性验证**（当前只判断"是否先失败"，不判断"测试是否测了真东西"）
- Reviewer 独立审查（信息隔离 + 跨 vendor）
- 成本与预算控制器
- 并行调度
- 多模态需求理解（参考图 → UI）

---

## 10. 已知限制

1. **脏工作区会污染结果**：`copy_template_contents` 不清空 `output_dir`，重复运行时上次生成的文件仍在。
   - 不会静默通过：RED 门禁会判 `WEAK_TEST` 并使整体 `ok=false`。
   - 建议每次使用全新 `output_dir`（ARC-Bench 本身就是这么做的）。
2. **合成需求格式是推断的**，不是官方 schema。见第 4 节替换契约。
3. **网关稳定性不在我们控制内**：同配置多次运行，结局可能是 2/2 通过，也可能 2/2 因断连失败。
   已做的应对见第 5.2 / 5.3 节。评估成功率时必须计入这一项。
4. `openai` SDK 未安装，`openai-sdk` 后端代码路径**未实跑**；实跑的是 `stdlib-http` 后端。

---

## 11. 常用开关

| 命令行 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `--requirements-file` | `FACTORY_REQUIREMENTS_FILE` | 自动探测 | 显式指定需求文件名 |
| `--generator` | `FACTORY_GENERATOR` | `auto` | `auto`/`llm`/`stub` |
| `--test-dialect` | `FACTORY_TEST_DIALECT` | `auto` | `auto`/`vitest`/`node` |
| `--max-repairs` | `FACTORY_MAX_REPAIRS` | `2` | 每需求最大修复轮次 |
| `--install-deps` | `FACTORY_INSTALL_DEPS` | `auto` | `auto`/`always`/`never` |
| `--dry-run` | `FACTORY_DRY_RUN` | 关 | 只解析与入库 |
| `--quiet` | `FACTORY_VERBOSE=0` | 关 | 降日志级别 |
| — | `FACTORY_MAX_TOKENS` | `1500` | 单次生成预算（调大显著变慢） |
| — | `FACTORY_MODEL_TIMEOUT` | `180` | 单次模型调用超时（秒） |
| — | `FACTORY_TEMPERATURE` | `0.2` | 采样温度 |
| — | `FACTORY_NPM_CACHE` | npm 默认 | npm 缓存目录（受限环境必须重定向） |
| — | `FACTORY_MAX_TEST_REWRITES` | `2` | WEAK_TEST 回退重写测试的次数上限 |
| — | `FACTORY_WEAK_FEEDBACK` | `import_aware` | 重写反馈形态：`basic` / `import_aware`（A/B 实验用） |
| — | `FACTORY_IMPL_ROOT` | `backend/src` | 实现代码根目录，供测试 import 审计使用 |
| — | `FACTORY_WHITELIST` | `1` | A：是否强制测试路径白名单 |
| — | `FACTORY_AUDIT_GATE` | `1` | C：是否把 import 审计纳入 RED 门禁 |
| — | `FACTORY_IMPORT_ALIASES` | `{}` | 路径别名解析规则，如 `{"@/":"backend/src/"}` |
| — | `FACTORY_BACKEND_DIR` | `backend` | 后端目录名 |

模型凭据（由 runner 注入）：`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`MODEL`。
