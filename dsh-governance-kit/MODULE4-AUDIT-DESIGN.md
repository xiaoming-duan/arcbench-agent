# 模块 4 设计：审计 Provider

> **状态：设计（未写代码）。** 接口名均经 `cordis_inspect_query` **实查**，非照抄。

---

## 一、为什么需要它（真实动机）

**实测 bug**：分类重试预算**机制在正确工作**（某需求确实消费了 4+3 次），
但账本被门禁段的 `result.gate_audits = {...}` **整体覆盖**，
于是报告里 6 个需求的账本**全部丢失**。

> **没有可观测性时，正确工作的机制与未工作的机制在报告上不可区分。**

只看报告会以为预算没生效；只看日志才知道花了 4+3 次。
若无日志重建，会得出「P0-2 未生效」的**错误结论**。

**这正是模块 4 要解决的**：把「机制」与「可观测性」分开建设，
使二者可以**独立验证**。

---

## 二、DSH 提供的接口（实查结果）

### 2.1 观测发布

```typescript
// Service: inspector
publish(topic: string, payload: InspectorJsonValue, monotonicMs?: number): void
```

**"Publish one JSON observation without waiting for Worker delivery."**

这是 DSH 原生的观测出口。审计事件应发到这里，**而不是**新建事件总线。

`payload` 必须是 `InspectorJsonValue`（纯 JSON）——
意味着审计记录**不能携带 Session 对象或函数**，只能带可序列化快照。

### 2.2 数据来源：四个事件 + 一个服务

| 来源 | 模式 | 用途 | 实查签名要点 |
|---|---|---|---|
| `tools/result` | **emit** | 工具调用结果 | `(exec, result) -> undefined` —— **frozen, lossless-JSON final outcome** |
| `tools/execute` | **waterfall** | 工具调用**前后**（文档明写 "timeout, retry, or **metrics**"） | `(exec, next) -> Promise<ToolExecutionResult>` |
| `agent/error` | emit | 步/轮错误 | `{ agent, turn, step, error }` |
| `agent/status` | emit | 状态变化 | `{ agent, status }`（idle ⇄ running） |
| `tokenMeter.measure(session, requestHeader?)` | Service | **成本** | 返回 `TokenMeasurement` |

**`tools/result` 是最佳的单点钩子** —— 文档明确它给的是
**frozen、lossless-JSON** 的最终结果，正是审计需要的形态。

### 2.3 持久化

```typescript
// Service: storage / storageDomain
storage.mount<K>(form: K, facility: StorageForms[K]): () => void
storageDomain.open<S extends DomainSpec>(spec: S): Promise<Domain<S>>
```

---

## 三、**关键约束**（必须先说，否则设计会走错）

来自 `cordis-plugin-development/references/practices.md`：

> **Do not append session events with a new `type`.**
> Readers accept an unknown stored event only when its envelope carries
> `ignorable: true`, and live `Session.append()` cannot set that marker,
> so the Session would **refuse to reopen**.
>
> Derive state from existing events, or **keep plugin-owned data in a storage
> service found through inspection**.

### 这条约束的后果

```
❌ 错：往 session log 里 append 自建的 audit 事件
       -> 会话无法重开（数据损坏，且是**不可逆**的）

✅ 对：审计数据写入**插件自有的 JSONL**（或 storageDomain）
       只从既有事件**读**
```

**审计必须是「只读既有事件 + 写自有存储」的单向结构。**
这条不满足会让整个 DSH 会话失效 —— 代价远大于审计的价值。

---

## 四、接口设计

### 4.1 四个方法（任务指定）

```typescript
log_step(step: StepRecord): Promise<void>
log_tool_call(tool: string, args: unknown, result: unknown): Promise<void>
log_cost(tokens: number, latencyMs: number): Promise<void>
query(filter: AuditFilter): Promise<AuditEntry[]>
```

### 4.2 记录形状（append-only JSONL）

每行一条 JSON，**只追加、不修改**。字段设计对齐 DSH 的既有标识：

