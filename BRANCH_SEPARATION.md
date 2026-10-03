# 工作流分支隔离

**建立时间**：2026-09-30 23:15
**基线提交**：`dc62868`
**目的**：两条工作流在同一目录并发写文件，已造成多次测量污染与归因困难；本文件记录分支结构与隔离方式。

---

## 一、为什么需要分支隔离

### 事故清单（本会话累计）

| # | 事故 | 后果 |
|---|---|---|
| 1 | v2 运行**进行到一半**时 `factory/loop.py`、`factory/testaudit.py` 被并发方改动（00:35，运行 23:57→00:47） | 侥幸没坏（Python 已加载模块），但无法证明 |
| 2 | closure6 第 4 轮运行时 `factory/loop.py`、`factory/version.py`、`tools/test_gates.py` 被改 | 同上，靠 `guard` 才证明测量未受污染 |
| 3 | 架构文档 `软件工厂-项目架构文档.md` 在我重写后被并发方改动 | 幸而我的 v1.1 修订完整存活（1426→1428 行） |
| 4 | **`all_passed` 假突破**：closure6 在 REQ-3 期间因 git 报错崩溃，写出只含 **2/6** 个需求的部分报告，被判为「全链通过」 | 差点产出假结论 |
| 5 | 目录里出现对方的临时产物：`.pack_v*.py`、`apidoc/`（2.8 MB）、`.zipv/`、`dist/*.zip` | 打包时混入 |

### 根因

**两条工作流共用同一个工作目录，且没有版本边界。**
仅靠"约定不碰对方文件"无法生效 —— 已实测多次被打破。

---

## 二、为什么不能回溯式干净切分

尝试过按文件归属三分，结论是**做不到**：

```
文件                         mtime        归属
factory/adapter.py          09-28 16:21   双方都未动（最近）
factory/workspace.py        09-28 21:49   双方都未动
factory/testplan.py         09-29 16:04   我
factory/config.py           09-29 23:20   我
factory/testaudit.py        09-30 00:35   我 + 对方
factory/store.py            09-30 00:06   对方（call_edges / node_contracts）
factory/llm.py              09-30 23:11   我 + 对方
factory/models.py           09-30 23:05   我 + 对方
factory/pipeline.py         09-30 23:05   我 + 对方（容器节点）
factory/loop.py             09-30 23:12   我 + 对方（TEST_BROKEN 规则修正）
main.py                     09-30 22:08   我 + 对方
tools/test_gates.py         09-30 23:14   双方（各自追加断言）
```

**几乎每个核心文件都已被双方交替修改**，且并发方**此刻仍在写**（23:05–23:14）。
hunk 级的归属无法在不猜测的前提下还原。

**所以采取的是**：冻结当前混合态为**共同基线**，两个分支从此各自前进。
这是诚实的做法 —— 不假装能还原历史，但保证未来不再混。

---

## 三、分支结构

```
dc62868  chore(isolation): 建立工作流隔离        ← 共同基线
   │
   ├── main            集成线（当前 HEAD，并发方在此继续提交）
   ├── factory-verify  本工作流（工厂验证：门禁 / 审计 / 度量 / DAG 断言 / 架构核验）
   └── sdk-contract    并发工作流（SDK 契约：call_edges / node_contracts / API 文档）
```

三个分支**都指向同一基线** `dc62868`，从此刻起各自独立。

### 物理隔离：git worktree

```
/home/duanlinghui/dshwork                    dc62868 [main]              ← 并发方
/home/duanlinghui/dshwork/.wt/factory-verify dc62868 [factory-verify]    ← 本工作流
```

**worktree 的价值**：两个**独立的工作目录**共享同一个对象库。
各自检出不同分支，互不干扰 —— 不是"约定不碰"，而是**文件系统层面碰不到**。

### 隔离实证

```bash
$ echo probe > .wt/factory-verify/ISOLATION_PROBE.txt
$ ls .wt/factory-verify/ISOLATION_PROBE.txt   → 存在
$ ls ISOLATION_PROBE.txt                       → 不存在 ✅
$ git -C .wt/factory-verify status --short     → ?? ISOLATION_PROBE.txt
$ git status --short                           → （不含该文件）
```

---

## 四、归属清单

### 本工作流（`factory-verify`）拥有

