#!/usr/bin/env python3
"""打包 v2 提交包（按目录打包 + 排除清单，Python 实现）。

为什么不用 `zip -r ... -x`：本环境没有 zip CLI，用 zipfile 实现同样的语义。
为什么改成「按目录打包」而不是之前的清单式：清单式会**漏文件**——已经发生过两次，
`tools/check_all.py` 引用了新增的测试脚本，而脚本不在清单里，包自带的校验入口
一解压就报缺文件。按目录打包 + 排除清单可以彻底避免这一类漏配。

排除项（与提交包契约一致）：
  backups/  .workstreams/  dist/  out*/  .arc/  .verify-*  .probe-*  .zipcheck*
  *.log  __pycache__/  *.pyc  .npm-cache/  node_modules/  .env  .git/
关键：**排除 .git/** ——版本控制是本地基础设施，不进提交包。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist" / "arcbench-agent-v2.zip"

EXCLUDED_DIRS = {"backups", ".workstreams", "dist", ".arc", ".npm-cache",
                 "__pycache__", "node_modules", ".git", ".cache",
                 # 运行期日志目录（与 .arc/ out*/ *.log 同类：运行产物，可重建）
                 "logs",
                 # 平台 API 文档的抓取缓存（2.8MB 派生物，可重新抓取）
                 ".apidoc",
                 # git worktree（另一条工作流为分支隔离建的检出副本）——
                 # 里面是**同一份源码的第二份拷贝**，进包会让文件数翻倍
                 ".wt",
                 # pytest 运行缓存（跑一次测试就会生成）
                 ".pytest_cache"}

# 仅**顶层**的游离物。必须限定在顶层：同名文件在子目录里是正式文件 ——
#   factory/loop.py、tools/test_gates.py、arcbench-agent-runtime/src 都是本体。
# 这几项是实测检出的历史遗留（v7 打包时发现）：
EXCLUDED_ROOT_NAMES = {
    "loop.py",                      # factory/loop.py 的陈旧副本（hash 不同、时间更早）
    "test_gates.py",                # tools/test_gates.py 的陈旧副本
    "src",                          # 游离的 src/services/inventoryService.js
    "agent-blank-based (1).zip",    # 嵌套的 baseline agent 包（119KB，53 条目）
}
EXCLUDED_PREFIXES = (".verify-", ".probe-", ".zipcheck", ".zipv", ".bkcheck")
EXCLUDED_SUFFIXES = (".log", ".pyc", ".pyo")


def is_excluded(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in EXCLUDED_DIRS for p in parts):
        return True
    if any(p.startswith(EXCLUDED_PREFIXES) for p in parts):
        return True
    # out-v2 / out-probe 这类运行输出目录（仅限顶层）
    if parts[0].startswith("out") and len(parts) > 1:
        return True
    # 仅顶层的游离物（见 EXCLUDED_ROOT_NAMES 的说明）
    if parts[0] in EXCLUDED_ROOT_NAMES:
        return True
    if rel.endswith(EXCLUDED_SUFFIXES):
        return True
    if parts[-1] == ".env":
        return True
    return False


def main() -> int:
    files: list[Path] = []
    skipped = 0
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        if rel == "dist/arcbench-agent-v2.zip":      # 不要把包自己装进去
            skipped += 1
            continue
        if is_excluded(rel):
            skipped += 1
            continue
        files.append(p)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            rel = p.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo.from_file(p, arcname=rel)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (p.stat().st_mode & 0xFFFF) << 16
            zf.writestr(info, p.read_bytes())

    names = zf_name_list = None
    with zipfile.ZipFile(OUT) as zf:
        names = zf.namelist()
    print(f"✅ 已生成 {OUT.relative_to(ROOT)}")
    print(f"   纳入 {len(names)} 个文件 / 排除 {skipped} 个    体积 {OUT.stat().st_size:,} 字节")
    print(f"   main.py 在根         : {'main.py' in names}")
    print(f"   requirements.txt 在根: {'requirements.txt' in names}")
    print(f"   含 .git/ 条目         : {any(n.startswith('.git/') for n in names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
