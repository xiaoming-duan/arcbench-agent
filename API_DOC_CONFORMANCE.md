# 官方 API 文档一致性核验

对照来源：<https://arc-bench.com/api-doc>（SPA，正文在 `/assets/index-*.js` 里；已抓取解析，缓存于 `.apidoc/`，不进包）。

核验时间：2026-09-30

---

## 1. 文档结构（英文正文）

| 小节 | 要点 |
|---|---|
| Execution Flow | 「Your agent should call the built-in `arcbench_agent_runtime` package. The SDK owns the fixed event protocol, appends `.arc/runner-events.jsonl`, writes keyed JSON tables under `.arc/traceability/`, and lets the backend drive frontend refreshes by reading those tables.」 |
| Upload Entry Contract | 「Every uploaded agent is started with the same **fixed CLI shape**. Python submissions use `main.py` and `requirements.txt`. JavaScript use `index.js` + `package.json`. TypeScript use `index.ts` + `package.json`. When `package.json` exists, the runner installs Node dependencies before invoking the agent.」 |
| What The Frontend Does | 三行映射：Mark a node state → 读 runner event 行 → 需求树/状态面板更新；Write traceability data → 解析事件或刷新信号 → 可追溯面板重载；Create/rollback git commits → 触发提交历史与预览刷新 → 提交列表与预览快照更新 |
| Usage Rule | 「**Do not construct event payloads manually. Call high-level SDK methods only.** Event `type`, refresh flags, and file formats are fixed by the runtime package.」 |
| Node State APIs | 9 个 `mark_*` |
| Core APIs | 6 组共 23 个方法 |
| Automatic Refresh | 「These APIs write the live database, **atomically refresh `.arc/traceability/*.json`**, and **append fixed-format events into `runner-events.jsonl`**.」 |
| Git APIs | 10 个方法 |
| Manual File Sync Case | 只有在**绕过 SDK 手动写** git 状态或 traceability 产物时，才需要 `notify_commit_history_changed(...)` / `notify_traceability_changed(...)` |

另：平台执行流程分三段 —— `deploy_agent`（拉容器、备工作区、装依赖）、`start_agent`（「Execute the agent until it finishes the task and **exits cleanly**.」）、`run_tests`（对产出执行基准测试套件）。

---

## 2. API 覆盖：44/44 全部具备

用 `hasattr` 逐个核对本仓库自带的 SDK（`arcbench-agent-runtime`）：

```
events          文档 11 个 -> 缺失 0  ✅
traceability    文档 23 个 -> 缺失 0  ✅
git             文档 10 个 -> 缺失 0  ✅
paths.project_dir 存在      ✅
```

文档中还出现过 `from arcbench_agent_runtime import AgentRun` 的片段，经核验是**抓取截断的假象**——bundle 中不存在 `AgentRun`，只有 `AgentRuntime`，与本实现一致。

---

## 3. 逐条对照本实现

| 文档要求 | 本实现 | 结论 |
|---|---|---|
| 必须调用内置 `arcbench_agent_runtime` | `main.py` 用 `AgentRuntime.from_env(project_dir=...)`，经 `FactoryStore` 全面接入 | ✅ |
| **不得手写 event payload，只用高层方法** | 早前几轮的任务模板反复要求 `events.log("agent.failed", {...})`；实测该模块**无 `log()`**，我们拒绝并改用真实的 `mark_run_failed(message)` | ✅ 与文档一致 |
| `type` / refresh flags / 文件格式由 runtime 固定 | 全程只调高层方法；`describe`/`snapshot` 只读，不改文件格式 | ✅ |
| 追加快照表 + 事件由 SDK 负责 | 实测：`mark_*` 同时写事件流与快照表（`runtime.py:34` 的 writer 钩子）；`upsert_*` 追加 `signal` 事件 | ✅ |
| 手动写文件时才需 `notify_*` | 我们从不绕过 SDK，因此从不调 `notify_*`；`commit()` 自带刷新信号（实测 5 条 `git_commit` 均 `commit_history=True, preview=True`） | ✅ |
| Python 入口 = 根目录 `main.py` + `requirements.txt` | 包根正是这两个；**根目录无 `index.js` / `index.ts` / `package.json`**，不会触发 Node 依赖安装分支 | ✅ |
| `runtime.paths.project_dir` 是写文件的根 | `main.py` 显式传 `project_dir=str(output_dir)`，实测与 `--output-dir` 解析结果相等 | ✅ |
| 三段执行流程，agent 需「exits cleanly」 | 见下节 | ✅（已按此修正） |

---

## 4. 该文档**没有**规定的事（据此校正我此前的判断）

| 项 | 文档 | 影响 |
|---|---|---|
| **退出码语义** | api-doc **完全不提退出码**；只有流程描述里的 "exits cleanly"。starter README 才写了 "exit with code 0 when finished" | 我此前把「未全通过就 `exit 2`」改成「完成即 0」，与此不冲突，且 "exits cleanly" 支持这一改动 |
| CLI 具体参数 | 只说 "fixed CLI shape"，**未列 `--output-dir` / `--type`**（全文 `--output-dir` 出现 0 次） | 参数形态以 starter README 与平台实际调用为准。平台实际调用**不传 `--type`**，我们靠默认值兜住 |
| 超时 / 预算 / token 上限 | api-doc 区域内 `timeout`/`seconds`/`max_tokens`/`budget` 均无实质规定 | 工厂内部的 `FACTORY_MAX_TOKENS`(1500) 等仍是本地权衡，不受平台文档约束 |
| node state 合法值清单 | 只给了示例 `upsert_node_state("REQ-1", "CONVERGED")` | 9 值枚举仍以 `skills/arcbench-traceability/references/schema.md` 为准 |

---

## 5. 结论

**本实现与官方 API 文档完全吻合，无需改动。** 44 个文档 API 全部具备；唯一那条硬性规则（不得手写 event payload）正是本实现一路坚持的做法。

此前几轮基于「平台契约」的修复，事后看方向都正确：

1. SDK 接入（`AgentRuntime.from_env` + 高层方法）
2. 拒绝 `events.log(...)` 这类**不存在**的 API
3. 逐条 `upsert_*` + `insert_call_edge`(source=调用方) + `upsert_node_contract`，7 张表全非空
4. 退出码「完成即 0」
5. RED 门禁：缺实现模块导致的 0 个测试 = 有效 RED，不是 TEST_BROKEN

仍未被官方文档裁决的一条：`insert_call_edge` 的 `source_req_id`/`target_req_id` 方向语义。api-doc 只列方法名与用途（「Node status and links」），未定义方向；我们采用 `source=调用方`（依赖方），并已用断言锁死。
