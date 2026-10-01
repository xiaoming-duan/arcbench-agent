"""工作流隔离工具（tools/workstream.py）的断言测试。

为什么隔离工具本身需要断言：
  它的价值全在「能否真的检出漂移」上 —— 一个检测不出漂移的检测器，
  比没有检测器更危险：它会让被污染的测量看起来是干净的。
  所以必须验证**正向**（无漂移时报无漂移）与**负向**（有漂移时必须报出来）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import workstream as ws  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'}  {name}" + (f"   {detail}" if detail else ""))


def make_fake_ws(root: Path) -> None:
    """造一个最小的假工作区：有 owned 文件（factory/）和 other 文件（docs/）。"""
    (root / "factory").mkdir(parents=True)
    (root / "tools").mkdir(parents=True)
    (root / "requirements_sample").mkdir(parents=True)
    (root / "factory/loop.py").write_text("# owned\n", encoding="utf-8")
    (root / "tools/helper.py").write_text("# owned\n", encoding="utf-8")
    (root / "requirements_sample/requirements.yaml").write_text("a: 1\n", encoding="utf-8")


def with_root(root: Path):
    """临时把模块的 ROOT/STORE 指到假工作区。"""
    old_root, old_store = ws.ROOT, ws.STORE
    ws.ROOT = root
    ws.STORE = root / ".workstreams"
    return old_root, old_store


def main() -> int:
    print("=" * 78)
    print("工作流隔离断言")
    print("=" * 78)

    tmp = Path(tempfile.mkdtemp(prefix="ws-iso-"))
    make_fake_ws(tmp)
    old_root, old_store = with_root(tmp)
    try:
        # ---- snapshot ----
        rc = ws.cmd_snapshot(type("A", (), {"label": "t1", "force": True})())
        check("① snapshot 成功", rc == 0)
        manifest = ws.load_manifest("t1")
        check("①b manifest 记录了逐文件 sha256",
              manifest is not None and manifest["file_count"] == 3
              and all(len(r["sha256"]) == 64 for r in manifest["files"]),
              f"file_count={manifest['file_count'] if manifest else '?'}")
        check("①c owned 标记正确（factory/ 与 tools/ 为我方，requirements_sample/ 为他方）",
              {r["path"]: r["owned"] for r in manifest["files"]} == {
                  "factory/loop.py": True, "tools/helper.py": True,
                  "requirements_sample/requirements.yaml": False},
              str({r["path"]: r["owned"] for r in manifest["files"]}))

        # ---- 正向：无漂移 ----
        added, changed, removed = ws.compare("t1", base=tmp, against_manifest=manifest,
                                             against_label="t1")
        check("② 无改动时报无漂移", not (added or changed or removed))

        # ---- 负向：改我方文件必须检出，且标记为 owned ----
        (tmp / "factory/loop.py").write_text("# owned CHANGED\n", encoding="utf-8")
        added, changed, removed = ws.compare("t1", base=tmp, against_manifest=manifest,
                                             against_label="t1")
        check("③ 我方文件被改 -> 检出且标记 owned",
              changed == [("factory/loop.py", True)], str(changed))

        # ---- 负向：改他方文件也要检出，但标记为非 owned ----
        (tmp / "requirements_sample/requirements.yaml").write_text("a: 2\n", encoding="utf-8")
        added, changed, removed = ws.compare("t1", base=tmp, against_manifest=manifest,
                                             against_label="t1")
        check("④ 他方文件被改 -> 检出且不标 owned",
              ("requirements_sample/requirements.yaml", False) in changed, str(changed))

        # ---- 新增 / 删除 ----
        (tmp / "tools/new.py").write_text("# new\n", encoding="utf-8")
        (tmp / "factory/loop.py").unlink()
        added, changed, removed = ws.compare("t1", base=tmp, against_manifest=manifest,
                                             against_label="t1")
        check("⑤ 新增与删除都能检出",
              ("tools/new.py", True) in added and ("factory/loop.py", True) in removed,
              f"added={added} removed={removed}")

        # ---- guard：副本被运行改动时必须报警 ----
        (tmp / "factory/loop.py").write_text("# restored\n", encoding="utf-8")
        ws.cmd_snapshot(type("A", (), {"label": "t2", "force": True})())
        env = dict(os.environ, PYTHONPATH=f"{tmp}")
        # 注意：这里必须让子进程在**临时工作区**里跑，否则会在真实 ROOT 下
        # 留下 .workstreams/t2（测试污染真实工作区 —— 初版就犯了这个错）。
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools/workstream.py"), "--store", str(tmp / ".workstreams"),
             "guard", "--label", "t2", "--",
             sys.executable, "-c",
             "import pathlib; pathlib.Path('factory/loop.py').write_text('# run tampered\\n')"],
            cwd=str(tmp), env=dict(env, PYTHONPATH=str(ROOT)), capture_output=True, text=True, timeout=120)
        check("⑥ guard 检出「运行改动了冻结副本内的我方源码」",
              "本工作流的源码被运行本身改动" in proc.stdout,
              [ln for ln in proc.stdout.splitlines() if "污染" in ln or "未变" in ln][:1])
        check("⑥b 测试未污染真实工作区（真实 .workstreams/ 里不应出现 t2）",
              not (ROOT / ".workstreams" / "t2").exists())
    finally:
        ws.ROOT, ws.STORE = old_root, old_store
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 陈旧快照守卫（第 7 类自伤的防线）----
    #
    # 事故：修复分支后重跑 `guard --label contractfreeze`，它**静默复用**了
    # 上一次失败留下的快照（含跨 worktree 污染代码），于是修复完仍然 ImportError。
    # 静默复用是「验证基础设施失效但伪装成正常」的又一变体。
    print()
    print("--- 陈旧快照守卫 ---")
    tmp2 = Path(tempfile.mkdtemp(prefix="ws-stale-"))
    make_fake_ws(tmp2)
    old_root2, old_store2 = with_root(tmp2)
    try:
        ws.cmd_snapshot(type("A", (), {"label": "t3", "force": True})())
        man = ws.load_manifest("t3")
        check("⑦a 快照刚建时不算陈旧", ws._stale_owned_files("t3", man) == [])

        # 改动我方文件 -> 应判陈旧
        (tmp2 / "factory" / "loop.py").write_text("# changed after snapshot\n")
        stale = ws._stale_owned_files("t3", man)
        check("⑦b 我方文件改动后判为陈旧（正向：能拦住）",
              stale == ["factory/loop.py"], f"stale={stale}")

        # 只改**他方**文件 -> 不应判陈旧（反向：不误报）
        ws.cmd_snapshot(type("A", (), {"label": "t3", "force": True})())
        (tmp2 / "requirements_sample" / "requirements.yaml").write_text("changed: true\n")
        check("⑦c 仅他方文件改动**不**判陈旧（反向：不误报）",
              ws._stale_owned_files("t3", ws.load_manifest("t3")) == [])

        # guard 必须**拒绝**陈旧快照（不是警告）
        (tmp2 / "factory" / "loop.py").write_text("# changed again\n")
        rc = ws.cmd_guard(type("B", (), {
            "label": "t3", "refresh": False, "command": [sys.executable, "-c", "print(1)"],
        })())
        check("⑦d guard 对陈旧快照返回非零（拒绝而非警告）", rc != 0, f"rc={rc}")

        # 带 --refresh 应放行并重新冻结
        rc2 = ws.cmd_guard(type("B", (), {
            "label": "t3", "refresh": True, "command": [sys.executable, "-c", "print(1)"],
        })())
        check("⑦e --refresh 时放行并重新冻结", rc2 == 0, f"rc={rc2}")
        check("⑦f --refresh 后不再陈旧",
              ws._stale_owned_files("t3", ws.load_manifest("t3")) == [])
    finally:
        ws.ROOT, ws.STORE = old_root2, old_store2
        shutil.rmtree(tmp2, ignore_errors=True)

    print("=" * 78)
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print(f"❌ {len(failed)}/{len(RESULTS)} 项失败：")
        for item in failed:
            print(f"  - {item}")
        return 1
    print(f"✅ 全部 {len(RESULTS)} 项断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
