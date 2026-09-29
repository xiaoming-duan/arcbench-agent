# 门禁修订实验：A（路径白名单）与 C（import 审计）

日期：2026-09-28　模型：`deepseek-v4-pro`　方言：`node`
复现：`python3 tools/experiment_gates.py --reps 2`（需模型凭据，约 60 分钟）

前置：[EXPERIMENT.md](EXPERIMENT.md) 用反例推翻了 RED 门禁的判定口径——
模型会**新增一个会失败的测试文件**而把空转文件留在原地（15/15），
导致严格口径 0/8 却"看起来成功"。

本实验检验 A 与 C 能否堵住它。

---

## 0. 结论速览

| 处理组 | 严格口径通过 | **静默假通过** | 拒绝新增文件 | 重写轮次 |
|---|---|---|---|---|
| control（无 A 无 C） | 0/8 | **8/8** | 0 | 8 |
| **A 单独** | **7/8** | **0/8** | 7 | 15 |
| **A + C** | **7/8** | **0/8** | 7 | 15 |

- 预测"严格口径 0/8 → 接近 8/8"：**成立**（7/8，另 1 例为网关断连）。
- 预测"A+C 后重写轮次应下降"：**不成立，实际从 8 升到 15**（见第 5 节）。
- 预测"C 有增量"：**在本实验里测不出**（A 与 A+C 完全相同，见第 6 节）。

---

## 1. 四处位置的读取结果（改前）

| # | 位置 | 改前状态 |
|---|---|---|
| 1 | **测试文件生成/写入** | `generator.py:50/163/371`（Protocol / 桩 / LLM 的 `write_tests`）→ 落盘 `loop.py:398` → 写入原语 `workspace.py:71`：**只做目录穿越检查，无条件写** |
| 2 | **测试重写** | `loop.py:420-461`（rewrite while 循环）+ 反馈构造 `loop.py:83` `_weak_test_feedback` + `generator.py:371` `write_tests(weak_feedback=...)` |
| 3 | **路径处理（有无白名单）** | **无白名单**。`loop.py:51-69` `_test_paths()` 把 `plan.tests[].file_path` 与**所有新写的 `backend/tests/**` 合并**成运行集合——`:57-61` 就是漏洞源头。`loop.py:314` 的护栏只管**实现阶段** |
| 4 | **RED 门禁** | `loop.py:416` `red = runner.run(test_paths)`；判定只看 `TestOutcome.passed`，即 `testrunner.py` 的 **`exit_code == 0 and failed == 0`——整组退出码**，不是逐文件 |

---

## 2. `test_plan.yaml` —— A 的前提（新增设计维度）

A 的核心问题是"什么叫计划内声明的路径"。答案是：**在测试生成之前先产出计划**。

契约文件：[schemas/test_plan.schema.yaml](schemas/test_plan.schema.yaml)

```yaml
schema_version: "1.0"
test_plan:
  - requirement: REQ-1
    scenarios: [REQ-1-SCN-1]
    test_files:
      - path: backend/tests/req1.list.test.js
        covers: [REQ-1-SCN-1]
        type: unit            # unit | integration | e2e
```

Schema 里同时冻结了 7 条 `x-validation-rules`（非空、必须在 `backend/tests/` 下、
必须是测试后缀、路径不重复、场景全覆盖、`covers` 是子集、仅 e2e 可豁免 import）。

**计划本身也走门禁**：校验不过则带反馈重试产出，耗尽则该需求阻断（`_make_test_plan`）。

实际落盘产物（实验工作区取证）：

```yaml
schema_version: '1.0'
test_plan:
- requirement: REQ-1
  scenarios: [REQ-1-SCN-1]
  test_files:
  - path: backend/tests/req1.items.list.test.js
    covers: [REQ-1-SCN-1]
    type: unit
```

---

## 3. A：路径白名单

**改动**：运行集合不再由模型的输出决定，而由计划派生。

- `loop.py:216 _run_paths()`：白名单开启时**只跑计划内路径**；关闭时还原原行为（计划路径 + 所有写出的测试文件）。
- `loop.py:241 _enforce_test_whitelist()`：写测试与重写两处都过；计划外测试文件一律拒绝，记 `UNAUTHORIZED_TEST_FILE`。
- 违规写入 `RequirementResult.unauthorized_files` → `.arc/factory-report.json`。

**同步监控的新逃逸路径**：

| 逃逸路径 | 拦截手段 | 违规码 |
|---|---|---|
| 把种子文件拆成两个 | 新路径不在白名单内 | `UNAUTHORIZED_TEST_FILE` |
| 删除已有断言 / 用例 | 对**已引用实现**的文件，拒绝断言数或用例数下降 | `ASSERTION_DELETION` |
| 只改断言把测试改弱 | 同上 | `ASSERTION_DELETION` |
| 删除测试文件 | 计划内文件缺失即阻断 | `TEST_FILE_REMOVED` |

> **一个必须说明的设计取舍**：弱化守卫只保护**本来就有意义**的文件（已引用实现）。
> 空转文件的断言毫无价值，删掉它们正是我们想要的结果——若一律保护，
> 修复空转文件时会被自己拦住。

基线记录在 `factory/testplan.py:record_baseline`（哈希 + 断言数 + 用例数）。

---

## 4. C：`audit_imports()`

`factory/testaudit.py`，逐项处理了指定边界情况（9/9 单测通过）：

