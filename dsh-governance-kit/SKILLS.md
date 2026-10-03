# SKILLS.md —— 能力 → 形态 → 依赖

> 本文件回答三个问题：**这项能力做什么、它是什么形态、它依赖谁。**
> 每项都标注**验证状态** —— 未验证的不得升级标注。

---

## 总表

| # | 能力 | 形态 | 依赖 | 验证状态 | 建议顺序 |
|---|---|---|---|---|---|
| 1 | UI 契约提取 | `SKILL.md` | 无 | ✅ **真实语料实测**（覆盖 70.7% / 精确 90.8%） | 1 |
| 2 | 契约冻结 | Tool Plugin（`defineTool`） | 无 | ✅ 真实运行（6/6，0 误报） | 2 |
| 3 | 分类型重试预算 | Agent Loop Plugin | DSH Agent Loop 接口 | ✅ 真实运行（尝试 7 次 / 预算正确耗尽） | 3 |
| 4 | 审计 Provider | Inspect Provider | 门禁与预算的结论结构 | ⚠️ 数据已有，Provider 未写 | 4 |
| 5 | 五道门禁 Interceptor | Interceptor | 前三项 + DSH 执行钩子 | ⚠️ 合成验证（UI 门禁真实首轮为误报） | 5 |

**先给 1 + 2，验证 DSH 接口后再做 3+。**

---

## 模块 1：UI 契约提取

### 做什么

从**自然语言需求正文**提取结构化的 UI 契约：

```
输入：需求描述（自然语言）
输出：
  - page      页面标识
  - elements[]  元素清单（id / type / 可访问名 / 校验规则 / 错误消息）
  - invariants[] 页面级不变量（含「不得发生」这类否定式要求）
```

**为什么值得独立**：编排层只知道"做注册功能"，
无法回答"页面上应该有什么、可访问名该叫什么"。
而可访问名是**可测的**——它可以直接映射到 `getByRole` / `getByLabel`。

### 形态：`SKILL.md`

不依赖整个工厂，可独立使用。规则：**正则抓明确模式 + LLM 兜底**。

### 输入输出契约

```yaml
# 输入：需求正文（任意自然语言）
# 输出：
ui_contracts:
  - page: "/(实现自定义：工作簿首页)"
    title: "Workbooks"
    elements:
      - id: last-updated
        type: text
        label: "Last updated:"          # ← 可访问名规则，可直接映射 getByLabel
        validation: "每条记录都显示 'Last updated: <值>'"
        error_messages: {format: "..."}
        initial: unchecked
    error_display: "..."
    state_preservation: "..."
    invariants:
      - "**另一工作簿的数据不得出现在当前网格中**"
```

### `invariants` 为什么是必要扩展

有些 UI 要求**不是**"某元素存在"，而是"某种情况**不得发生**"
（如跨工作簿数据隔离）。这类用元素清单表达不了，必须单列。

**这是一个真实的规格缺口，不是设计偏好。**

### 验证状态：✅ 真实语料实测（可引用）

**移植来源**：`factory/uicontract.py`（294 行）+ `tests/test_ui_contract_extraction.py`（22 项）

| 指标 | 实测 | 阈值 | 口径 |
|---|---|---|---|
| **覆盖率** | **70.7%** | ≥ 60% | 468 个**叶子需求** / 6 个真实应用中有多少提取出元素 |
| **精确率** | **90.8%** | ≥ 85% | 提取出的可访问名（357 个）有多少真的出现在平台基准测试 / helpers 中 |
| 单应用下限 | 逐应用校验 | 30%–90% | 见 ARCHITECTURE §五 的表 |

**本工作区已复现**（`.probe-upstream/arc-bench/webapp/`，6 个应用：12306 / bookstack /
ctrip / keep / prestashop / stackoverflow）。

**两个必须随数字一起引用的口径限定**：

1. **分母是叶子需求**。用**全部**需求算得 **51.7%** —— 容器节点本就不该有 UI 契约。
   不写明口径，两个数字的差异会被误读为矛盾。
2. **精确率是代理指标；召回率未测**。真实 helpers 大量用 role + 正则定位，
   召回率无法可靠测量，故只断言精确率。

**另有一处方向对齐断言**（不要删）：`test_helpers_contract_is_role_based`
钉住「真实 helpers 的定位方式以 `getByRole` 为主」——
若这一点不成立，「按可访问名判覆盖」的整个方向就是错的。

---

## 模块 2：契约冻结

### 做什么

把"跨模块调用约定"从**提示词建议**升级为**不可篡改的验收合同**。

**实测动机**：某需求拿到 **3 次**依赖归因提示 + 1 次阻断理由，仍未真实集成上游。
**提示词的约束力不够。**

