"""工作流隔离：把测量运行跑在**冻结副本**上，并在运行前后校验副本未被改动。

=======================  为什么需要它  =======================
实测事故（v2 运行）：运行期间工作区被另一条工作流并发修改，
`factory/{store,generator,llm,pipeline,loop,testaudit}.py` 与多个 tools/ 文件被改，
其中 `loop.py`、`testaudit.py` 的改动发生在运行**进行到一半**时（00:35，运行 23:57→00:47）。

那次侥幸没坏：Python 进程启动时已加载模块，之后的磁盘编辑不影响运行中的进程。
但这个「侥幸」不可依赖 —— 一旦并发方改动发生在启动前的一瞬，或者运行器以子进程重新导入，
测量就会静默对应到一份**说不清的代码**，而报告上不会有任何痕迹。

所以隔离的目标不是「请别人别改」（做不到），而是：
  **让每一次测量都能证明自己跑在哪份代码上，且运行期间那份代码没有变。**

=======================  三个命令  =======================
  snapshot --label X          把代码冻结到 .workstreams/X/，并写 manifest（sha256）
  verify   --label X          把冻结副本与当前工作区比对，报告漂移
  guard    --label X -- cmd   冻结 -> 在副本内运行 cmd -> 校验副本未被改动

`guard` 是主入口：它跑在副本目录里（cwd = 副本），并发方改的是工作区，物理上够不着。

=======================  归属清单  =======================
manifest 里带 `owned` 标记：哪些文件属于本工作流。
比对时能区分「我方文件被改」（要警惕）与「他方文件被改」（与本测量无关）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / ".workstreams"

# 参与冻结的顶层条目（运行 factory 所需的最小充分集）
FROZEN = (
    "factory", "tools", "schemas", "template", "skills", "examples",
    "requirements_sample", "requirements_probe", "requirements_probe_closure6",
    "requirements_probe_chain5", "requirements_probe_subset3", "requirements_probe_roots",
    "arcbench-agent-runtime/src", "arcbench-agent-runtime/pyproject.toml",
    "main.py", "requirements.txt",
)

# 本工作流**拥有**的文件（会主动修改）。其余文件即使变化也不影响本测量，只需记录。
OWNED_PREFIXES = ("factory/", "tools/", "schemas/", "main.py")
OWNED_FILES = {
    "factory/loop.py", "factory/generator.py", "factory/pipeline.py", "factory/config.py",
    "factory/models.py", "factory/testaudit.py", "factory/testplan.py", "factory/llm.py",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iter_files(base: Path):
    for entry in FROZEN:
        target = base / entry
        if target.is_file():
            yield target.relative_to(base)
        elif target.is_dir():
            for p in sorted(target.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts and "node_modules" not in p.parts:
                    yield p.relative_to(base)


def build_manifest(base: Path) -> dict:
    rows = []
    for rel in iter_files(base):
        p = base / rel
        try:
            rows.append({
                "path": str(rel),
                "sha256": sha256(p),
                "size": p.stat().st_size,
                "owned": str(rel).startswith(OWNED_PREFIXES),
            })
        except OSError:
            continue
    return {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "file_count": len(rows),
        "owned_count": sum(1 for r in rows if r["owned"]),
        "files": rows,
    }


def cmd_snapshot(args: argparse.Namespace) -> int:
    label = args.label
    dest = STORE / label
    if dest.exists():
        if not args.force:
            print(f"❌ 快照已存在: {dest}（用 --force 覆盖）")
            return 1
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for entry in FROZEN:
        src = ROOT / entry
        if not src.exists():
            continue
        tgt = dest / entry
        tgt.parent.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            shutil.copy2(src, tgt)
        else:
            shutil.copytree(src, tgt,
                            ignore=shutil.ignore_patterns("__pycache__", "node_modules", "*.pyc"))
    manifest = build_manifest(dest)
    # ★ 记录**快照的来源根**：陈旧判定必须与「快照是从哪份工作区冻结的」比，
    # 而不是与当前进程的 ROOT 比 —— 两者可能不同（例如 `--store` 指向别处，
    # 或测试用 with_root 临时切换）。初版用模块级 ROOT，导致
    # 现有断言 ⑥（guard 检出副本被改动）误判为陈旧而回归。
    manifest["source_root"] = str(ROOT)
    (dest / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 快照已冻结: {dest}")
    print(f"   {manifest['file_count']} 个文件（其中本工作流拥有 {manifest['owned_count']} 个）")
    return 0


def load_manifest(label: str) -> dict | None:
    path = STORE / label / "MANIFEST.json"
    if not path.is_file():
        print(f"❌ 找不到快照 manifest: {path}")
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def compare(label: str, *, base: Path, against_manifest: dict, against_label: str) -> tuple[list, list, list]:
    """把 base 与 manifest 比对，返回 (新增, 修改, 删除)；每项带 owned 标记。"""
    old = {r["path"]: r for r in against_manifest["files"]}
    new = {r["path"]: r for r in build_manifest(base)["files"]}
    added = [p for p in new if p not in old]
    removed = [p for p in old if p not in new]
    changed = [p for p in new if p in old and new[p]["sha256"] != old[p]["sha256"]]
    def mark(paths):
        return sorted((p, (new.get(p) or old.get(p, {})).get("owned", False)) for p in paths)
    return mark(added), mark(changed), mark(removed)


def report_drift(added, changed, removed, *, title: str) -> bool:
    """打印漂移报告；返回 True 表示**本工作流拥有的文件**发生了漂移。"""
    total = len(added) + len(changed) + len(removed)
    print(f"\n--- {title} ---")
    if not total:
        print("  ✅ 无漂移（逐文件 sha256 完全一致）")
        return False
    mine = False
    if changed:
        print(f"  修改 {len(changed)} 个:")
        for path, owned in changed:
            tag = "【我方】" if owned else "（他方）"
            print(f"    {tag} {path}")
            mine = mine or bool(owned)
    if added:
        print(f"  新增 {len(added)} 个:")
        for path, owned in added:
            print(f"    {'【我方】' if owned else '（他方）'} {path}")
            mine = mine or bool(owned)
    if removed:
        print(f"  删除 {len(removed)} 个:")
        for path, owned in removed:
            print(f"    {'【我方】' if owned else '（他方）'} {path}")
            mine = mine or bool(owned)
    return mine


def cmd_verify(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.label)
    if manifest is None:
        return 1
    added, changed, removed = compare(args.label, base=ROOT,
                                      against_manifest=manifest, against_label=args.label)
    mine = report_drift(added, changed, removed,
                        title=f"冻结副本 .workstreams/{args.label}  vs  当前工作区")
    print()
    if not (added or changed or removed):
        print("✅ 工作区与冻结副本一致")
        return 0
    if mine:
        print("⚠️ **本工作流拥有的文件**发生漂移 —— 冻结副本已与工作区分叉")
        print("   → 若要基于旧结论继续测量，请跑 : guard（它用冻结副本，不受此影响）")
        return 2
    print("ℹ️ 仅他方文件漂移，与本工作流的测量无关")
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    """冻结 -> 在副本内运行命令 -> 校验副本未被改动。"""
    label = args.label
    dest = STORE / label
    if not dest.exists() or args.refresh:
        rc = cmd_snapshot(argparse.Namespace(label=label, force=True))
        if rc:
            return rc
    manifest = load_manifest(label)
    if manifest is None:
        return 1

    # ★ 陈旧快照守卫：**静默复用**会把上次的旧代码当成这次的被测对象。
    #
    # 实测事故：修复分支后重跑 `guard --label contractfreeze`，它复用了 16:07
    # 那次失败留下的快照（含跨 worktree 污染代码），于是**修复完仍然 ImportError**。
    # 更早还有一次：同名快照复用导致测量跑在旧代码上，报告却看起来正常。
    #
    # 静默复用是「验证基础设施失效但伪装成正常」的又一变体 ——
    # 所以这里**默认拒绝**，要求显式 --refresh 或换 label。
    stale = _stale_owned_files(label, manifest)
    if stale and not args.refresh:
        print(f"❌ 快照 `{label}` 已陈旧 —— 它冻结的是**旧代码**，不能用于本次测量。")
        print(f"   快照: {dest}")
        print(f"   与本工作流当前代码不一致的文件（{len(stale)} 个）:")
        for rel in stale[:8]:
            print(f"     - {rel}")
        if len(stale) > 8:
            print(f"     ...（共 {len(stale)} 个）")
        print("   修法：加 `--refresh` 让 guard 重新冻结，或换一个 label。")
        print("   （这不是警告 —— 复用旧快照会让整轮测量结论指向错误的代码。）")
        return 2
    return _guard_run(args, dest, manifest)


def _stale_owned_files(label: str, manifest: dict) -> list[str]:
    """返回「本工作流拥有、且与快照不一致」的相对路径。

    基准是 manifest 里记录的 `source_root`（快照的来源工作区），
    不是当前进程的 ROOT —— 二者可能不同（`--store` / with_root 切换）。
    缺失 `source_root` 的旧快照回退到 ROOT（并因此可能保守误判，可接受）。
    """
    base = Path(manifest.get("source_root") or ROOT)
    stale: list[str] = []
    for entry in manifest.get("files", []):
        rel = entry.get("path", "")
        if not str(rel).startswith(OWNED_PREFIXES):
            continue
        cur = base / rel
        if not cur.is_file():
            stale.append(f"{rel}（已删除）")
            continue
        if sha256(cur) != entry.get("sha256"):
            stale.append(rel)
    return stale


def _guard_run(args: argparse.Namespace, dest: Path, manifest: dict) -> int:

    if not args.command:
        print("❌ guard 需要命令，例如：-- python3 main.py ...")
        return 1

    # ★ 让快照**自带 .git**，切断与工作区仓库的关系。
    #
    # 实测事故：快照放在 `.workstreams/` 下，而工作区的 .gitignore 忽略了该路径。
    # 工厂的 git 层执行 `git add .`（cwd = 输出目录），git 向上解析到**工作区仓库**，
    # 于是报「The following paths are ignored ... .workstreams」并抛 RuntimeError，
    # 运行在 REQ-3 期间崩溃，异常处理器写出只含 2/6 个需求的**部分报告**。
    #
    # 更糟的是它**看起来像成功**：初版的 all_passed 只检查「报告里的是否全通过」，
    # 于是那个 2/6 的部分报告被打成「全链通过」——假突破。
    #
    # 快照自带 .git 后，`git rev-parse --show-toplevel` 停在快照，
    # 外层 .gitignore 不再适用，且隔离更彻底（连仓库都独立）。
    snap_git = dest / ".git"
    if not snap_git.exists():
        subprocess.run(["git", "init", "-q", "."], cwd=str(dest), check=False)
        subprocess.run(["git", "config", "user.email", "factory@local"], cwd=str(dest), check=False)
        subprocess.run(["git", "config", "user.name", "factory"], cwd=str(dest), check=False)
        subprocess.run(["git", "add", "-A"], cwd=str(dest), check=False)
        subprocess.run(["git", "commit", "-q", "-m", "snapshot base"], cwd=str(dest), check=False)
        print(f"   （已为快照建立独立 git 仓库：{snap_git}）")

    before_ws = build_manifest(ROOT)
    print(f"\n▶️  在冻结副本内运行: {' '.join(args.command)}")
    print(f"    cwd = {dest}（并发方改的是工作区，物理上够不着）\n")

    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = f"{dest / 'arcbench-agent-runtime' / 'src'}:{dest}"
    proc = subprocess.run(args.command, cwd=str(dest), env=env)

    # 副本自身是否被改动（命令若写了副本内的源码，测量基础就被污染了）
    after_snap = build_manifest(dest)
    snap_changed = [
        r["path"] for r in after_snap["files"]
        if next((o["sha256"] for o in manifest["files"] if o["path"] == r["path"]), None) != r["sha256"]
    ]
    # 工作区在运行期间是否被改动（记录，用于归因）
    after_ws = build_manifest(ROOT)
    ws_changed = [
        r["path"] for r in after_ws["files"]
        if next((o["sha256"] for o in before_ws["files"] if o["path"] == r["path"]), None) != r["sha256"]
    ]

    print(f"\n{'=' * 74}")
    print(f"隔离校验（命令退出码 {proc.returncode}）")
    print(f"{'=' * 74}")
    snap_mine = [p for p in snap_changed if str(p).startswith(OWNED_PREFIXES)]
    if snap_changed:
        print(f"  ⚠️ 冻结副本内有 {len(snap_changed)} 个文件被本次运行改动:")
        for p in snap_changed[:10]:
            print(f"      {'【我方】' if str(p).startswith(OWNED_PREFIXES) else '（他方）'} {p}")
        if snap_mine:
            print("  ❗ 本工作流的源码被运行本身改动 —— 该次测量基础已受污染")
    else:
        print("  ✅ 冻结副本逐文件未变 —— 本次测量**可证明**跑在冻结的那份代码上")

    ws_mine = [p for p in ws_changed if str(p).startswith(OWNED_PREFIXES)]
    ws_other = [p for p in ws_changed if not str(p).startswith(OWNED_PREFIXES)]
    if ws_changed:
        print(f"  ℹ️ 运行期间工作区有 {len(ws_changed)} 个文件被改动"
              f"（我方 {len(ws_mine)} / 他方 {len(ws_other)}）—— 对本次测量无影响，因为跑的是副本")
    else:
        print("  ✅ 运行期间工作区无改动")
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="工作流隔离：冻结副本 + 漂移检测 + 受控运行")
    parser.add_argument("--store", default="", help="快照存放目录（默认 <ROOT>/.workstreams）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("snapshot", help="冻结当前代码到 .workstreams/<label>/")
    p.add_argument("--label", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("verify", help="冻结副本 vs 当前工作区的漂移")
    p.add_argument("--label", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("guard", help="在冻结副本内运行命令，并校验副本未被改动")
    p.add_argument("--label", required=True)
    p.add_argument("--refresh", action="store_true", help="运行前重新冻结")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_guard)

    args = parser.parse_args()
    if args.store:
        # 允许把快照存到别处 —— 测试与多工作流并行时都需要，
        # 否则子进程会按脚本自身路径算出真实 ROOT，污染真实工作区。
        globals()["STORE"] = Path(args.store).resolve()
    if getattr(args, "command", None) and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