| 路径 | 内容 |
|---|---|
| `factory/testaudit.py` | 依赖使用审计 / mock 检测 / 注入旁路检测 |
| `factory/testplan.py` | 测试计划、基线守卫、间接/被 mock 依赖声明 |
| `factory/config.py` | 门禁开关族 |
| `factory/models.py` | 结果字段（保留对方新增的字段） |
| `factory/loop.py` | 门禁逻辑（**注意**：对方也改了 TEST_BROKEN 规则，见下） |
| `tests/test_dag.py` | D1–D10 图性质断言 |
| `tools/*`（除 `test_call_edges.py`） | 验证与运维工具 |
| `DAG_DESIGN.md` · `ARCHITECTURE_AUDIT.md` · `ISOLATION.md` · `BRANCH_SEPARATION.md` | 设计/核验文档 |
| `V1_RELEASE.md` · `V2_RELEASE.md` | 发布说明 |

### 并发工作流（`sdk-contract`）拥有

| 路径 | 内容 |
|---|---|
| `factory/version.py` | 代码版本锚点 |
| `factory/store.py`（call_edges / node_contracts 部分） | 追溯表补全 |
| `factory/pipeline.py`（容器节点 / ROOT 部分） | 需求树分解 |
| `tools/test_call_edges.py` | call edge 方向断言 |
| `API_DOC_CONFORMANCE.md` · `sdk_contract_report.json` · `v2_contract_report.json` | API 文档一致性 |
| `.pack_v1.py` · `.pack_v2.py` | 打包脚本 |
| `.apidoc/` · `.zipv/` | 文档快照（临时） |

### 共存（双方都在改，需协调）

| 文件 | 我的改动 | 对方的改动 |
|---|---|---|
| `factory/loop.py` | TEST_BROKEN 判据、依赖门禁接入、回归检查 | **修正了我的 TEST_BROKEN 规则**（见下） |
| `factory/llm.py` | 成本记账字段、prompt 体积 | SDK 后端补齐 |
| `factory/models.py` | 门禁结果字段 | （字段扩展） |
| `main.py` | （入口不变） | 退出码语义 |
| `tools/test_gates.py` | T1–T25 | 追加 call edge 断言 |

### 一处值得记录的协作

并发方提交 `b582582 fix(red-gate): 缺实现模块导致的 0 个测试是有效 RED，不是 TEST_BROKEN`
**修正了我引入的一个过宽规则**：

- 我的规则：`not passed and total == 0` → TEST_BROKEN
- 他们指出：TDD 里实现尚未写出时，测试 import 实现模块必然失败、收集到 0 个测试 ——
  **这本就是有效的 RED**，不是坏测试

**他们是对的。** 我的规则会把每一次首次运行都误判为坏测试。

---

## 五、同步流程（重要）

**两个分支不自动同步。** 交换改动必须显式进行：

```bash
# 从对方分支取某个文件（不合并整个分支）
cd /home/duanlinghui/dshwork/.wt/factory-verify
git checkout sdk-contract -- factory/version.py

# 或看对方改了什么
git diff factory-verify sdk-contract -- factory/loop.py

# 把对方的分支合并进来（会产生 merge commit，冲突显式暴露）
git merge sdk-contract
```

**为什么不用 rebase**：rebase 会重写历史，而两个分支都在被持续提交，
rebase 造成的冲突比 merge 更难归因。

**冲突处理原则**：共存文件（`loop.py` / `llm.py` / `models.py` / `test_gates.py`）
优先保留**后验证过的一方** —— 即谁有断言覆盖就保留谁，另一方把改动重做在断言之上。

---

## 六、guard 的配套修复（本轮附带）

分支隔离暴露了一个**会让隔离失效**的缺陷：

**事故 4 的完整根因**：`guard` 把运行放在 `.workstreams/` 下，而该路径被工作区 `.gitignore` 忽略。
工厂的 git 层执行 `git add .`（cwd = 输出目录），git 向上解析到**工作区仓库**，
报 `The following paths are ignored ... .workstreams` 并抛 `RuntimeError`。

**修复**：`guard` 现在为每个快照建立**独立 git 仓库**（`git init` + 初始提交）。
`git rev-parse --show-toplevel` 停在快照，外层 `.gitignore` 不再适用，
且隔离更彻底 —— 连仓库都独立。

