#!/usr/bin/env python3
"""重新打包 v1 发布包（zip 格式）。

清单来源：上一版发布包 dist/arcbench-agent-v1.tar.gz 的文件列表（.v1manifest.txt），
内容来源：当前工作区（包含 tar 打包之后落地的 v1 修复）。

运行输出与缓存产物（out-v2/、dist/、src/、.npm-cache/、*.log、__pycache__）不在清单中，
因此不会被重新打进去。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / ".v1manifest.txt"
OUT = ROOT / "dist" / "arcbench-agent-v1.zip"


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

    size = OUT.stat().st_size
    print(f"✅ 已生成 {OUT.relative_to(ROOT)}")
    print(f"   文件数：{len(names)}    大小：{size:,} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
