# ARC-Bench Agent Starter v1 发布说明

## 标签

> **上游失败传播已修；REQ-7 待修（测试可收集性）**

判定依据（四轮 closure6 累计）：

| 需求 | 通过 | 失败 | 跳过 | 说明 |
|---|---|---|---|---|
| REQ-1（枢纽，被 8 个下游依赖） | 3 | 1 | 0 | 唯一失败为环境（网关 `proxy_error: connection reset`） |
| REQ-5 | **4** | 0 | 0 | 每轮都先判 `TEST_BROKEN`，回退重写后通过 |
| REQ-3 | 3 | 0 | 1 | 每轮都触发依赖门禁 `REQ-1=DEPENDENCY_NOT_USED`，重写后通过 |
| **REQ-7** | **0** | 3 | 1 | **持续受阻于「测试文件无法被收集」** |
| REQ-11 | 0 | 0 | 4 | 因 REQ-7 未通过而正确跳过 |
| REQ-12 | 0 | 0 | 4 | 因 REQ-11 跳过而级联跳过 |

失败**集中在 REQ-7（上游节点）**，不在最深链末端 → 按决策树属「上游失败传播已修，上游实现待修」。
需补充说明：REQ-7 的阻塞点不是实现能力，而是**测试自身写不出来（不可收集）**；其最后一次重写尝试死于环境断连。

## 交付内容

| 路径 | 内容 |
|---|---|
| `factory/` | 工厂实现：适配层、TDD 循环、生成器、审计、运行器、流水线 |
| `tools/` | 验证与实验工具（见下） |
| `evidence/` | 所有运行的原始日志与报告（closure6 四轮、chain5、subset3、probe12） |
| `requirements_probe*/` | 探针需求集（全量 12、closure6、chain5、subset3、roots） |
| `schemas/` | `test_plan.schema.yaml`（含 7 条 `x-validation-rules`） |
| `*.json` | 实验结果与任务报告 |
| `软件工厂-项目架构文档.md` | 架构文档（17 章） |

## 验证状态：119/119 断言通过

```
✅ 声称变更核查   tools/verify_changes.py        24 项
✅ 门禁行为断言   tools/test_gates.py            60 项
✅ 度量函数审计   tools/test_measures.py         18 项
✅ 依赖使用审计   tools/test_dependency_audit.py 24 项
✅ D10 修复验证   tools/verify_d10.py             6 项
✅ 合计 119 项
```

一键复现：`PYTHONPATH=arcbench-agent-runtime/src:. python3 tools/check_all.py`

## 门禁回归（三配置）

| 配置 | 结果 |
|---|---|
| 干净 | `2 通过 / 0 失败 / 0 上游失败跳过 / 0 空转` — EXIT=0 |
| 脏（同目录重跑） | `0 通过 / 1 失败 / 1 上游失败跳过 / 1 空转` — EXIT≠0 |
| 全关对照臂 | `2 通过` — EXIT=0 |

## 本轮修掉的结构性缺陷

### 1. 上游失败传播（`factory/pipeline.py`）
**此前**：声明依赖的上游失败后，下游仍被调度并消耗预算，还可能靠旁路造成假阳性通过，使失败归因不可分辨。
**现在**：上游未通过 → 下游判 `UPSTREAM_FAILED`，**不进入 TDD 循环、不计入通过率分母**。
**实测**：closure6 第 3 轮中 REQ-1 失败 → 4 个下游按依赖关系正确跳过，包括两级级联（REQ-7 跳过 → REQ-11 跳过 → REQ-12 跳过）。

### 2. 实现阶段看不到测试源码
**此前**：提示词给「接口契约」并要求「测试是权威」，却**从不给测试源码**。REQ-7 的接口契约写 `updateQuantity(sku, quantity)`，测试却按仓储注入写 `updateQuantity(repository, sku, quantity)` —— 两个契约互相矛盾，模型只能猜签名。
**现在**：实现阶段直接收到测试源码，并明确「若与接口契约冲突，以测试源码为准」。

### 3. 测试无法被收集 = 有效的 RED（最严重）
**此前**：模型写出非法的 vitest 惯用法（在 `vi.hoisted(() => ...)` 里引用顶层 `import` 的绑定，必然抛 `ReferenceError`），整个文件在收集阶段就失败、0 个测试被发现。RED 门禁把它当成有效的 RED（"整组失败"确实是失败），于是 **4 轮实现预算全部浪费在一个坏掉的测试文件上** —— 任何实现都救不了它。
**现在**：新增 `TEST_BROKEN` 判据（`not passed and total == 0`），回退到**写测试阶段**（复用 WEAK_TEST 的成对回退），并给出针对 `vi.hoisted` 的可执行修法。
**实测**：第 3、4 轮中 REQ-1、REQ-5 各触发一次并成功修复；第 4 轮 REQ-3 也触发。

## 已知阻塞

**REQ-7 的测试可收集性**：`max_test_rewrites=2` 用尽后仍未能产出可收集的测试。
第 4 轮的最后一次重写尝试死于环境（`RemoteDisconnected`），所以「预算是否足够」尚无干净数据。

**建议**：把 `max_test_rewrites` 从 2 提到 4，再跑一次 closure6。这是当前唯一已知的、与「实测阻塞点」直接对应的旋钮。

## 依赖闭包（本轮分析产物）

- closure(REQ-12) = **6 个**：`REQ-1, REQ-5, REQ-3, REQ-7, REQ-11, REQ-12` → 策略 `full`（≤8）
- 全量探针集 = 12 个 → 策略 `closure`（9–12 带）
- 拓扑深度 4 层，最深 REQ-12
- **枢纽 REQ-1 被 8 个下游依赖**（失败影响面最大）
- 无悬空依赖 —— 探针集自洽

## 复现方式

```bash
export OPENAI_API_KEY=... OPENAI_BASE_URL=https://api.arc-bench.com/v1 MODEL=deepseek-v4-pro
export FACTORY_MAX_TOKENS=3000 FACTORY_MODEL_TIMEOUT=240
export FACTORY_NPM_CACHE=$PWD/.npm-cache XDG_CACHE_HOME=$PWD/.cache

# 闭包分析
python3 tools/closure.py --target REQ-12

# 跑前检查（网关 5x + 字段可记录 + 拓扑 + 悬空依赖）
python3 tools/pre_run_check.py --requirements requirements_probe_closure6 --health-count 5

# 依赖闭包运行
python3 main.py requirements_probe_closure6 --output-dir out --type web \
  --generator llm --install-deps auto --max-repairs 3

# 全量验证
PYTHONPATH=arcbench-agent-runtime/src:. python3 tools/check_all.py
```

## 未验证事项（如实声明）

1. **REQ-11 / REQ-12 从未进入 TDD 循环** —— 依赖累积在「最深链末端」的结论**仍无数据**。
2. **全量 12 需求集未跑** —— 本轮只跑了 closure6。
3. 多模块 DAG（拓扑排序 + 依赖序串行）未实现，等上述阻塞解除。
