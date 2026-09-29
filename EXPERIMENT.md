# 度量实验：WEAK_TEST 重写反馈与门禁的结构性漏洞

日期：2026-09-28　模型：`deepseek-v4-pro`　后端：`stdlib-http`　方言：`node`

复现：
```bash
python3 tools/experiment_weak_feedback.py --reps 2   # 跑实验（需模型凭据，约 25 分钟）
python3 tools/reanalyze_experiment.py                # 严格口径重算（不调模型，需保留 out-exp/）
```
> 原始试验记录已固化在 [experiment_result.json](experiment_result.json)（宽松口径）与
> [experiment_reanalysis.json](experiment_reanalysis.json)（严格口径）。若已清理 `out-exp/`，
> 重算前需先重跑实验。

---

## 0. 结论速览

| 命题 | 实验结论 |
|---|---|
| "把重写反馈换成 import 诊断，能提高一次重写修好的概率" | **被否证**。两组都是 0/8 |
| RED 门禁能否保证测试是有意义的 | **不能**。存在稳定可复现的逃逸路径 |
| import 审计的价值定位 | 不是"反馈杠杆"（无效），而是"检测器"（必需） |

---

## 1. 被检验的假设

上一轮我提出（**未经证实**）：

> 把重写反馈从"你的测试在没有实现的情况下通过了"，
> 换成"你的测试没有 import `src/` 下任何模块"，一次重写就修好的概率会高得多。

本实验就是来证实或否证它。

---

## 2. 实验设计（配对对照）

自然发生率太低（8 次需求执行里只有 1 次空转），直接跑流水线拿不到样本。因此：

1. **诱导统一起点**：用"故意不 import 实现、在测试里自造 mock"的提示，为每个需求造出一个空转测试。
2. **校验起点**：在无实现的工作区单独运行该文件，必须通过；否则丢弃该需求。
3. **配对处理**：同一份起点，分别用两种反馈各重写 `reps` 次。
4. **两组**：
   - `basic` —— 只说"测试在没有实现的情况下就通过了"（原实现）
   - `import_aware` —— 附加静态 import 诊断："测试没有引用 `backend/src` 下任何模块"

4 个需求 × 2 组 × 2 重复 = **16 次配对试验**，每次都是独立的干净工作区。
反馈文本由**真实代码路径**（`TddLoop._weak_test_feedback`）生成，不是脚本里复刻的副本。

---

## 3. 第一层结果：假设被否证

原始判定口径是"这一组测试文件整体是否失败（RED）"：

```
处理组            一次重写达成 RED      重写后引用实现
basic            8/8                7/8
import_aware     7/8                7/8        ← 唯一失败是网络断连（0.0s），非真实失败
Fisher 精确检验 双尾 p = 1.000
```

**两种反馈没有区别，而且 basic 本身就是 8/8。** 光说"你的测试通过了、必须失败"，
模型就已经知道要怎么办了——附加 import 诊断没有任何增量作用。

假设否证。

---

## 4. 第二层发现：原来的判定口径是错的

复核时发现一个反例：`REQ-2-basic-r2` 记的是 `RED ✓`，但那个种子文件单独跑是 **4/4 全过**——它**仍然是空转的**。

原因：模型没有去修那个文件，而是**另外新增了一个会失败的测试文件**。
整组跑当然失败，于是被判为"修复成功"，而真正要修的文件原封不动。

原口径**在测"整组有没有失败"，而不是"那个空转文件有没有被修好"**——这是度量效度错误。

---

## 5. 严格口径重算（不重跑模型，直接分析已有 16 个工作区）

严格口径要求三件事同时成立：

1. 种子文件仍然存在；
2. **单独运行种子文件**必须失败（RED）；
3. 种子文件必须引用了 `backend/src` 下的实现模块。

```text
trial                         种子仍在   种子单独RED   种子引用实现   目录下测试文件数
REQ-1-basic-r1                True      False        False        2
REQ-1-basic-r2                True      False        False        2
REQ-1-import_aware-r1         True      False        False        2
REQ-1-import_aware-r2         True      False        False        1   ← 网络失败，未发生重写
REQ-2-basic-r1                True      False        False        2
REQ-2-basic-r2                True      False        False        2
REQ-2-import_aware-r1         True      False        False        2
REQ-2-import_aware-r2         True      False        False        2
REQ-3-basic-r1                True      False        False        2
REQ-3-basic-r2                True      False        False        2
REQ-3-import_aware-r1         True      False        False        2
REQ-3-import_aware-r2         True      False        False        2
REQ-4-basic-r1                True      False        False        2
REQ-4-basic-r2                True      False        False        2
REQ-4-import_aware-r1         True      False        False        2
REQ-4-import_aware-r2         True      False        False        2

处理组            严格口径：种子文件被修好
basic           0/8
import_aware    0/8
```

