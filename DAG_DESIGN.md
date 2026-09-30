# 多模块 DAG 设计

**状态**：纯设计，未实现、未运行
**依据**：三处源码实读 + 三个实测实验（见 §2、§3）
**结论先行**：**用户列的四项要求里有三项已经满足**，真正需要做的不是"实现一个 DAG"，
而是补两个**静默失败**的缺口（悬空依赖、环），并把散落在两处的图逻辑收敛成一个可测的层。

---

## §1 读取位置

| 项 | 位置 | 内容 |
|---|---|---|
| **拓扑序执行** | `factory/adapter.py:364` `_topological_order()`，`:336` 调用 | 递归 DFS；稳定排序；环时退回原顺序并告警 |
| **调度（串行执行）** | `factory/pipeline.py` `run_factory()` 的 `for requirement in req_set.requirements` 循环 | 已按拓扑序，逐个串行跑 `TddLoop.run()` |
| **UPSTREAM_FAILED 传播** | `factory/pipeline.py`（同一循环内） | 依赖未 PASSED → 下游判 `UPSTREAM_FAILED`，不进 TDD 循环、不计入通过率分母 |
| **容器节点（ROOT）识别** | `factory/pipeline.py` `build_children_map()` / `container_ids_of()` | 平台注入的 ROOT 只分解不设计（**并发工作流新增**） |
| **需求依赖结构** | `requirements_probe_closure6/requirements.yaml` | 见 §2.1 的菱形/汇聚点分析 |

---

## §2 现状核对：四项要求逐项实测

用户的设计要求是「拓扑排序 / 串行执行 / UPSTREAM_FAILED 传播 / 菱形只执行一次」。
**实测结论：前三项已实现，第四项已正确。**

### 2.1 closure6 的依赖结构

```
REQ-1 (根) ─┬─────────────────────→ REQ-11 ──→ REQ-12
            │                        ↑          ↑
            └→ REQ-7 ────────────────┘          │
REQ-5 (根) ──→ REQ-7                             │
            └→ REQ-3 ───────────────────────────┘
```

- **汇聚点**（多重依赖）：`REQ-7 ← [REQ-1, REQ-5]`、`REQ-11 ← [REQ-1, REQ-7]`、`REQ-12 ← [REQ-3, REQ-11]`
- **枢纽**：REQ-1 被 3 个下游依赖（闭包内）/ 8 个（全量 12 集）
- **菱形**：`REQ-1 → REQ-11`（直接）与 `REQ-1 → REQ-7 → REQ-11`（间接）—— REQ-1 是共享祖先

### 2.2 实验 1：菱形只执行一次 + 顺序正确 —— **已满足 ✅**

输入乱序 `[REQ-12, REQ-11, REQ-7, REQ-3, REQ-1, REQ-5]`：

```
拓扑序输出: [REQ-1, REQ-3, REQ-5, REQ-7, REQ-11, REQ-12]
每节点执行次数: {REQ-1:1, REQ-3:1, REQ-5:1, REQ-7:1, REQ-11:1, REQ-12:1}
✅ 菱形只执行一次    ✅ 依赖先于自身    ✅ 共享祖先 REQ-1 只出现 1 次
```

**机制**：`_topological_order` 维护 `done` 集合，节点出栈后即标记完成；
菱形共享祖先在第二次被访问时命中 `done` 直接返回 —— **同一节点只入 `order` 一次**。
调度层是普通 `for` 循环，不做重入，因此**物理上不可能重复执行**。

**实跑佐证**：chain5 运行日志中 `每个需求执行次数: {REQ-1:1, REQ-3:1, REQ-7:1, REQ-11:1, REQ-12:1}`。

### 2.3 实验：UPSTREAM_FAILED 传播 —— **已实现 ✅**

`pipeline.py` 的规则：

```python
blocked_by = [dep for dep in requirement.dependencies
              if dep in outcomes and outcomes[dep].state != "PASSED"]
if blocked_by:
    outcome = RequirementResult(req_id=..., state="UPSTREAM_FAILED",
                                note=f"上游未通过，未进入 TDD 循环（{reason}）")
    # 不进 TDD 循环、不计入通过率分母
```

**实跑佐证**：`closure6c` 中 REQ-1 失败 → 4 个下游按依赖关系正确跳过，
含**两级级联**（REQ-7 跳过 → REQ-11 跳过 → REQ-12 跳过）。

---

## §3 真实缺口（两个，均实测复现）

### 缺口 1：悬空依赖静默忽略 ★ 高

**判定条件**：`dep in outcomes` —— 只对**跑过的**依赖做传播。
若声明的依赖**不在本次运行集合内**，则既不排序（`by_id.get(dep)` 返回 None）、
也不阻断，直接按原顺序执行。

**实测**：输入 `[REQ-12(deps=[REQ-3,REQ-11]), REQ-11(deps=[REQ-1,REQ-7])]`（REQ-1/3/5/7 全缺失）：

```
输出: ['REQ-11', 'REQ-12']      无任何报错、无任何记录
```

**危害**（与闭包分析此前的警告一致）：