```jsonc
// 一行的形状（JSONL：一行一条，便于流式追加与 grep）
{
  "seq": 42,                          // 插件自有单调序号（非 SessionSeq）
  "ts": "2026-10-03T21:14:07.123Z",   // 墙钟（人读）
  "mono": 123456.789,                 // 单调时钟（算延迟；不受系统时间调整影响）
  "sessionId": "…",                   // DSH SessionId（字符串）
  "agentId": "…",                     // 哪个 Agent（子 Agent 也要能区分）
  "turn": 3,                          // 来自 agent/* 事件
  "step": 7,
  "kind": "tool_call",                // step | tool_call | cost | error | gate

  // kind=tool_call 时：
  "tool": "freeze_contract",
  "argsHash": "sha256:…",             // ★ 存哈希不存原文（见 §5.1）
  "ok": true,
  "durationMs": 12,
  "resultBytes": 348,

  // kind=cost 时：
  "tokensIn": 1200, "tokensOut": 340,
  "latencyMs": 1840, "provider": "…", "model": "…",

  // kind=gate 时（与本内核的契约/UI 门禁对接）：
  "gate": "contract",
  "verdict": "CONTRACT_TAMPERED",
  "evidence": { "expected": "40f4…", "actual": "beaa…" }
}
```

### 4.3 为什么 `mono` 与 `ts` 都存

| 字段 | 用途 | 不能替代对方的原因 |
|---|---|---|
| `ts` | 人读、跨进程对齐 | 可被系统时间调整**回退** |
| `mono` | **算延迟** | 无绝对含义，不能跨进程比较 |

**延迟计算必须用 `mono`** —— 用 `ts` 会在时钟回拨时算出负数。
（`inspector.publish` 的第三参 `monotonicMs` 正是这个语义。）

---

## 五、三个设计决策

### 5.1 参数**存哈希不存原文**

工具参数可能含**凭据、密钥、个人信息**。审计日志会被长期保留、被检索、
可能被导出分析。

```
审计里存：  "argsHash": "sha256:9f2a…"    +  "argKeys": ["dir","req_id","calls"]
审计里不存："args": { "apiKey": "ak__…" }        ← ❌
```

**同时存 `argKeys`（键名列表）** —— 键名通常不含敏感信息，
且能让「调用了什么形状」可查。**哈希保证可比对，键名保证可理解。**

**若确需留存原文**，应显式配置并单独标注，默认**不存**。

### 5.2 `query` 不做全文检索，做**结构化过滤**

```typescript
interface AuditFilter {
  sessionId?: string;
  kind?: 'step' | 'tool_call' | 'cost' | 'error' | 'gate';
  tool?: string;
  gate?: string;
  verdict?: string;
  sinceMono?: number;      // 用 mono 而非 ts（同上）
  limit?: number;          // ★ 必须有上限，见下
}
```

**`limit` 必须有默认值**（建议 200）—— 审计日志会无限增长，
无上限查询会把整个日志读进内存。**这与本内核「误报比没有门禁更糟」同源**：
一个会 OOM 的查询接口会被弃用，弃用后什么可观测性都没有。

### 5.3 append-only 由**打开方式**保证，而非靠约定

```
打开：以 'a'（append）模式，O_APPEND
禁止：任何 seek / truncate / rewrite
轮转：写满 N MB 后**换新文件**（audit-<ts>.jsonl），不重写旧文件
```

**校验**：提供 `verify_append_only()` —— 遍历文件，断言 `seq` 严格递增且无缺口。
**这是把「我们约定只追加」变成「可以验证只追加」。**

> 与 LESSONS 纪律 5 同源：**机制工作 ≠ 可观测**。
> 「append-only」若不经验证，与「有时重写」在读取结果上不可区分。

---

## 六、与既有组件的对接