**16/16 的种子文件都还在，且都还是空的。完成的 15 次重写，100% 选择了"新增一个测试文件"。**

逐例确认（`REQ-1-basic-r1`）：

```text
tests/items.list.test.js       <- 种子，仍是 4/4 全过（自造 mock，无 import）
tests/req1.list.test.js        <- 模型新增，require('../src/services/items.service')，失败
```

`REQ-4-basic-r1` 同样是 `validateItem.test.js`（种子，7/7 全过）+ `req4.validate.test.js`（新增，引用实现）。

---

## 6. 这暴露了 WEAK_TEST 门禁的结构性漏洞

现有门禁的判定是：**"这一组测试整体有没有失败"**。因此存在一条稳定可复现的逃逸路径：

```text
1. 写一个空转测试（不 import 实现）        -> 整组通过 -> 门禁报警
2. 不去修它，而是另外新增一个正确的测试文件  -> 整组失败 -> 门禁放行 ✅
3. 实现写好，两个文件都变绿
4. 交付的测试套件里，永久留着一个永远通过、什么也不证明的测试
```

后果不只是"多了个无用文件"：

- 它会**永久拉高测试条数与表面覆盖率**，让质量信号失真；
- 它是**假绿**的种子——如果后续实现被改坏，这个文件仍然全绿，掩盖回归；
- 我的测试篡改护栏（`_guard_implementation_files`）只拦**实现阶段**写测试，
  而这里发生在**写测试阶段**，护栏不覆盖。

也就是说：**RED 门禁只能证明"存在至少一个失败的测试"，无法证明"每个测试都是有意义的"。**

---

## 7. 对 import 审计的最终定位

| 用途 | 结论 |
|---|---|
| 作为重写反馈的增强 | **无效**（0/8 vs 0/8），收益为零，不应作为卖点 |
| 作为检测器 | **必需**。它是唯一能发现"空转文件与正常文件并存"的手段，RED 门禁在原理上看不见 |

我上一轮把它的价值放在"提高重写成功率"上是错的；它真正的价值在**度量与拦截**。

---

## 8. 建议的门禁修订

按修复力度从小到大：

**A. 收紧运行范围（最小改动）**
测试生成/重写阶段只允许写**计划内声明的测试路径**，多出来的测试文件一律拒绝——
和实现阶段的护栏同理。这样模型只能去修那个种子文件，逃逸路径被堵死。

**B. 逐文件 RED（更严格）**
进入实现前，**每个测试文件单独运行都必须失败**，而不是整组失败。

**C. 审计纳入门禁（最全面）**
对计划内每个测试文件做 import 审计，任何 `NO_IMPLEMENTATION_IMPORT` 都判 `WEAK_TEST`。

建议 **A + C**：A 堵住新增文件的逃逸，C 保证留下来的文件是有意义的。
B 更严格但成本随文件数线性增长。

---

## 9. 效度说明（threats to validity）

必须如实标注：

1. **起点是诱导的，不是自然发生的**。本实验测的是"给定一个空转测试，反馈措辞的因果效应"，
   而非空转测试的自然发生率（后者由 RED 门禁在线上统计）。
2. **样本量小**：每组 n=8，只能检出大效应。但 0/8 vs 0/8 是一个很强的零结果；
   第 5 节那个"新增文件"的模式是 15/15，几乎是确定性的行为。
3. **单一模型**（`deepseek-v4-pro`）。换更弱的模型，行为可能不同。
4. **严格口径是事后发现的**（post-hoc）。之所以采信它，是因为"新增文件"这一机制
   被直接观测证实，而不是因为它让结果更好看——恰恰相反，它让结果更糟（8/8 → 0/8）。
5. 1 次试验因网关断连未完成，已在表中标出。

---

## 10. 附：本轮新增/改动的文件

| 文件 | 作用 |
|---|---|
| [factory/testaudit.py](factory/testaudit.py) | 测试静态审计：抽取 import 目标、判断是否引用实现根 |
| [tools/experiment_weak_feedback.py](tools/experiment_weak_feedback.py) | 配对 A/B 实验（含诱导起点、Fisher 检验） |
| [tools/reanalyze_experiment.py](tools/reanalyze_experiment.py) | 严格口径重新分析（不调模型） |
| `factory/loop.py` | `_weak_test_feedback` 支持 `basic` / `import_aware` 两种形态 |
| `factory/config.py` | 新增 `weak_feedback_mode`、`implementation_root` |
| [experiment_result.json](experiment_result.json) | 原始试验记录 |
| [experiment_reanalysis.json](experiment_reanalysis.json) | 严格口径重算结果 |