### 形态：Tool Plugin（`defineTool`）

```
freeze_contract(req_id, calls) → 写只读文件 + sha256 侧车
verify_contract(req_id)        → 校验哈希 → 返回 verdict
```

### 三态判定

| verdict | 含义 | 处置 |
|---|---|---|
| `CONTRACT_MISSING` | 合同**不存在**（或未冻结 / 与需求声明**漂移**） | 阻断 |
| `CONTRACT_TAMPERED` | 合同**存在但被改过**（哈希与冻结时不一致） | 阻断 |
| `CONTRACT_MISMATCH` | 合同**未被改**，但**实现/调用**不符合它 | 阻断（可重试） |

**MISSING 里含"漂移"**：合同文件原封不动、但需求声明变了 ——
这是"声明改了而合同没重新冻结"，与"合同被改"是**两件不同的事**，故分开判定。

**判定顺序**：**篡改优先于漂移** —— 文件被改时报更具体、更可操作的 TAMPERED。

### 不可编辑性的三层

| 层 | 手段 | 覆盖 | 作用 |
|---|---|---|---|
| 权限位 | `chmod 0444` | — | 让"随手改"不容易发生（**纵深防御，不是安全边界**） |
| 内嵌 | `integrity: {sha256, algorithm}` | **正文** | **可移植**：合同单独拿走也能自证 |
| 侧车 | `<req>.sha256` | **完整文件字节** | **权威**：连"只改 integrity 字段"也能发现 |

### 两条实现纪律（缺一即失效）

1. **生成与校验必须共用同一个 `hash_of_body()`** ——
   两边各写一套口径是这类校验最经典的失效方式。
2. **不得保留"读不到合同就回退到需求声明"的旁路** ——
   保留回退等于合同缺失时提示词仍拿到内容，**表面正常而"冻结"未生效**。
   实测中正是这个形态：读路径因 parent 链失效 → 静默回退 →
   提示词内容恰好相同 → 看起来一切正常。

### 验证状态：✅ 真实运行验证

- ✅ closure6 **cf3：6/6 全链通过**，`CONTRACT_MISSING` **0 次误报**
- ✅ 合同 `-r--r--r--` 只读**未影响正常流程**
- ✅ 12+3 项合成断言（生成 / 只读读取 / 未冻结 / **漂移** / **篡改** / 损坏 / 可执行理由 / **删掉侧车后仍能发现正文篡改**）
- ⚠️ 内嵌 `integrity` 双路哈希**未跑真实 closure6**（合成 + 冒烟 + 真实产物复核）

---

## 模块 3：分类型重试预算

### 做什么

按**错误类型**分配重试预算，而不是统一的 `max_retries`。

**实测动机**：一次 DNS 失败让需求连设计都没做完 —— 那与模型能力无关。

### 形态：Agent Loop Plugin（依赖 DSH Agent Loop 接口）

```js
const RETRY_BUDGET = {
  TEST_FAILED: 3,
  DEPENDENCY_NOT_USED: 3,
  CONTRACT_MISMATCH: 2,
  CONTRACT_MISSING: 1,
  CONTRACT_TAMPERED: 1,
  TEST_BROKEN: 2,
  WEAK_TEST: 2,
  ENVIRONMENT: 1,
};
```

### 行为

```
按错误信号查表 → 每类独立计数 → 环境错误不占实现预算
预算耗尽 → 升级或终止
```

### 三条设计约定

1. **计费键是信号**（不是粗粒度类别）—— 合同问题改实现修不好，只给 1 次；
   实现问题最宽，给 3 次。
2. **表外信号回退到类别兜底**，不会"没预算可用"。
3. **未知信号保守归 `implementation`**（预算最大）——
   宁可多给一次机会，也不要把可能的真问题当环境问题直接终止。

### 验证状态：✅ 真实运行验证

- ✅ closure6 **p0v**：REQ-11 共尝试 **7 次**（旧统一预算 3 下不可能到 7）
- ✅ `DEPENDENCY_NOT_USED` 花费 **4 > 预算 3** → **正确耗尽并阻断**
- ✅ `ENVIRONMENT` 花费 0（该轮无环境故障）——**该主张在真实运行中未被反驳，但也未被验证**（无对照组）
- ✅ 账本进报告（`spent / left / exhausted / history`）
- ✅ 30 项合成断言

### 一个真实发现的规则冲突（应记入待办）

`WEAK_TEST` 规则要求测试 **import 真实实现**（防"断言自己造的假数据"），
这是**为 unit 测试设计的**。而 **E2E 测试天然不 import 实现** ——
它通过**浏览器**驱动应用。

实测日志：