| 组件 | 关系 |
|---|---|
| 契约冻结 | **生产审计**：`verify_contract` 的 verdict 写入 `kind=gate` 记录 |
| 五道门禁 | **生产审计**：每道门的 `ok`/违规写入 `kind=gate` |
| 分类型重试预算 | **生产审计**：每次 `charge()` 写一条（信号、花费、剩余） |
| UI 门禁 | **生产审计**：元素覆盖映射写入 `evidence` |

**关键**：审计是**下游消费者**，不得反向影响门禁判定。
**审计失败不得阻断主流程**（写日志失败只记 warning）——
否则审计会成为新的故障点，而它的价值远低于主流程。

**但**：审计失败必须**自己可见**（一条 `kind=error` 记录 + 计数），
否则「审计静默失效」与「审计正常」不可区分 —— 又是同一条教训。

---

## 七、验证计划（**按本套件纪律，先写验证再写代码**）

| # | 断言 | 正确样本 | 错误样本 |
|---|---|---|---|
| 1 | append-only | 追加 3 条后 `verify_append_only()` 通过 | 手工改写第 1 行 -> 必须报红 |
| 2 | `seq` 单调无缺口 | 连续 3 条 | 删掉中间一条 -> 必须报红 |
| 3 | 延迟用 `mono` | 时钟回拨后延迟仍为非负 | 若改用 `ts`，构造回拨 -> 必须报负（证明判据有效） |
| 4 | 参数不落原文 | 传含 `apiKey` 的参数 -> 文件中 grep 不到该值 | 若落原文 -> grep 得到（证明判据有效） |
| 5 | `limit` 有默认值 | 不传 limit 时返回 ≤ 默认上限 | 构造 10 万条 -> 不得 OOM |
| 6 | 审计失败不阻断主流程 | 目录只读时主流程仍成功 | —— |
| 7 | 审计失败**可见** | 目录只读时产生 1 条 `kind=error` + 计数 | 若静默 -> 断言失败 |
| 8 | **反向验证** | 破坏 `verify_append_only` 使其恒真 -> 断言 1 的错误样本必须报红 | —— |

**第 8 条不可省** —— 与治理套件 `kit-test.mjs` 的反向验证同源：
**验证器自身也要被验证**（LESSONS 纪律 5）。

---

## 八、明确不做（本设计范围外）

```
不往 session log 追加自建事件    —— 会让会话无法重开（§三）
不做全文检索                     —— 结构化过滤足够，且避免 OOM
不存参数原文（默认）             —— 凭据泄露风险
不影响门禁判定                   —— 审计是下游消费者
不做实时流式推送                 —— 先做可查，流式留待需要时
不替代 DSH 既有 telemetry        —— 本 Provider 面向**治理**（门禁/预算/契约）
                                    而非通用会话遥测
```

---

## 九、实现前置条件

| # | 前置 | 状态 |
|---|---|---|
| 1 | 确认 `inspector.publish` 在目标版本可用 | ✅ 已实查 |
| 2 | 确认 `tools/result` 的 payload 形状 | ⚠️ 需查 `ToolExecution` / `ToolExecutionResult` 类型 |
| 3 | 确认 `tokenMeter.measure` 的返回形状 | ⚠️ 需查 `TokenMeasurement` 类型 |
| 4 | 选定存储：自有 JSONL 还是 `storageDomain` | ⚠️ **设计决策，见下** |

### 存储选型（待定）

| 方案 | 优点 | 缺点 |
|---|---|---|
| **自有 JSONL** | 简单、可 grep、无依赖、易验证 append-only | 不经 DSH 生命周期管理 |
| `storageDomain` | 随 DSH 生命周期、可能支持查询 | API 更重、需先查 `DomainSpec` |

**建议**：先做**自有 JSONL** —— 它让 §七 的 8 条断言都能**独立验证**，
不依赖 DSH 运行时的存储语义。若之后需要 DSH 管理，再加一层适配。

**理由**：审计的第一价值是**可信**，不是**集成度**。
一个能自证 append-only 的 JSONL 文件，比一个集成良好但无法验证的存储更有用。