**配套修复 2**：`gateway_watch.py` 的 `all_passed` 判据原本只检查
「报告里出现的是否全通过」，不检查**报告是否完整**，
于是把 2/6 的部分报告打成「全链通过」。现在要求
`reported == requirements_total` 才可能为 True。

---

## 七、当前状态

| 项 | 状态 |
|---|---|
| 分支 | `main` / `factory-verify` / `sdk-contract`，均在 `dc62868` |
| worktree | 2 个（主树 + `.wt/factory-verify`） |
| 隔离实证 | ✅ 通过（worktree 内文件不出现在主树） |
| `guard` git 缺陷 | ✅ 已修（快照自带 `.git`） |
| `all_passed` 假阳性 | ✅ 已修（要求报告完整） |
| closure6 全链结果 | ❌ **仍无有效数据** —— 唯一一次「全链通过」是假突破（2/6 部分报告）；其余轮次均被环境打断 |

### 待办

1. 用修好的 `guard` + 修好的 `all_passed` **重跑 closure6**，取得第一份完整数据
2. `factory-verify` 分支上实现 `factory/dag.py`，使 `tests/test_dag.py` 的 D1–D10 转绿
3. 与 `sdk-contract` 分支做一次显式合并，解决共存文件的冲突

---

## 八、操作纪律：**禁止跨 worktree 用 `cp`** **〔v1.1 新增〕**

### 规则

> **不得在 worktree 之间用 `cp` 同步文件。用 `git cherry-pick` 或 `git rebase`。**

### 理由（两次真实事故）

我用 `cp` 在 `.wt/factory-verify` 与主树之间同步文件，**两次**都造成了污染：

```
把主树的 loop.py / pipeline.py / test_gates.py 复制进分支
  -> 它们引用 ModelQuotaExhaustedError / _is_quota_exhausted
  -> 而分支的 llm.py 是重构前版本，没有这两个符号
  -> ImportError
```

| 次 | 时间 | 波及提交 | 后果 |
|---|---|---|---|
| 1 | 骨架提交那次 | `b062436` / `008e10b` / `c43bcf9` / `7dd9394` **四个提交均不可导入** | 分支一路「绿色」通过报告，因为 `check_all` 一直在**主树**跑 |
| 2 | P0 规格对齐那次 | 分支 `check_all` 3 类失败 | 本轮补跑前才发现 |

### 为什么按文件 `cp` 必然出问题

**按文件挑着 `cp`，必然产生「A 模块新版 + B 模块旧版」的半新半旧状态。**
而 `import` 是最先断裂的地方 —— 新模块引用了旧模块里还不存在的符号。

同步的**单位是提交，不是文件**。`git cherry-pick` / `git rebase` 保证整体一致性，
且留下可追溯的记录（谁在何时把什么带了过来）。

### 正确做法

```bash
# 取某个提交的整体改动
git cherry-pick <sha>

# 取整个分支的最新状态
git merge <branch>          # 或 git rebase <branch>
```

若确实只要一个文件，也应**说明理由并单独提交**，而不是批量 `cp`。

### `test_imports.py` 的角色

`tools/test_imports.py` **两次都立刻抓到了污染** —— 守卫有效。

但它是**事后发现**，不是事前阻止。**禁止 `cp` 是事前阻止。**
两者都要有：事前靠纪律，事后靠守卫。

---

## 九、`llm.py` 前向同步的影响 **〔v1.1 新增〕**

第二次污染修复时，分支选择**前向同步**（取用主树的 `llm.py`）而非回退，理由：
并发方的 llm 重构（配额终局判定 `_is_quota_exhausted`、两后端重试对拍
`_retry_allowance`）**已在 git 历史中提交**（`f200cc7` / `686ca1b` / `2220d7b` 在 `main` 上），
源可追溯，故前向同步是干净的。

**后果**：分支 `llm.py`（`8b96cf18bbd0`）与基线样本 1–4 的（`53b57986690c`）**差 110 行**。

因此：

| 用途 | 应当从哪跑 |
|---|---|
| **验证特性**（P0-1/P0-2 等） | `factory-verify`（含特性） |
| **基线对比**（通过率方差） | `.wt/baseline4`（固定提交 `1157538`，与样本 1–4 逐字节一致） |

**分析通过率时不得声称「与 cf3 可比」** —— cf3 用的是旧 `llm.py`。
两者只可并列陈述，不可直接对比归因。
