"""治理套件（dsh-governance-kit）的验证入口。

把 kit 的 Node 自测接入 check_all —— 否则「工具能注册」与「工具真的能判定」
会被混为一谈（前者是安装问题，后者是正确性问题）。

运行：python3 tools/test_governance_kit.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KIT = ROOT / "dsh-governance-kit"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'}  {name}" + (f"   {detail}" if detail else ""))


def main() -> int:
    print("=" * 78)
    print("治理套件（DSH Tool Plugin）自测")
    print("=" * 78)

    if not KIT.is_dir():
        check("① kit 目录存在", False, str(KIT))
    else:
        check("① kit 目录存在", True, str(KIT.relative_to(ROOT)))

    node = shutil.which("node")
    check("② node 可用（kit 是 JS 实现，无 Python 版）", node is not None, node or "未找到 node")
    if not node:
        return _summary()

    # ③ 交付形态：manifest 声明 dsh.bundle.patch，入口可导出 apply
    try:
        pkg = json.loads((KIT / "package.json").read_text(encoding="utf-8"))
        patch = (pkg.get("dsh") or {}).get("bundle", {}).get("patch")
        check("③a package.json 声明 dsh.bundle.patch", bool(patch), str(patch))
        check("③b 入口文件存在", (KIT / "index.js").is_file())
        check("③c cordis.patch.yml 存在且声明了插件行",
              (KIT / "cordis.patch.yml").is_file()
              and "dsh-governance-kit" in (KIT / "cordis.patch.yml").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        check("③ manifest 可解析", False, f"{type(exc).__name__}: {exc}")

    # ④ index.js 导出 DSH 要求的 apply（实查 API：apply + 可选 inject）
    proc = subprocess.run(
        [node, "-e",
         "import('./index.js').then(m=>{"
         "if(typeof m.apply!=='function') { console.error('no apply'); process.exit(1);} "
         "console.log(JSON.stringify({inject:m.inject, verdicts:m.VERDICTS}));})"],
        cwd=str(KIT), capture_output=True, text=True, timeout=60,
    )
    check("④ index.js 导出 apply（DSH 插件入口）", proc.returncode == 0,
          (proc.stdout or proc.stderr).strip()[:120])
    if proc.returncode == 0:
        try:
            meta = json.loads(proc.stdout.strip())
            check("④b inject 声明了 tools 服务", meta.get("inject") == ["tools"],
                  str(meta.get("inject")))
            check("④c 三态判定名已导出（供上层断言，防拼写漂移）",
                  set(meta.get("verdicts") or []) >=
                  {"CONTRACT_MISSING", "CONTRACT_TAMPERED", "CONTRACT_MISMATCH"},
                  str(meta.get("verdicts")))
        except Exception:  # noqa: BLE001
            pass

    # ⑤ ★ 端到端功能自测（真正的正确性验证）
    proc = subprocess.run([node, "kit-test.mjs"], cwd=str(KIT),
                          capture_output=True, text=True, timeout=120)
    tail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    check("⑤ kit 端到端自测（冻结/完好/漂移/不符/篡改/不存在/空声明/重新冻结）",
          proc.returncode == 0, tail[-1] if tail else (proc.stderr or "")[-160:])
    if proc.returncode != 0:
        for ln in tail[-14:]:
            print(f"      {ln}")

    # ⑥ 反向：故意破坏一个判定，自测必须报红（防止自测恒绿）
    #    —— 与「判据双向验证」同源：验证器自身也要被验证。
    broken = KIT / "lib" / "_contract_backup.js"
    lib = KIT / "lib" / "contract.js"
    try:
        original = lib.read_text(encoding="utf-8")
        lib.write_text(original.replace("export function sha256(text) {",
                                         "export function sha256(text) { return 'x'.repeat(64);"),
                      encoding="utf-8")
        # ★ 前提断言：破坏必须真的改了文件
        assert lib.read_text(encoding="utf-8") != original, "破坏未生效"
        proc = subprocess.run([node, "kit-test.mjs"], cwd=str(KIT),
                              capture_output=True, text=True, timeout=120)
        check("⑥ 反向验证：sha256 被破坏后自测必须失败（防恒绿）",
              proc.returncode != 0, "自测正确报红")
    finally:
        if 'original' in dir():
            lib.write_text(original, encoding="utf-8")
        if broken.exists():
            broken.unlink()

    return _summary()


def _summary() -> int:
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
    sys.exit(main())