```
[门禁] REQ-1-1-1 WEAK_TEST（3 个测试文件未引用实现
  (backend/test-e2e/*.spec.js[ALIAS_UNRESOLVED]）），回退到写测试阶段重写（1/4）
```

**后果**：一次无谓的写测试重写（消耗预算与 token）。
**修法**：按 `type` 给 `WEAK_TEST` 加豁免。**本轮未修**。

---

## 模块 4：审计 Provider（未写）

### 做什么

把门禁与预算的结论暴露成 DSH 可查询的 Inspect Provider。

**动机**：实测中「机制在正确工作，可观测性却为零」——
账本被门禁段整体覆盖，报告里 6 个需求的账本全部丢失，
**正确工作的机制与未工作的机制在报告上不可区分**。

### 形态：Inspect Provider

```json
{
  "req_id": "REQ-11",
  "state": "FAILED",
  "gate_audits": {
    "dep_ok": false, "mock_ok": true, "bypass_ok": true,
    "contract_ok": true, "ui_ok": true, "combined_ok": false
  },
  "repair_budget": {
    "spent": {"DEPENDENCY_NOT_USED": 4, "IMPLEMENTATION_REGRESSION": 3},
    "exhausted": ["DEPENDENCY_NOT_USED"]
  },
  "ui_element_coverage": {"grid": true, "pivot-table": true},
  "contract_violations": [], "ui_violations": []
}
```

### 验证状态：⚠️ 数据结构已有，Provider 未写

数据在真实运行中被验证过（p0v 报告含 `repair_budget`），但**没有 DSH Provider 形态**。

---

## 模块 5：五道门禁 Interceptor（整合成本最高）

### 做什么

```
combined_ok = dep_ok and mock_ok and bypass_ok and contract_ok and ui_ok
```

| 门 | 判定对象 | 真实运行验证 |
|---|---|---|
| 依赖使用 | 声明了依赖就必须**真实调用**上游 | ✅（多次触发并阻断） |
| mock | 测试不得 mock 未声明的上游 | ✅ |
| 注入旁路 | 实现不得用形参守卫绕过上游 | ✅ |
| 契约 | 签名符合 + 合同未被篡改/漂移 | ✅（`CONTRACT_MISMATCH` 真实触发 1 次） |
| UI | E2E 存在 / RED 有效 / 元素覆盖 / 错误消息一致 | ⚠️ **合成验证**；真实首轮是误报 |

### 两条关键设计

1. **不短路**：任一门禁为假都不放行，且**全部门都被求值** ——
   否则被跳过的门禁结果不会被使用，也就不会被发现。
2. **门禁位置本身也是被测对象**：依赖门禁原先在 `if outcome.passed:` 之内 ——
   测试不过就永不审计。而"测试过不去"**恰恰最可能有依赖问题**。

### UI 元素覆盖的三级匹配（防误报）

```
① 元素 id 原样 / 连字符 / 下划线 / 驼峰变体
② 可访问名 label 的**可字面部分**（getByLabel('Last updated:')）
③ **占位符型 label**（如 `<workbook name>`，可访问名运行时才确定）
   → 回退到按**角色**匹配（getByRole('link' / 'button' ...)）
```

**第 ③ 级不可省**：只认字面 label 会误报，而**误报会让人关掉门禁 —— 那比没有门禁更糟**。
（这是真实误报逼出来的设计：`workbook-link` 的 label 是占位符。）

### 验证状态：⚠️ 合成验证

- ✅ 真实运行**部分**验证：生成的 3 个 E2E 文件覆盖 **11/11 元素**（真实产物复核）
- ⚠️ **门禁本身的端到端修复未经完整真实运行**（第三次运行被 `HTTP 402` 余额耗尽阻断）
- ⚠️ `UI_TEST_WEAK` 与 `UI_ERROR_MESSAGE_MISMATCH` **在真实运行中从未触发过**
  （前者因无法从"测试已通过"反推 RED；后者因工作簿需求**没有错误消息**）
- ⚠️ **E2E 从未真正驱动过浏览器** —— 浏览器已装、`webServer` 已补，
  但 UI 实现从未生成到可运行程度

---

## 移植时必须一起带走的四个文件

| 文件 | 作用 | 为什么不能省 |
|---|---|---|
| `criterion`（判据双向验证） | 断言必须双向成立 | 无它则验证器自身失效无法发现 |
| `import 冒烟守卫` | 跨模块符号一致性 | 两次真实抓到污染；是**事前**阻止的一半 |
| `陈旧快照守卫` | 拒绝复用旧代码 | 静默复用会让测量跑在旧代码上 |
| `LESSONS.md` | 已知纪律 | 见该文件 |
