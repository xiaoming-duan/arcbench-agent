# ARC-Bench Agent Starter v2 发布说明

## 标签

> **单模块闭环已验证；REQ-7 已通过测试阶段，因网关超时未能完成依赖修复**

判定依据：两次 **guard 隔离运行**（代码恒定 = 冻结快照 `v2-run`，只变环境）

| | run1 | run2 |
|---|---|---|
| 结果 | **3 通过 / 1 失败 / 2 跳过** | **1 通过 / 1 失败 / 4 跳过** |
| REQ-1 | ✅ PASSED | ❌ FAILED（`RemoteDisconnected`，重试 0% 成功） |
| REQ-3 | ✅ PASSED | ⏭️ 跳过 |
| REQ-5 | ✅ PASSED | ✅ PASSED |
| **REQ-7** | ❌ FAILED（**网络超时** `[Errno 110]`） | ⏭️ 跳过 |
| REQ-11 / REQ-12 | ⏭️ 跳过 | ⏭️ 跳过 |
| 网关重试 | 14（成功 12 / 耗尽 2 = 86%） | 2（成功 0 / 耗尽 2 = **0%**） |
| **隔离校验** | ✅ 冻结副本逐文件未变 | ✅ 冻结副本逐文件未变 + 运行期间工作区无改动 |

**两次的阻塞点都是网关，不是代码。** 代码侧的全部门禁都按设计工作。

---

## ⚠️ 诊断修正：REQ-7 的真实错误不是 `vi.hoisted`

本轮 `_runner_excerpt()`（把 stderr 一并带出）修复生效后，TEST_BROKEN 首次输出**完整错误**：

```
[TEST_BROKEN 诊断] exit_code=1，收集到 0 个测试
  stderr（前 500 字符）:
⎯⎯⎯ Failed Suites 1 ⎯⎯
 FAIL  tests/req7.update-quantity.test.js
Error: Cannot find module '../src/inventory-service.js'
 ❯ tests/req7.update-quantity.test.js:2:1
```

**真实根因是测试里的 import 路径写错**（`inventory-service.js` ↔ 实际 `inventoryService.js`），
不是此前推断的 `vi.hoisted` 顶层引用。

这一修正本身**证明了 `_runner_excerpt` 的价值**：修好之前，模型只看到「0 个测试被收集」，
既不知道错在第 2 行，也不知道是模块路径问题 —— 两轮重写都无法对症。

### 修好之后 REQ-7 的表现

```
第 1 次重写 → 测试可收集（FAIL 0/3 = 有效 RED）
           → RED 确认通过
           → 实现后 PASS (3/3)          ← 测试阶段完全走通
           → 依赖门禁触发（REQ-1 / REQ-5 = DEPENDENCY_NOT_USED）
           → 重写实现时 → 网络错误 [Errno 110] Connection timed out
```

**REQ-7 已经能走到「测试通过 + 依赖门禁正确拦截」这一步了** ——
剩下的只是网关不给机会完成最后一次重写。

---

## 本轮修复（v2）

### 1. `vi.hoisted` 硬约束（测试生成提示词）

`factory/generator.py:_dialect_note()` 的 vitest 分支加入约 8 行：

- 禁止在 `vi.hoisted(() => ...)` 内引用顶层 `import` / `require` 的绑定
- 说明成因（回调先于模块加载执行）与后果（收集阶段失败、0 个测试被发现）
- 给出两种正确写法（回调内部 `require(...)` / `vi.importActual`）

**注**：本轮诊断发现 REQ-7 的真实错误并非此项，但该约束仍然有效 ——
它防的是**另一类**收集失败（历史上确实出现过），收益不限于 REQ-7。

### 2. `_runner_excerpt()`：反馈同时带出 stdout 与 stderr ★ 本轮最关键

**旧写法** `outcome.stdout or outcome.stderr` 在 stdout 非空时**丢弃 stderr**。
实测（本机 vitest 4.1.8）：

| 流 | 内容 |
|---|---|
| stdout（10 行） | 只有摘要 `❯ <file> (0 test)` / `Tests no tests` —— **零错误信息** |
| stderr（16 行） | 完整 `Failed Suites` + `ReferenceError` / `Cannot find module` + 精确代码框 |

修复后两流都带出，stderr 优先。

### 3. `max_test_rewrites`: 2 → 4

v1 的实测收敛深度是 3 次重写，旧预算 2 在结构上不可能到达 RED。

### 4. 全量架构文档修订（v1.0 → v1.1）

见 `ARCHITECTURE_AUDIT.md`：核验结果由 **偏差 14 / 文档滞后 1** 改善为 **偏差 1 / 符合 35**。

---

## 隔离运行（本轮的方法学收获）

之前的 v2 测量是**裸跑**的，而运行期间工作区被并发修改（`factory/loop.py` 等被改在运行中途）。
本轮改为 `tools/workstream.py guard`：**在冻结副本内运行**，运行前后各校验一次。