1. **失败归因不可分辨** —— REQ-12 若因缺少 REQ-3 的模块而失败，
   报告上看不出是「REQ-12 实现能力不足」还是「上游根本没跑」。
2. **可能假阳性通过** —— 若下游用参数注入绕过（本会话已实测过这种逃逸），
   它会在上游完全缺失的情况下"通过"。
3. **子集运行的合法性被混淆** —— 子集运行本身是合理用法（省成本），
   但必须**显式记录**这是子集，否则结论被污染。

**当前唯一的防线**在 `tools/pre_run_check.py`（跑前打印悬空依赖），
但它是**工具侧**的，不在**运行报告**里 —— 报告自身不带这个信息。

### 缺口 2：依赖环退回原顺序，可能违反依赖 ★ 中

**实测**：输入 `[A(deps=[B]), B(deps=[A])]` → 输出 `['B', 'A']` + 告警。

退回原顺序意味着**可能先执行依赖方**。虽然 UPSTREAM_FAILED 会兜住一部分
（后执行的 A 看到 B 未 PASSED 会被跳过），但：

- 输出顺序**取决于输入顺序**，不可预测
- 告警只进日志，**不进报告**
- 环通常是**需求规格缺陷**，工厂无法自行解决，却表现得像普通执行

### 附带缺口 3：图逻辑散落两处，无单一可测入口

| 逻辑 | 位置 |
|---|---|
| 排序 + 环检测 | `adapter._topological_order` |
| 调度 + 阻断传播 | `pipeline` 的 for 循环 |

两者对 `requirement.dependencies` 的解释**必须是同一套**，
但没有任何断言保证这一点。且两处都**静默忽略悬空依赖** —— 一致地静默，不等于正确。

---

## §4 设计

### 4.1 目标

不重写已有的正确逻辑，只做三件事：

1. 把图逻辑**收敛成一个可测的层**（`factory/dag.py`）
2. 让**悬空依赖与环进入运行报告**（可归因）
3. 提供**显式策略开关**，让子集运行与严格模式各得其所

### 4.2 新增模块 `factory/dag.py`

```python
@dataclass(frozen=True)
class ExecutionPlan:
    order: tuple[str, ...]                    # 拓扑序（依赖先于自身）
    cycles: tuple[tuple[str, ...], ...]       # 检测到的环（强连通分量）
    dangling: dict[str, tuple[str, ...]]      # 节点 -> 集合外的依赖
    levels: dict[str, int]                    # 最长路径深度（根=0）
    hubs: tuple[tuple[str, int], ...]         # 被依赖次数 ≥2 的枢纽

def build_plan(requirements: Sequence[Requirement]) -> ExecutionPlan: ...

def blocked_by(req: Requirement, outcomes: dict[str, RequirementResult],
               *, policy: str = "warn") -> list[str]:
    """下游是否应被跳过 —— 唯一的阻断判定入口。"""
```

**为什么是 `ExecutionPlan` 而不是只返回一个 list**：
闭包分析（`tools/closure.py`）已经算出这四项，且证明了它们的价值
（枢纽识别出 REQ-1 被 8 个下游依赖）。让运行时的计划与它同构，
报告里就能直接呈现同一套结构 —— 分析工具与执行层不再各算一套。

### 4.3 悬空依赖策略

新增配置 `dangling_dependency_policy: "warn" | "block"`，**默认 `warn`**：

| 策略 | 行为 | 适用 |
|---|---|---|
| `warn`（默认） | 记录进 `ExecutionPlan.dangling` 与该需求的 `dangling_dependencies`；**照常执行** | 合法的子集运行 |
| `block` | 同 `warn`，且该需求不进 TDD 循环，判 `DANGLING_DEPENDENCY` | 结论必须干净的正式运行 |

**关键不在于默认值，而在于"必须被记录"**：
无论哪种策略，`RequirementResult.dangling_dependencies` 都要有值，
报告才能回答"这次失败是不是因为上游没跑"。

### 4.4 环策略

检测**强连通分量**（Tarjan 或 DFS 栈），环内节点：

- 一律判 `CYCLIC_DEPENDENCY`，**不进 TDD 循环**（规格缺陷，工厂无法解决）
- **其余节点照常执行**（隔离缺陷，不因一个环节报废整轮）
- 环写入报告的 `cycles` 字段

当前「退回原顺序继续跑」的问题在于：它把规格缺陷伪装成正常执行，
且结果依赖输入顺序。

### 4.5 与 UPSTREAM_FAILED 的一致性

统一到**一个判定函数**，消除"两处各解释一遍"：

```python
def blocked_by(req, outcomes, plan, *, policy) -> tuple[list[str], str]:
    """返回 (阻断原因列表, 判定码)。判定码 ∈ {OK, UPSTREAM_FAILED, DANGLING_DEPENDENCY, CYCLIC_DEPENDENCY}"""
```

判定优先级（前者优先）：

```
1. 在环里            -> CYCLIC_DEPENDENCY
2. 有悬空依赖 且 policy=block -> DANGLING_DEPENDENCY
3. 有跑过但未 PASSED 的依赖   -> UPSTREAM_FAILED
4. 否则              -> OK，进入 TDD 循环
```

