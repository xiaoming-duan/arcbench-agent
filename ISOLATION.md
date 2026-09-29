# 工作流隔离说明

## 为什么需要它

**实测事故**：v2 运行期间（09-29 23:57 → 09-30 00:47），工作区被**另一条工作流并发修改**：

| 时间 | 被改文件 |
|---|---|
| 00:06 | `factory/store.py` |
| 00:12 | `factory/pipeline.py` |
| 00:18 | `factory/generator.py`、`factory/llm.py` |
| **00:35** | **`factory/loop.py`、`factory/testaudit.py`**（运行进行到一半时） |
| 00:36 | `tools/test_gates.py` |
| 00:52+ | `tools/check_all.py`、`tools/test_gates.py`（仍在继续） |

那次**侥幸没坏**：Python 进程在 23:57:27 启动时已加载模块，之后的磁盘编辑不影响运行中的进程。

但这个"侥幸"不可依赖：

- 若并发改动发生在**启动前的一瞬**，测量就会对应到一份说不清的代码；
- 若运行器以子进程**重新导入**，运行中途的改动会直接生效；
- 无论哪种，**报告上都不会留下任何痕迹** —— 你会得到一份看起来干净、实际无法归因的结果。

所以隔离的目标不是"请别人别改"（做不到），而是：

> **让每一次测量都能证明自己跑在哪份代码上，且运行期间那份代码没有变。**

---

## 三个命令

### 1. 冻结快照

```bash
python3 tools/workstream.py snapshot --label v2-verified --force
```

把运行所需的代码集复制到 `.workstreams/<label>/`，并写 `MANIFEST.json`（逐文件 sha256 + `owned` 标记）。

冻结范围：`factory/`、`tools/`、`schemas/`、`template/`、`skills/`、`examples/`、
`requirements*`、`arcbench-agent-runtime/src`、`main.py`、`requirements.txt`。

### 2. 漂移检测

```bash
python3 tools/workstream.py verify --label v2-verified
```

把冻结副本与当前工作区逐文件比对，报告**新增 / 修改 / 删除**，并区分：

- **【我方】** —— 本工作流拥有的文件（`factory/`、`tools/`、`schemas/`、`main.py`）发生漂移 → 退出码 2，说明冻结副本已与工作区分叉
- **（他方）** —— 其他文件变化 → 退出码 0，与本工作流的测量无关

### 3. 受控运行（主入口）

```bash
python3 tools/workstream.py guard --label v2-verified -- \
  python3 main.py requirements_probe_closure6 --output-dir out --type web \
  --generator llm --install-deps auto --max-repairs 3
```

做的事：**在冻结副本内运行命令**（`cwd = .workstreams/<label>/`），运行前后各校验一次。

因为跑的是副本，**并发方改的是工作区，物理上够不着** —— 这是真正的隔离，不是君子协定。

---

## 输出解读

```
▶️  在冻结副本内运行: python3 main.py ...
    cwd = .../.workstreams/v2-verified（并发方改的是工作区，物理上够不着）

...（命令输出）...

==========================================================================
隔离校验（命令退出码 0）
==========================================================================
  ✅ 冻结副本逐文件未变 —— 本次测量**可证明**跑在冻结的那份代码上
  ℹ️ 运行期间工作区有 3 个文件被改动（我方 0 / 他方 3）—— 对本次测量无影响，因为跑的是副本
```

两种告警要分清：

| 告警 | 含义 | 严重性 |
|---|---|---|
| `❗ 本工作流的源码被运行本身改动` | 命令写了副本内的源码，**该次测量基础已受污染** | 高 —— 需重跑 |
| `ℹ️ 运行期间工作区有 N 个文件被改动` | 并发方在动工作区，但**不影响本次测量** | 记录即可 |

---

## 归属清单（`owned` 标记）

`MANIFEST.json` 里每个文件带 `owned` 字段。判定规则：

- 本工作流**拥有**：`factory/`、`tools/`、`schemas/`、`main.py` —— 这些是我会主动修改的
- 其余（`requirements*`、`arcbench-agent-runtime/`、docs）视为他方或共享

有了它，漂移报告才能回答"**这个变化要不要紧**"，而不是笼统地报"有变化"。

---

## 验证状态

`tools/test_workstream.py` —— **8 项断言**，已登记进 `check_all`：

| 断言 | 覆盖 |
|---|---|
| ① snapshot 成功 + manifest sha256 完整 | 正向 |
| ①c owned 标记正确（我方/他方） | 归属 |
| ② **无改动时报无漂移** | 正向（防误报） |
| ③ **我方文件被改 → 必须检出且标 owned** | **负向** |
| ④ **他方文件被改 → 必须检出且不标 owned** | **负向** |
| ⑤ 新增与删除都能检出 | 负向 |
| ⑥ **guard 检出「运行改动了副本内我方源码」** | **负向** |

第 ③④⑤⑥ 项是关键：**一个检测不出漂移的检测器，比没有检测器更危险** ——
它会让被污染的测量看起来是干净的。所以负向路径必须有断言。

---

## 文件备份

除隔离外，另有一份**时点备份**：

| 文件 | 内容 |
|---|---|
| `backups/ws-<时间戳>.tar.gz` | 工作区完整归档（排除 `out-*` / `node_modules` / 缓存） |
| `backups/ws-<时间戳>.manifest.json` | **逐文件 sha256 清单** —— 让备份可校验，同时是漂移基线 |
| `backups/LATEST` | 最新备份标签 |

创建方式见下方"复现"。备份与快照的区别：

- **备份** = 时点存档，用于回滚与审计
- **快照**（`.workstreams/`）= 可执行的冻结副本，用于受控运行

---

## 复现

```bash
# 备份（归档 + 逐文件 sha256 清单）
TS=$(date '+%Y%m%d-%H%M%S')
tar --exclude='out-*' --exclude='node_modules' --exclude='.npm-cache' \
    --exclude='.cache' --exclude='__pycache__' --exclude='backups' \
    -czf "backups/ws-$TS.tar.gz" .

# 冻结 + 校验 + 受控运行
python3 tools/workstream.py snapshot --label v3 --force
python3 tools/workstream.py verify --label v3
python3 tools/workstream.py guard --label v3 -- <你的命令>
```

## 已知边界

1. **`.workstreams/` 会随快照数量增长**。每个快照约 1 MB 级；不再需要的用 `rm -rf .workstreams/<label>` 清理。
2. **`guard` 不阻止并发方修改工作区**，只是让这种修改**无法影响本次测量**，并且**留下记录**。
3. **快照不含 `node_modules`**。需要 npm 依赖的运行（`--install-deps auto`）会在副本内重新安装，成本与在工作区跑相同。
4. 若并发方也修改 `.workstreams/`，隔离即失效 —— 该目录是本工作流私有的约定，未做强制保护。
