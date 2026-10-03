# 架构符合性核验报告

**核验对象**：工作区全部文件
**核验依据**：《软件工厂-项目架构文档.md》（v1.1，1423 行 / 18 章）
**核验工具**：`tools/audit_architecture.py`（可复现，输出 `architecture_audit.json`）
**核验时间**：2026-09-30

> **本报告已更新。** 首次核验（文档 v1.0）发现 14 项偏差 + 1 项文档滞后；
> 据其修订文档（v1.0 → v1.1）后**复验**，结果见下方"修订前后对照"。
> 报告正文保留首次核验的发现（它们记录了问题是怎么被发现的），
> 修订结果集中在第 0 节与第 4 节。

---

## 0. 修订前后对照

| 维度 | 首次核验（文档 v1.0） | 复验（文档 v1.1） |
|---|---|---|
| A 目录结构（第 9 章） | ⚠️ 偏差 10 —— 9 个包目录一个都不存在 | ✅ **符合** —— 文档第 9.1 节的 14 个文件与磁盘 14 个逐一对应 |
| B 平台契约（第 11.1） | ⚠️ 偏差 1 —— 环境变量缺失 4/5 | ✅ **符合** —— 文档 CLI 选项（9 个）与 `main.py` argparse 一致 |
| C 内部接口（第 11.2） | ⚠️ 偏差 1 + 缺失 1 | ✅ **符合** —— 18 个实现入口全部出现在映射表，3 个缺口已标注 |
| D 模块→层归属 | ✅ 符合 13 | ✅ 符合 13 |
| E 数据面产物 | ✅ 符合 11 | ✅ 符合 11 |
| F 非功能性 | ✅ 符合 4 | ✅ 符合 4 |
| G 文档↔实现一致性 | 📄 **文档滞后** —— 12/12 模块零提及 | ✅ **符合** —— 0/12 零提及 |
| H 孤儿文件 | ⚠️ 偏差 2 | ⚠️ 偏差 1（仅剩并发方产物） |
| **合计** | **符合 29 / 偏差 14 / 缺失 1 / 文档滞后 1** | **符合 35 / 偏差 1** |

**唯一剩余偏差**：根目录 `.pack_v1.py` / `.pack_v2.py` —— **来自并发工作流**，非本工作流产物。

---

## 结论速览（首次核验，文档 v1.0）

| 维度 | 符合 | 偏差 | 缺失 | 文档滞后 |
|---|---|---|---|---|
| A 目录结构（第 9 章） | 0 | 10 | 0 | — |
| B 平台契约（第 11.1） | 1 | 1 | 0 | — |
| C 内部接口（第 11.2） | 0 | 1 | 1 | — |
| D 模块→层归属（第 2/9.1） | **13** | 0 | 0 | — |
| E 数据面产物（第 5 章） | **11** | 0 | 0 | — |
| F 非功能性（第 13 章） | **4** | 0 | 0 | — |
| G 文档↔实现一致性 | 0 | 0 | 0 | **1** |
| H 孤儿文件 | 0 | 2 | 0 | — |
| **合计** | **29** | **14** | **1** | **1** |

**一句话**：**实现与"控制面/正确面/数据面"三面架构在语义上自洽，数据面与非功能要求全部达标；
但架构文档第 9 章（目录结构）与第 11 章（接口）描述的是一套从未落地的目标结构，
文档与实现已严重脱节。**

---

## 一、符合的部分（29 项）

### D 模块 → 架构层映射：13/13 全部符合

| 实际模块 | 架构层 | 对应系统（文档 9.1） |
|---|---|---|
| `factory/pipeline.py` | 控制面 | 编排器 / 领域执行引擎 |
| `factory/loop.py` | 控制面 + 正确面 | TDD 修复循环 + 质量门禁 |
| `factory/generator.py` | 控制面 | 智能体池（设计/计划/写测试/实现四角色） |
| `factory/llm.py` | 横向 | 模型路由层 + 成本记录 |
| `factory/config.py` | 横向 | 模型路由层、预算策略 |
| `factory/adapter.py` | 数据面 | 需求模型仓库 + 拓扑排序 |
| `factory/models.py` | 数据面 | 统一对象模型 |
| `factory/store.py` | 数据面 | 追溯矩阵、审计日志、检查点（Git） |
| `factory/workspace.py` | 控制面 + 正确面 | 工作区隔离、制品落盘 |
| `factory/testplan.py` | 正确面 | 测试计划与基线守卫 |
| `factory/testaudit.py` | 正确面 | 验证执行器（import/依赖/mock/注入旁路审计） |
| `factory/testrunner.py` | 正确面 | 验证执行器（vitest / node:test） |
| `factory/__init__.py` | — | 包声明 |

**三个面都有模块落点，无空洞。** 目录名虽与文档不同，但**职责归属与文档 9.1 的映射意图一致**。

### E 数据面产物：11/11 全部符合

文档第 5 章要求的产物全部落盘且**有数据**：

