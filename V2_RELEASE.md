# ARC-Bench Agent Starter v2 发布说明

## 标签

> **依赖累积已验证（结论为负）：REQ-11 未真实调用上游，被依赖门禁正确阻断**

这是第一次真正拿到「依赖累积」的数据 —— 不是因为它通过了，而是因为**整条链终于跑到了足够深的位置**，
并且门禁**正确地阻断了失败，而不是假阳性放行**。

## v2 运行结果（closure6，6 个需求）

| 需求 | 状态 | 尝试 | 测试重写 | token | 网关重试 | 依赖违规 |
|---|---|---|---|---|---|---|
| REQ-1 | ✅ PASSED | 1 | 0 | 12,253 | 3 | 0 |
| REQ-5 | ✅ PASSED | 1 | 1 | 20,041 | 0 | 0 |
| REQ-3 | ✅ PASSED | 2 | 1 | 27,949 | 0 | 0 |
| **REQ-7** | ✅ **PASSED**（历史首次） | 2 | **3** | 64,592 | 2 | 0 |
| **REQ-11** | ❌ **FAILED** | 4 | 0 | 70,220 | 0 | **2** |
| REQ-12 | ⏭️ UPSTREAM_FAILED | 0 | 0 | 0 | 0 | 0 |

**4 通过 / 1 失败 / 1 跳过（尝试 5 个，通过率 80%）**

全局：30 次调用 / 195,055 token（推理 142,841 = 73%）/ 网关重试 5 次（成功 5、耗尽 0，成功率 100%）
墙钟：23:57:27 → 00:47:39 = **50 分 12 秒**

## 关键结论

### 1. REQ-7 首次通过 —— 修复链完整生效

REQ-7 在 v1 的四轮里**从未通过**。本轮通过，路径是：

```
TEST_BROKEN ×3（测试不可收集，回退写测试阶段）
  → RED 确认通过（测试终于可收集）
  → 依赖门禁：REQ-1=DEPENDENCY_NOT_USED; REQ-5=DEPENDENCY_NOT_USED
  → 重写实现，真实调用上游
  → PASSED
```

**这直接验证了 `max_test_rewrites` 2→4 的必要性**：REQ-7 用了 **3 次**重写才产出可收集的测试，
旧预算 2 下它在结构上不可能走到 RED。

### 2. REQ-11 失败 = 依赖累积存在实现层问题的直接证据

REQ-11 的**测试通过了**，但依赖门禁判定它没有真实调用声明的上游：

```
测试通过但依赖未被真实验证（REQ-1=DEPENDENCY_NOT_USED; REQ-7=DEPENDENCY_NOT_USED）
重写 4 次仍未满足依赖使用要求
```

这不是环境、不是测试问题、不是误判 —— 是**下游模块确实没有调用上游**。
门禁把它**正确阻断**，而不是让一个「测试全绿但模块间毫无耦合」的结果冒充成功。

**这正是当初要找的那类空转**：测试层空转（测试不 import 实现）已被 RED 门禁 + import 审计堵住，
模块层空转（下游不调用上游）需要依赖使用门禁来堵 —— 本轮证明了它确实会触发、且拦得住。

### 3. 依赖门禁在本轮共触发 4 次

| 需求 | 触发内容 | 结果 |
|---|---|---|
| REQ-3 | REQ-1=DEPENDENCY_NOT_USED | 重写后满足 → PASSED |
| REQ-7 | REQ-1、REQ-5=DEPENDENCY_NOT_USED | 重写后满足 → PASSED |
| REQ-11 | REQ-1、REQ-7=DEPENDENCY_NOT_USED | 4 次重写耗尽 → **FAILED** |

REQ-3 与 REQ-7 都在门禁压力下**学会了真实调用上游**；REQ-11 没有。差别在于
REQ-11 需要同时接上**两个**上游（REQ-1 与 REQ-7），复杂度更高。

## 本轮（v1 → v2）的核心修复：诊断驱动

v1 的遗留结论是「REQ-7 待修（测试可收集性）」。诊断（不是盲试）发现两点：

### 缺陷一：反馈丢掉了 stderr（**直接死因**）

`_broken_test_reason` 写的是 `outcome.stdout or outcome.stderr`。
实测复现（本机 vitest 4.1.8）：

| 流 | 内容 |
|---|---|
| **stdout**（10 行） | 只有摘要：`❯ tests/broken.test.js (0 test)` / `Tests no tests` —— **零错误信息** |
| **stderr**（16 行） | 完整 `Failed Suites` + `ReferenceError: Cannot access '__vi_import_0__' before initialization` + 精确代码框 |