**与现状的兼容性**：默认 `policy=warn` 时，第 2 条不触发，
行为与当前实现**逐位一致**（已由现有 236 项断言中的 T21a–T21e 覆盖）。

---

## §5 接口与数据结构变更

| 位置 | 变更 | 说明 |
|---|---|---|
| `factory/dag.py` | **新增** | `ExecutionPlan` / `build_plan` / `blocked_by` |
| `factory/adapter.py` | `_topological_order` → 迁移到 `dag.build_plan` | 保留薄封装以兼容现有调用与断言 |
| `factory/pipeline.py` | 改用 `dag.build_plan` + `dag.blocked_by` | 删除循环内的内联判定 |
| `factory/config.py` | 新增 `dangling_dependency_policy: str = "warn"` | 环境变量 `FACTORY_DANGLING_POLICY` |
| `factory/models.py` | `RequirementResult.dangling_dependencies: list[str]`、`RunReport.execution_plan: dict` | 进报告 |
| `factory/models.py` | 新增判定码 `DANGLING_DEPENDENCY` / `CYCLIC_DEPENDENCY` | 与 `UPSTREAM_FAILED` 并列 |

---

## §6 验证计划（实现后要加的断言）

**不重跑 closure6 也能验证** —— 全部可用纯函数测试覆盖：

| # | 断言 | 覆盖 |
|---|---|---|
| D1 | 乱序输入 → 拓扑序依赖先于自身 | 排序正确性 |
| D2 | 菱形共享祖先只出现一次 | 缺口外的既有正确性（防回归） |
| D3 | 环 → `cycles` 非空，环内节点全判 `CYCLIC_DEPENDENCY` | 缺口 2 |
| D4 | 环 → **环外节点仍进 TDD 循环** | 缺陷隔离 |
| D5 | 悬空依赖 → `dangling` 记录完整（节点 → 缺失依赖列表） | 缺口 1 |
| D6 | `policy=warn` → 悬空依赖**不阻断**，行为与现状一致 | 向后兼容 |
| D7 | `policy=block` → 悬空依赖判 `DANGLING_DEPENDENCY` | 严格模式 |
| D8 | 上游 FAILED → 下游 `UPSTREAM_FAILED`（保持 T21 语义） | 传播不回退 |
| D9 | 判定优先级：环 > 悬空 > 上游失败 | §4.5 优先级 |
| D10 | `ExecutionPlan.levels` / `hubs` 与 `tools/closure.py` 对同一图输出**完全一致** | 分析与执行同构 |

D10 是最有价值的一条：它保证**核验工具与执行层不会各算一套图**。

---

## §7 不做的事（明确边界）

| 不做 | 理由 |
|---|---|
| **并行调度** | 当前瓶颈是网关与模型收敛，不是 CPU/墙钟；并行会让 token 峰值与失败归因复杂化，收益不明 |
| **按需重算下游**（上游改动后自动重跑下游） | 每轮运行都是全新输出目录，无增量场景 |
| **任务级（Step 级）DAG** | 架构文档第 3.3 章的 `decompose` 缺口属**另一个层级**（需求 → 任务），本轮只做需求级 |
| **动态重排**（运行中根据失败重排） | 上游失败已被 UPSTREAM_FAILED 正确处理，无需重排 |
| **改动 `_topological_order` 的算法** | 它已实测正确（§2.2），只搬家不改算法 |

---

## §8 实施顺序（网关恢复后可直接接上）

```
1. 新建 factory/dag.py + D1–D10 断言          （纯函数，不依赖网关）
2. adapter 改为薄封装，跑 check_all 确认无回归  （236 项断言必须仍全过）
3. pipeline 改用 dag.blocked_by               （T21a–T21e 必须仍全过）
4. 加 dangling_dependencies 报告字段 + 断言
5. 用 closure6 实跑一次，确认：
     - 报告里出现 execution_plan
     - 悬空依赖为空（closure6 是闭包，应当为空）
     - 行为与本次 guard 运行逐位一致
```

**第 5 步是关键的等价性验证**：默认策略下 DAG 层应当**不改变任何行为**，
只增加可观测性。若行为变了，说明搬家过程中引入了偏差。

---

## §9 与当前阻塞的关系

**本设计与网关无关，可立即实施。** 但建议的顺序是：

1. 等网关恢复 → 跑 closure6 → 拿到「依赖累积」的第一份数据（**最高优先级**，
   因为 REQ-11/REQ-12 从未进入 TDD 循环，多模块的真实失败模式**还没有被观察过**）
2. 再按 §8 实施 DAG 层

**理由**：§3 的两个缺口是**归因**问题，不是**执行**问题。
在还没见过一次真实的多模块运行之前就改图逻辑，等于在不知道失败模式的情况下设计对策。
若 closure6 跑通后 REQ-11/REQ-12 的失败模式恰好与悬空依赖或环无关，
那么这一层的优先级就应当下调。

**但这不妨碍先写断言**（§6 的 D1–D10 全部是纯函数测试，不依赖网关）。