| 产物 | 对应系统（第 5 章） | 行数 |
|---|---|---|
| `traceability/requirements.json` | 5.1 需求模型仓库 | 2 |
| `traceability/interfaces.json` | 5.2 统一对象模型 | 4 |
| `traceability/tests.json` | 5.2 统一对象模型 | 2 |
| `traceability/scenarios.json` | 5.2 统一对象模型 | 2 |
| `traceability/node_states.json` | 5.2 统一对象模型 | 2 |
| `traceability/call_edges.json` | 5.3 追溯矩阵（调用边） | 1 |
| `traceability/node_contracts.json` | 5.3 追溯矩阵（节点契约） | 2 |
| `runner-events.jsonl` | 5.4 审计日志（append-only） | ✅ |
| `factory-report.json` | 5.6 制品仓库 | ✅ |
| `test_plan.yaml` | 5.6 制品仓库 | ✅ |

**追溯闭环 `req → iface → test → code` 成立**（requirements=2 / tests=2 / call_edges=1），
满足第 5.3 章「全链路可追溯」的要求。

> 注：`call_edges` / `node_contracts` 两张表此前长期为 0 行，
> 由**并发工作流**在本轮核验期间补齐（见文末声明）。

### F 非功能性：4/4 全部符合

| 要求（第 13 章） | 核验结果 |
|---|---|
| 13.1 绝不硬编码密钥 | ✅ 源码中未发现任何硬编码密钥 |
| 13.4 本地优先（不依赖外部服务） | ✅ 状态全部落本地磁盘，无数据库/队列依赖 |
| 13.4 Git 检查点 | ✅ `FactoryStore` 通过 runtime 写入提交 |
| 第 10 章 凭据经环境变量注入 | ✅ `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL` |

---

## 二、偏差与缺失（15 项）

### ★ 偏差 1（中）：目录结构完全偏离第 9 章

文档规定 **9 个包目录**，实际**一个都不存在**；实现是 `factory/` 下 **13 个扁平模块**：

```
文档第 9 章要求                    实际实现
factory/                          factory/
├── orchestrator/                 ├── pipeline.py      （编排器）
├── planner/                      ├── loop.py          （TDD 循环）
├── agents/                       ├── generator.py     （四角色）
├── workspace/                    ├── workspace.py
├── verification/                 ├── testaudit.py / testrunner.py / testplan.py
├── integration/                  ├── （并入 pipeline.py + workspace.py）
├── requirements/                 ├── adapter.py
├── artifacts/                    ├── store.py
├── config/                       ├── config.py
└── main.py                       └── （在仓库根，非 factory/ 内）
```

**判定**：这是**有意的简化**，不是缺陷 —— 各模块的职责与文档 9.1 的映射意图一致（见 D 维度 13/13）。
但文档未记录这一决策，读者按第 9 章去目录里找会一无所获。

### ★ 偏差 2（中）：平台契约与第 11.1 不符

文档 11.1 要求 5 个环境变量：`REQUIREMENT_PATH` / `OUTPUT_PATH` / `API_KEY` / `BUDGET_LIMIT` / `VALIDATION_TEST_PATH`。
实际**缺失 4/5**（仅 `OPENAI_API_KEY` 语义相近），改为命令行参数：

```bash
python3 main.py <req_dir> --output-dir <out> --type web
```

**判定**：**实现是对的，文档是错的。** 实际契约与 ARC-Bench Agent Starter 平台模板一致，
是被平台 Runner 真正调用的形式；第 11.1 描述的是另一套从未落地的方案。

### ★ 偏差 3（低）+ 缺失 1（低）：内部接口命名

文档 11.2 命名 **15 个接口**，同名实现仅 **1 个**（`snapshot`，且含义不同）。但按**职责**核对：

| 文档接口 | 实现中的职责对应 |
|---|---|
| `submit_goal` | `run_factory()` |
| `next_task` | `_topological_order()` + pipeline 逐需求循环 |
| `run_phase` / `attempt_step` | `TddLoop.run()` 内的 RED→实现→GREEN |
| `decompose` | 适配层 `dependencies` + 拓扑排序（**有排序，无 DAG 分解**） |
| `dispatch` | `Generator` Protocol 四角色 |
| `settle` | `CallStats.delta()` |
| `generate_tests` | `LLMGenerator.plan_tests()` + `write_tests()` |
| `evaluate` | `_is_weak()` / `_enforce_no_weakening()` / 依赖门禁 / `_is_uncollectable()` |
| `restore` | 回归检查回退（`IMPLEMENTATION_REGRESSION` → 回退上一版实现） |
| `append` / `link` | `FactoryStore.record_*` → `.arc/` |
| **`reserve`** | **无** —— 只有事后成本记账，无预算预留 |
| **`trace`** | **无** —— 追溯只写不查 |

**判定**：命名整体不同属文档滞后；`reserve` / `trace` 是**真实的功能缺口**。

### 偏差 4（中）：架构文档严重滞后

**12/12 个实际模块在架构文档中零提及**（按 `X.py` / `factory/X` 精确匹配）。

文档第 9 章详述的 9 个包目录在实现中不存在；实际模块在文档中无任何记录。
**文档描述的是一个从未构建的目标，而不是这个系统。**