两次运行都给出：

```
▶️  在冻结副本内运行: python3 main.py requirements_probe_closure6 ...
    cwd = .workstreams/v2-run（并发方改的是工作区，物理上够不着）
隔离校验（命令退出码 2）
  ✅ 冻结副本逐文件未变 —— 本次测量**可证明**跑在冻结的那份代码上
  ℹ️ 运行期间工作区有 3 个文件被改动（我方 3 / 他方 0）—— 对本次测量无影响，因为跑的是副本
```

**这不是形式主义**：run1 期间并发方确实又改了 `factory/loop.py`、新增 `factory/version.py`、
并改动了 `tools/test_gates.py`。没有隔离，这次测量又会变成「说不清跑在哪份代码上」。

---

## 验证状态：236/236 断言通过（8 类）

```
✅ 声称变更核查    tools/verify_changes.py           24 项
✅ 门禁行为断言    tools/test_gates.py              134 项
✅ 度量函数审计    tools/test_measures.py            18 项
✅ 依赖使用审计    tools/test_dependency_audit.py    24 项
✅ D10 修复验证    tools/verify_d10.py                6 项
✅ 依赖边方向断言  tools/test_call_edges.py          11 项
✅ 工作流隔离      tools/test_workstream.py           9 项
✅ 架构核验工具    tools/test_audit_architecture.py  10 项
✅ 合计 236 项
```

一键复现：`PYTHONPATH=arcbench-agent-runtime/src:. python3 tools/check_all.py`

## 门禁工作实况（两次运行累计）

| 门禁 | 触发次数 | 表现 |
|---|---|---|
| **TEST_BROKEN** | 2 | REQ-5、REQ-7 各 1 次 —— **均回退写测试阶段并成功修复** |
| **依赖使用门禁** | 1 | REQ-7 触发（REQ-1 / REQ-5 = `DEPENDENCY_NOT_USED`）—— 正确拦截 |
| **上游失败传播** | 2 | run2 中 REQ-1 失败 → 4 个下游按依赖关系正确跳过（含两级级联） |
| **RED 门禁** | 全部 | 无需回退 |
| **路径白名单 / 弱化守卫** | 0 | 未触发（模型未越界） |

---

## 结论：能做与不能做的声明

**可以声明**：

- ✅ 单模块闭环已验证（REQ-1 / REQ-3 / REQ-5 在隔离运行中通过）
- ✅ REQ-7 的**测试阶段已走通**（可收集 → 有效 RED → 实现后测试通过）
- ✅ 依赖使用门禁**确实会触发并正确拦截**
- ✅ 上游失败传播**确实生效**（含两级级联）
- ✅ 全部代码门禁在隔离条件下按设计工作

**不能声明**：

- ❌ 「依赖累积已验证」—— **REQ-11 / REQ-12 从未进入 TDD 循环**，最深链末端仍无数据
- ❌ REQ-7 的**依赖修复**已通过 —— 它在依赖门禁后死于网关超时，未取得干净结论

**下一步**：等网关稳定后重跑。**不需要改配置** —— 代码侧已就绪，缺的只是网络。

---

## 交付内容

| 路径 | 内容 |
|---|---|
| `dist/arcbench-agent-v2-final.tar.gz` | 本包（534 KB / 233 项） |
| `factory/` | 工厂实现（14 个模块，含并发方新增的 `version.py`） |
| `tools/` | 22 个工具（含 `workstream.py` 隔离、`audit_architecture.py` 核验） |
| `evidence/` | 全部运行日志与报告（closure6 六轮 + v2 四轮） |
| `ARCHITECTURE_AUDIT.md` | 架构符合性核验报告（含修订前后对照） |
| `ISOLATION.md` | 工作流隔离说明 |
| `软件工厂-项目架构文档.md` | v1.1（1428 行 / 18 章） |
| `check_all_output.txt` | 236 项断言输出 |

**打包排除**（按指定列表）：`node_modules` / `.env` / `.git` / `__pycache__` / `*.pyc` /
`dist` / `.pack_*.py`，另排除 `.workstreams` / `backups` / `out-*` / `.zipv` / 缓存目录。

---

## ⚠️ 工作区并发状态

并发工作流**仍在活跃**（本轮期间改动 `factory/loop.py`、`tools/test_gates.py`，
新增 `factory/version.py` 与 `.pack_v2.py`）。

值得记录：`factory/version.py` 的目标是「记录每一次运行跑在哪份代码上」，
其 docstring **直接引用了 `tools/workstream.py`** ——
两条工作流在「验证代码本身未被验证」这同一个问题上汇合了。

本次交付**不含**并发方对本工作流文件的改动以外的内容；其新增文件（`version.py` 等）
随包提交，但**未由本工作流评审**。
