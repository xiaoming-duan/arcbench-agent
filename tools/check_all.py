"""统一校验入口：一次跑完三类断言，任何一类失败即整体失败。

=====================  为什么需要它  =====================
出现两次「验证代码本身没有被验证」：
  1. RunReport.to_dict 少输出 5 个字段（patch 锚点静默失败）
  2. measure_source 计数恒为 0（别名断言不识别）-> 弱化守卫长期失效

两者的共同模式：**关键度量 / 验证函数没有断言保护**，只能靠肉眼。
解法不是"下次更仔细"，而是把断言固化下来并做成可一键执行的关卡：

  verify_changes.py  声称的改动是否真的在代码里（24 项）
  test_gates.py      门禁行为是否符合契约（21 项）
  test_measures.py   度量函数是否真的在度量（18 项）

用法：python3 tools/check_all.py     全部通过退出码 0，否则 1
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SUITES = (
    ("声称变更核查", "tools/verify_changes.py", "24 项：每条声称的改动必须真的在代码里"),
    ("import 冒烟守卫", "tools/test_imports.py", "4 项：跨模块符号一致性 —— 跨 worktree 复制后必跑"),
    ("门禁行为断言", "tools/test_gates.py", "30 项：白名单 / 反馈闭环 / 弱化守卫 / 依赖门禁 T7-T11"),
    ("度量函数审计", "tools/test_measures.py", "18 项：measure_source / audit_imports / red_first_ok / CallStats"),
    ("依赖使用审计", "tools/test_dependency_audit.py", "24 项：静态调用 / 假依赖 / 间接声明 / mock / 注入旁路"),
    ("D10 修复验证", "tools/verify_d10.py", "6 项：mock 违规是否真的阻断（含旧判定复算）"),
    ("依赖边方向断言", "tools/test_call_edges.py", "11 项：call edge 方向(source=调用方) + node contract 落盘"),
    ("契约完整性", "tests/test_contract_integrity.py", "3 项：内嵌 integrity 字段 / 无侧车时仍能发现正文篡改 / 非 sha256 拒绝"),
    ("错误分类预算", "tests/test_retry_budget.py", "30 项：四类分类 / 预算语义 / 环境不挤占实现 / 配置对接"),
    ("契约冻结", "tests/test_contract_freeze.py", "12 项：生成/只读读取/CONTRACT_MISSING 门禁/拒绝理由可执行"),
    ("判据双向验证", "tests/test_assertion_meta.py", "4 项：元测试 —— 判据本身必须双向成立"),
    ("契约合成验证", "tests/test_contract_mismatch.py", "25 项：CONTRACT_MISMATCH 门禁合成验证（含元断言与已知局限）"),
    ("工作流隔离", "tools/test_workstream.py", "9 项：冻结快照 / 漂移检出(正/负向) / guard 污染报警"),
    ("架构核验工具", "tools/test_audit_architecture.py", "9 项：追溯表计数 / 文档精确匹配 / 孤儿分类 / 制品 kind"),
)


def main() -> int:
    print("=" * 78)
    print(f"统一校验：{len(SUITES)} 类断言")
    print("=" * 78)
    failures: list[str] = []
    counts: list[int] = []
    for label, script, note in SUITES:
        # pytest 套件必须经 pytest 运行 —— 它们没有 __main__ 入口，
        # 直接 `python <file>` 会零输出，导致「总数诚实」这条性质失真。
        #
        # 按**内容**判断而不是按路径：`tests/` 下既有 pytest 文件
        # （test_contract_freeze / test_assertion_meta），
        # 也有自带 __main__ 的独立脚本（test_contract_mismatch / test_dag）。
        # 初版只看路径前缀，于是把独立脚本也塞给 pytest -> "no tests ran"。
        _src = (ROOT / script).read_text(encoding="utf-8")
        is_pytest = "def test_" in _src and "__main__" not in _src
        cmd = ([sys.executable, "-m", "pytest", str(ROOT / script),
                "-q", "--no-header", "-p", "no:cacheprovider"]
               if is_pytest else [sys.executable, str(ROOT / script)])
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        tail = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
        # 摘要行取"✅/❌ 全部 ..."那一行（末尾可能是 JSON 输出）
        summary = next(
            (ln.strip() for ln in reversed(tail)
             if ln.strip().startswith(("✅ 全部", "❌", "✅ D10", "✅ 依赖", "✅ 全部"))),
            tail[-1] if tail else "(无输出)",
        )
        ok = proc.returncode == 0
        # 从摘要行里取数目参与合计，避免各套件增删断言后总数失真。
        # 两种口径：自研套件写「N 项」；pytest 写「N passed」。
        if is_pytest:
            m = re.search(r"(\d+)\s+passed", proc.stdout or "")
            if m:
                counts.append(int(m.group(1)))
                summary = f"✅ {m.group(1)} 项断言通过（pytest）"
        else:
            found = re.search(r"(\d+)\s*项", summary)
            if found:
                counts.append(int(found.group(1)))
        print(f"{'✅' if ok else '❌'}  {label:<12} {script:<28} {summary}")
        if not ok:
            failures.append(f"{label} ({script})")
            for line in tail[-12:]:
                print(f"      {line}")
    print("=" * 78)
    if failures:
        print(f"❌ {len(failures)}/{len(SUITES)} 类断言未通过：")
        for item in failures:
            print(f"  - {item}")
        return 1
    total = sum(counts)
    print(f"✅ 全部 {len(SUITES)} 类断言通过（合计 {total} 项）")
    print("   任何关键改动之后都应重跑本入口，而不是靠肉眼确认。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