### 偏差 5（低）：孤儿文件

| 文件 | 判定 |
|---|---|
| `tools/anchored_edit.py` | **真孤儿** —— 被 heredoc 内联补丁脚本使用，但无任何文件或文档引用它。属**文档缺口**（本会话的改动全部经它落地，却无处可查） |
| 根目录 `.pack_v1.py` / `.pack_v2.py` | 来自**并发工作流**（打包脚本） |

其余 20 个 `tools/*.py` 分类正常：8 个由 `check_all` 调度，11 个是有 `__main__` 的独立入口。

---

## 三、核验工具自身的 4 处误报（已修）

**核验工具交付的是「符合/偏差」判断，误报比不审计更危险** —— 它会让正确的实现看起来不合格。
初版有 4 处误报，全部已修并加断言（`tools/test_audit_architecture.py`，9 项）：

| 误报 | 真因 | 修正 |
|---|---|---|
| 7 张追溯表报「0 行」 | 表是**按 id 索引的 dict**（`{"REQ-1": {...}}`），不是 `{"rows": [...]}`；我按 `rows` 取值得到 0 | `table_rows()` 按真实结构计数 |
| 「6/12 模块零提及」 | 用裸子串匹配，`loop`/`pipeline`/`config` 偶然命中文档里的普通词 | 改为 `X.py` / `factory/X` 精确匹配 → 真实结果是 **12/12** |
| 12 个工具报「孤儿」 | 把「未被 .py 引用」等同于孤儿，但**独立入口与文档记录都算被使用** | 分三类：`check_all` 调度 / 独立入口 / 真孤儿 |
| `factory-report.json` 按行计数 | 它是 10 个键的配置对象，不是表格 | 新增 `kind` 区分 table / file |

---

## 四、建议与执行结果

| 优先级 | 动作 | 状态 |
|---|---|---|
| **高** | 把第 9 章改写为实际结构，并记录"扁平模块是有意简化"这一决策 | ✅ **已执行**（第 9 章重写为 9.1 实际结构 / 9.2 层映射 / 9.3 与设计目标的差距） |
| **高** | 修正第 11.1，改为真实的 CLI 契约 | ✅ **已执行**（原环境变量方案标注为未落地） |
| 中 | 第 11.2 标注"接口按职责对应，命名不同" | ✅ **已执行**（改为「设计接口 → 实现对应」两栏对照，18 个实现入口全部登记） |
| 中 | 补 `reserve`（预算预留）与 `trace`（追溯查询） | ⏳ **未执行** —— 已作为**功能缺口**标注在第 11.2 与第 18.4 节，待后续迭代 |
| 低 | 在 `FACTORY.md` 记录 `anchored_edit.py` | ✅ **已执行**（FACTORY.md 第 3 节重写，列出全部 21 个工具及各自职责） |
| 低 | 清理根目录 `.pack_*.py`（并发方产物） | ⏳ **未执行** —— 非本工作流产物，不擅自删除 |
| — | 新增第 18 章记录核验结果与缺口 | ✅ **已执行** |

**附带完成的文档修复**（核验过程中发现 `FACTORY.md` 同样滞后）：

| 章节 | 原状 | 修订 |
|---|---|---|
| FACTORY.md 第 3 节 | 漏记 3 个模块（testplan / testaudit / \_\_init\_\_），工具只列 2/21 | 重写：13 个模块 + 21 个工具分类列出 |
| FACTORY.md 第 8 节 | 标题「两道真实生效的门禁」 | 扩充为**8 道门禁总览表** + 2 条护栏 + 1 条调度规则 |

---

## 五、⚠️ 工作区并发修改声明

核验期间（01:08 前后）工作区**仍被另一条工作流并发修改**，证据：

- `factory/{store,generator,llm,pipeline,loop,testaudit}.py` 的 mtime 落在核验窗口内
- 新增 `tools/test_call_edges.py`（11 项）、`sdk_contract_report.json`
- `tools/test_gates.py` 断言数由 67 → 92 → 102 → **115** 持续增长
- 新增 `.zipv/`（2.2M 验证快照）与 `.pack_v1.py` / `.pack_v2.py`

**已用 `tools/workstream.py` 做隔离**：快照 `.workstreams/v2-verified`（103 文件），
与工作区逐文件 sha256 一致；后续测量可用
`python3 tools/workstream.py guard --label v2-verified -- <命令>` 跑在冻结副本上，不受并发影响。

**本报告的核验结果对应核验时刻的磁盘状态**，此后并发方的改动不在其中。

---

## 六、复现

```bash
export PYTHONPATH=arcbench-agent-runtime/src:.
python3 main.py requirements_sample --output-dir out-audit --type web --install-deps never
python3 tools/audit_architecture.py --json architecture_audit.json
python3 tools/test_audit_architecture.py     # 核验工具自身的 10 项断言
python3 tools/check_all.py                   # 全量 547 项（23 类）
# 注：本行初稿写「9 项断言」「216 项（8 类）」，是 2026-09-30 的值；
#     2026-10-03 复核后更新。
```