只要 stdout 非空，**stderr 永远不显示**。于是模型只收到「0 个测试被收集」，
**从未知道 ReferenceError 在第 5 行、也不知道是 `vi.hoisted` 的问题** —— 连续两轮无法修复。

修复：新增 `_runner_excerpt()`，**两流都带出，stderr 优先**。

### 缺陷二：提示词没有禁止 `vi.hoisted` 顶层引用

模型反复使用的写法：

```js
import Database from 'better-sqlite3';
const { default: testDb } = vi.hoisted(() => {
  const db = new Database(':memory:');   // ← 必然 ReferenceError
```

`vi.hoisted` 的回调先于所有 import 执行，访问不到顶层作用域。
修复：vitest 方言提示词加入硬约束（禁止该写法 + 说明成因与后果 + 给出
「回调内部 `require`」与 `vi.importActual` 两种正确写法）。

### 修复三：预算 2 → 4

有了前两项，3 次重写是 REQ-7 的实际收敛深度；2 不够，4 有余量。

## 验证状态：175/175 断言通过（6 类）

```
✅ 声称变更核查    tools/verify_changes.py          24 项
✅ 门禁行为断言    tools/test_gates.py              92 项
✅ 度量函数审计    tools/test_measures.py           18 项
✅ 依赖使用审计    tools/test_dependency_audit.py   24 项
✅ D10 修复验证    tools/verify_d10.py               6 项
✅ 依赖边方向断言  tools/test_call_edges.py         11 项
✅ 合计 175 项
```

一键复现：`PYTHONPATH=arcbench-agent-runtime/src:. python3 tools/check_all.py`

## ⚠️ 工作区并发修改声明（重要）

v2 运行期间（23:57 → 00:47），**工作区被另一条工作流并发修改**，被改文件包括
`factory/{store,generator,llm,pipeline,loop,testaudit}.py`。这些修改**不是本次任务所为**，
内容是为 arcbench 追溯性写入 `call_edges` / `node_contracts` 两张此前恒为 0 行的表，
并新增 `tools/test_call_edges.py`（11 项 call edge 方向断言）。

**对 v2 结果的影响**：Python 进程在 23:57:27 启动时已加载模块，其后对磁盘 `.py` 的编辑
**不影响该运行中的进程**。因此上表结果对应的是**任务开始时的代码状态**。

**但需要如实说明**：
1. 本包同时包含两条工作流的改动（二者兼容，175/175 全过）。
2. 若需要一份**严格的、与当前磁盘代码一一对应**的 v2 测量，应在当前合并后的代码上重跑一次 closure6。
3. 本次未做该重跑。

## 复现方式

```bash
export OPENAI_API_KEY=... OPENAI_BASE_URL=https://api.arc-bench.com/v1 MODEL=deepseek-v4-pro
export FACTORY_MAX_TOKENS=3000 FACTORY_MODEL_TIMEOUT=300
export FACTORY_NPM_CACHE=$PWD/.npm-cache XDG_CACHE_HOME=$PWD/.cache
export FACTORY_BYPASS_BLOCK=1          # B（注入旁路）升级为阻断

python3 tools/pre_run_check.py --requirements requirements_probe_closure6 --health-count 5
python3 main.py requirements_probe_closure6 --output-dir out --type web \
  --generator llm --install-deps auto --max-repairs 3
PYTHONPATH=arcbench-agent-runtime/src:. python3 tools/check_all.py
```

## 未验证 / 遗留

1. **REQ-12 从未进入 TDD 循环** —— 最深链末端仍无数据。它需要 REQ-3 与 REQ-11 同时就绪。
2. **REQ-11 的失败只观测到 1 次** —— 需要多轮重复才能区分「稳定缺陷」与「本轮运气」。
3. **未做拒绝理由的进一步强化** —— REQ-11 需要在一次实现里同时接上两个上游，
   可考虑在拒绝理由里显式列出「你还需要接上哪一个上游」的检查清单。（REQ-3/REQ-7 都通过的那次
   是因为各自只需接一个或已被逐个提示。）
4. 多模块 DAG（拓扑排序 + 依赖序串行）仍未实现。
5. 严格对应当前磁盘代码的重跑（见上方并发修改声明）。

## 交付内容

| 路径 | 内容 |
|---|---|
| `factory/` | 工厂实现（13 个模块） |
| `tools/` | 验证与实验工具（含 closure.py / verify_d10.py / check_all.py / test_call_edges.py） |
| `evidence/` | 全部运行日志与报告（closure6 六轮、v2 两轮、chain5、subset3、probe12） |
| `requirements_probe*/` | 探针需求集 |
| `dist/arcbench-agent-v2.tar.gz` | 本包 |
