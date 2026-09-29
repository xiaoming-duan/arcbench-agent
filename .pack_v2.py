#!/usr/bin/env python3
"""重新打包 v2 提交包（zip 格式）。

清单来源：v1 发布包的 129 文件清单（.v1manifest.txt）——它保证
  * main.py 与 requirements.txt 位于 zip 根目录（平台入口契约）
  * 不含 out-*/、dist/、.npm-cache/、__pycache__/、*.log、scratch 目录
内容来源：当前工作区（含本轮平台契约修复）。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / ".v1manifest.txt"
OUT = ROOT / "dist" / "arcbench-agent-v2.zip"


def main() -> int:
    names = [ln.strip() for ln in MANIFEST.read_text(encoding="utf-8").splitlines() if ln.strip()]

    missing = [n for n in names if not (ROOT / n).is_file()]
    if missing:
        print("清单中的文件在工作区缺失：", file=sys.stderr)
        for n in missing:
            print(f"  - {n}", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in names:
            src = ROOT / name
            info = zipfile.ZipInfo.from_file(src, arcname=name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (src.stat().st_mode & 0xFFFF) << 16
            zf.writestr(info, src.read_bytes())

    print(f"✅ 已生成 {OUT.relative_to(ROOT)}")
    print(f"   文件数：{len(names)}    大小：{OUT.stat().st_size:,} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