| 情况 | 判定 | 处理 |
|---|---|---|
| 引用了 `backend/src` 下模块 | `IMPORTS_IMPLEMENTATION` | 通过 |
| 只 import 外部包，无相对路径 | `NO_IMPLEMENTATION_IMPORT` | **WEAK_TEST** |
| 动态 `import(变量)` | `DYNAMIC_IMPORT` | **WEAK_TEST**（无法静态解析） |
| `@/` 别名未配置规则 | `ALIAS_UNRESOLVED` | **WEAK_TEST**（配置命中则正常解析） |
| 纯 E2E | `E2E_EXEMPT` | 仅当声明类型全为 e2e 时豁免 |
| helper / fixture / setup | `HELPER_EXEMPT` | 不审计；但**全部都是 helper 则整体不通过** |
| 文件不存在 | `MISSING_FILE` | **WEAK_TEST** |

接入门禁：`loop.py:160 _is_weak()` 改为

```python
整组通过            -> WEAK_TEST
否则任一文件 weak    -> WEAK_TEST     # C
```

**关键验证**（单测 ⑨）：空转种子与新增正常文件并存时，`ok=False` 且种子被标记——
这正是 RED 在原理上看不见的那一环。

---

## 5. 实验：三组对照

**设计**：同一套配对设计（诱导空转起点、写在**计划声明的路径**上、三组共用同一份
design + plan + seed）。每个 trial 在全新工作区里驱动**真实的 `TddLoop`**，
只切换 `enforce_test_whitelist` / `audit_in_red_gate` 两个开关。

### 结果

```text
处理组        严格口径通过    静默假通过    拒绝新增文件   重写轮次   n
control      0/8          8/8         0           8       8
A            7/8          0/8         7           15      8
A_plus_C     7/8          0/8         7           15      8
```

逐条明细里，**control 8/8 全部标记"★静默假通过"**（门禁放行、进入实现，
但种子文件始终没有引用实现）；A 与 A+C 的对应 trial 全部变成
`seed_exists=True red=True import=True`。

取证（A 组 REQ-1 r1）：目录下**只有计划内那一个测试文件**，它已被就地修复：

```javascript
const test = require('node:test');
const assert = require('node:assert');
  return require('../src/services/inventoryService');   // ← 原来这里是自造 mock
```

### 一个关键的过程发现

**A 的第一版是无效的。** 最初的实现只做拒绝，但没有把"你的文件被拒了"告诉模型。
冒烟测试显示：模型连续两轮都试图新增文件、连续两轮被拒，最后种子依然空转
（`拒绝新增=2`，严格口径 ✗）。

**补上拒绝反馈闭环后**（`_weak_test_feedback` 带上 `violations` + 计划内路径清单 +
"请在计划内文件里直接修复"），A 立刻生效：7/8 严格口径通过。

> **结论：光有白名单只能"阻断"，把拒绝理由回传给模型才能"纠正"。**
> 这是本次任务里最有价值的一条工程经验。

### 我的两个预测被证伪

1. **"A+C 后重写轮次应下降"——错，实际从 8 升到 15。**
   原因：模型第一轮**总是**先尝试新增文件（7/7 成功 trial 都恰好被拒 1 次），
   被拒后才转为原地修复。所以 A 让每个需求多付一轮模型调用。
   代价是 +1 轮重写，收益是把"静默假通过"换成"真正有效的测试"。

2. **"C 有增量"——本实验测不出。** A 与 A+C 的 8 个 trial 结果逐条相同。
   一旦模型被迫修改计划内文件，它自然就会引用实现，于是 C 的判定不再额外触发。
   C 的价值退回到**纵深防御**与**可见性**（上一轮 EXPERIMENT.md 里正是审计
   才让我们看见"空转文件与正常文件并存"）。保留 C，但不宣称它在本场景有独立增量。

---

## 6. 效度说明

1. **2 个失败是网关造成的，不是门禁造成的**：
   `A/REQ-3/rep2`（`Remote end closed connection`）与 `A_plus_C/REQ-4/rep1`（单 trial 882s 超时），
   两者的 `拒绝新增=0` 且 `重写=1`，说明重写调用根本没跑完。
   若剔除，A 与 A+C 实际上都是 7/7。
2. **起点是诱导的**（自然发生率太低），测的是"给定空转测试时门禁的因果效应"。
3. **n=8/组**，只能检出大效应。但 control 8/8 vs A 0/8 的"静默假通过"差异是
   完全分离的，不是统计推断。
4. **单一模型**（`deepseek-v4-pro`）。"第一轮总是尝试新增文件"这个行为模式
   是否对所有模型成立，未验证。
5. 实现阶段被置空（`implement` 返回 `[]`），本实验只考察测试阶段的门禁。

---

## 7. 附：本轮产出

| 文件 | 作用 |
|---|---|
| [schemas/test_plan.schema.yaml](schemas/test_plan.schema.yaml) | 测试计划契约（含 7 条校验规则与门禁联动说明） |
| [factory/testplan.py](factory/testplan.py) | 计划结构 / 规范化 / 校验 / 基线 |
| [factory/testaudit.py](factory/testaudit.py) | C：`audit_imports()` |
| [tools/experiment_gates.py](tools/experiment_gates.py) | 三组对照实验 |
| [experiment_gates_result.json](experiment_gates_result.json) | 逐 trial 原始记录 |
| [experiment_report.json](experiment_report.json) | 结构化任务报告 |
| `factory/loop.py` / `config.py` / `models.py` / `generator.py` | 计划阶段、白名单、弱化守卫、门禁开关 |
