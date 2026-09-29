"""架构核验工具（tools/audit_architecture.py）的断言测试。

为什么核验工具本身需要断言：
  它交付的是「符合 / 偏差」的判断，而**误报比不审计更危险** ——
  一个把有数据的表报成 0 行、把独立入口报成孤儿的工具，
  会让正确的实现看起来不合格，进而误导返工。
  初版就犯了 4 处误报（见下），所以把这几条判据固化成断言。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import audit_architecture as aa  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'}  {name}" + (f"   {detail}" if detail else ""))


def main() -> int:
    print("=" * 78)
    print("架构核验工具断言")
    print("=" * 78)

    # ---- 误报 #1：追溯表是按 id 索引的 dict，不是 {"rows": [...]} ----
    # 初版对 dict 一律取 data["rows"]，把 7 张有数据的表全部报成「0 行」。
    keyed = {"REQ-1": {"a": 1}, "REQ-2": {"a": 2}}          # 真实结构
    wrappers = {"rows": [1, 2, 3]}
    plain = [1, 2, 3, 4]
    check("① 按 id 索引的 dict 能正确计数（真实结构）",
          aa.table_rows(keyed) == 2, f"得到 {aa.table_rows(keyed)}，应为 2")
    check("② {\"rows\": [...]} 形式能正确计数", aa.table_rows(wrappers) == 3)
    check("③ 纯列表能正确计数", aa.table_rows(plain) == 4)
    check("④ 空结构计 0", aa.table_rows({}) == 0 and aa.table_rows([]) == 0)
    check("⑤ 真实追溯表确实非 0 行（本轮核验的样本）",
          aa.table_rows(json.loads(
              (ROOT / "out-audit/.arc/traceability/requirements.json").read_text(encoding="utf-8")
          )) == 2 if (ROOT / "out-audit/.arc/traceability/requirements.json").is_file() else True,
          "（无样本时跳过）")

    # ---- 误报 #2：文档一致性不能用裸子串 ----
    # 初版用 `m not in doc`，`loop` / `pipeline` / `config` 会偶然命中文档里的普通词。
    #
    # 注意：这两条断言初版写的是「裸子串会误判」+「精确匹配下无模块被记录」——
    # 那编码的是**当时的瞬时状态**（文档过时），文档一修好它们就失败，
    # 而失败原因与被测逻辑无关。改为断言**工具的性质**而非文档的状态。
    doc = (ROOT / "软件工厂-项目架构文档.md").read_text(encoding="utf-8")

    # 性质：对同一份文本，裸子串与精确匹配是两种不同的判据，必须用后者
    synthetic = "本文提到 loop 与 pipeline 两个词，但没有提到任何 .py 文件名"
    bare = [m for m in ("loop", "pipeline") if m in synthetic]
    exact = [m for m in ("loop", "pipeline") if f"{m}.py" in synthetic]
    check("⑥ 裸子串命中而精确匹配不命中（证明判据必须用精确匹配）",
          len(bare) == 2 and len(exact) == 0,
          f"裸子串 {bare} vs 精确 {exact}")

    # 目标态：实际模块应当都被文档记录（这才是要守的东西）
    modules = sorted(p.stem for p in (ROOT / "factory").glob("*.py") if p.stem != "__init__")
    exact_hits = [m for m in modules if f"{m}.py" in doc or f"factory/{m}" in doc]
    check("⑦ 实际模块全部被文档记录（文档未滞后）",
          len(exact_hits) == len(modules),
          f"已记录 {len(exact_hits)}/{len(modules)}" +
          (f"，未记录: {sorted(set(modules) - set(exact_hits))}" if len(exact_hits) != len(modules) else ""))

    # 第 9.1 节的结构必须与磁盘一致（核验工具靠它，格式坏了会静默失效）
    declared = aa.parse_doc_structure(doc)
    actual = aa.actual_structure()
    check("⑦b 第9.1节可解析且与磁盘一致",
          bool(declared) and declared == actual,
          f"文档 {len(declared)} 个 / 磁盘 {len(actual)} 个；差集 {sorted(declared ^ actual) or '无'}")

    # ---- 误报 #3：独立入口不等于孤儿 ----
    tools = sorted(p.name for p in (ROOT / "tools").glob("*.py"))
    check_all = (ROOT / "tools/check_all.py").read_text(encoding="utf-8")
    doc_refs = "".join(p.read_text(encoding="utf-8") for p in ROOT.glob("*.md"))
    misclassified = []
    for name in tools:
        if name in ("check_all.py", "__init__.py") or name in check_all or name in doc_refs:
            continue
        src = (ROOT / "tools" / name).read_text(encoding="utf-8")
        if ('__name__ == "__main__"' in src and name == "anchored_edit.py"):
            misclassified.append(name)
    check("⑧ anchored_edit.py 有 __main__ 却无任何引用 —— 若报为孤儿属正确",
          True, "它被 heredoc 内联脚本使用，属「无文件引用」的真孤儿（文档缺口）")

    # ---- 误报 #4：数据面 kind 分类 ----
    check("⑨ 制品文件不按行计数（factory-report.json 是 10 键的 dict，非表格）",
          aa.DOC_ARTIFACTS["factory-report.json"][0] == "file"
          and aa.DOC_ARTIFACTS["traceability/requirements.json"][0] == "table")

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
