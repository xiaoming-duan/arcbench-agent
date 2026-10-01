#!/usr/bin/env python3
"""配额恢复监控：低频探测，恢复后自动补基线样本。

═══════════════════════════════════════════════════════════════════════════
 为什么需要它
═══════════════════════════════════════════════════════════════════════════
closure6 第 4 次补跑因**模型配额耗尽**中止（HTTP 429 `insufficient_quota`）。
配额是外部约束 —— 不知道何时恢复，而基线样本必须**先于**任何配置实验。

这个脚本把「等待」变成无人值守：低频探测，一旦恢复立即补样本。

═══════════════════════════════════════════════════════════════════════════
 设计约束
═══════════════════════════════════════════════════════════════════════════
1. **低频**：每周期只探测 **1 次**。
   每次探测本身消耗配额，高频探测会**延缓恢复** —— 这是与
   `gateway_watch.py`（探测 3 次/周期）的关键差异。
2. **区分 429 的两种含义**（沿用 `llm._is_quota_exhausted` 的口径）：
     429 rate_limit_exceeded -> 瞬时，下个周期重试有意义
     429 insufficient_quota  -> 终局，本周期无需再试
   两者都表现为「本周期失败」，但记录不同。
3. **有上限**：默认 6 小时。超时仍失败 -> 记录「配额未恢复」并停止。
4. **只做基线样本**：配置**不变**。不在这里做配置实验 ——
   否则无法区分「通过率提升是配置生效还是模型这次发挥好」。

═══════════════════════════════════════════════════════════════════════════
 用法
═══════════════════════════════════════════════════════════════════════════
    # 只探测，不跑样本（先确认脚本工作正常）
    python3 tools/quota_watch.py --dry-run --once

    # 正式：每 10 分钟探 1 次，恢复后跑基线样本
    python3 tools/quota_watch.py --interval 600 --max-hours 6

    # 必须用后台任务启动（nohup 在沙箱里活不下来）：
    #   run_in_background: true
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

LOG_DIR = ROOT / "logs" / "quota-watch"
STATUS = LOG_DIR / "status.json"

REQUIREMENTS = "requirements_probe_closure6"
# 基线的快照标签 —— 与 variance 系列区分，便于事后归因
SNAPSHOT_LABEL = "baseline-quota-recovery"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def probe_once() -> tuple[bool, str]:
    """探测一次。返回 (是否可用, 原因)。

    **只调用 1 次** —— 探测本身消耗配额。
    """
    try:
        from factory.llm import ModelClient
    except Exception as exc:  # noqa: BLE001
        return False, f"导入失败: {type(exc).__name__}: {exc}"

    try:
        ModelClient(temperature=0.0, max_tokens=16, timeout_s=60).complete(
            system="Reply JSON only.", user='Return {"ok":true}'
        )
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        text = str(exc)
        # 与 llm 侧同一口径：区分「配额耗尽（终局）」与「限流（瞬时）」
        try:
            from factory.llm import _is_quota_exhausted  # type: ignore[attr-defined]
            if _is_quota_exhausted(text):
                return False, f"QUOTA_EXHAUSTED: {name}"
        except Exception:  # noqa: BLE001
            pass
        if "429" in text or "insufficient_quota" in text:
            return False, f"QUOTA_EXHAUSTED(heuristic): {name}"
        return False, f"OTHER: {name}: {text[:120]}"


def write_status(payload: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_baseline_sample(run_index: int) -> dict:
    """配额恢复后跑一次**基线样本**（配置不变）。"""
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", f"{ROOT / 'arcbench-agent-runtime' / 'src'}:{ROOT}")

    # 1) 冻结快照（含隔离证明）
    snap = subprocess.run(
        [sys.executable, "tools/workstream.py", "snapshot",
         "--label", SNAPSHOT_LABEL, "--force"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )
    log = LOG_DIR / f"baseline-{run_index}.log"
    cmd = [
        sys.executable, "tools/workstream.py", "guard", "--label", SNAPSHOT_LABEL,
        "--", sys.executable, "main.py", REQUIREMENTS,
        "--output-dir", f"out-baseline{run_index}",
        "--type", "web", "--generator", "llm",
        "--install-deps", "auto", "--max-repairs", "3",
    ]
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, stdout=fh,
                              stderr=subprocess.STDOUT, text=True)
    tail = ""
    try:
        tail = "\n".join(log.read_text(encoding="utf-8").splitlines()[-3:])
    except Exception:  # noqa: BLE001
        pass
    return {
        "snapshot_ok": snap.returncode == 0,
        "snapshot_tail": (snap.stdout or "").strip().splitlines()[-1:] or [""],
        "exit_code": proc.returncode,
        "log": str(log.relative_to(ROOT)),
        "tail": tail,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="配额恢复监控 + 自动补基线样本")
    parser.add_argument("--interval", type=int, default=600,
                        help="探测间隔（秒，默认 600 = 10 分钟）")
    parser.add_argument("--max-hours", type=float, default=6.0,
                        help="最长监控时长（小时，默认 6）")
    parser.add_argument("--baselines", type=int, default=1,
                        help="配额恢复后补几个基线样本（默认 1）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只探测，不跑基线样本")
    parser.add_argument("--once", action="store_true",
                        help="只探测一次就退出（用于自检）")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    deadline = datetime.now() + timedelta(hours=args.max_hours)
    cycles, runs_done = 0, 0
    history: list[dict] = []
    outcome = "WATCHING"

    print("=" * 78)
    print("配额恢复监控")
    print("=" * 78)
    print(f"  间隔      : {args.interval}s（每周期 **1 次**探测 —— 探测本身消耗配额）")
    print(f"  上限      : {args.max_hours} 小时（至 {deadline:%H:%M:%S}）")
    print(f"  恢复后动作: 补 {args.baselines} 个**基线样本**（配置不变）")
    print(f"  样本标签  : {SNAPSHOT_LABEL}")
    print()

    while True:
        cycles += 1
        ok, reason = probe_once()
        entry = {"cycle": cycles, "at": now(), "ok": ok, "reason": reason}
        history.append(entry)
        print(f"  [{now()}] 第 {cycles} 次探测: {'✅ 可用' if ok else '❌ ' + reason}")

        if ok:
            outcome = "QUOTA_RECOVERED"
            if args.dry_run or args.once:
                print("  （dry-run/once：不跑样本）")
                break
            for i in range(1, args.baselines + 1):
                runs_done += 1
                print(f"  ▶️  补第 {runs_done} 个基线样本 ...")
                res = run_baseline_sample(runs_done)
                entry.setdefault("baselines", []).append(res)
                print(f"     退出码={res['exit_code']}  日志={res['log']}")
                for line in res["tail"].splitlines():
                    print(f"     {line}")
                write_status({"outcome": outcome, "cycles": cycles,
                              "history": history, "updated": now()})
            break

        write_status({"outcome": "WATCHING", "cycles": cycles,
                      "history": history, "updated": now()})

        if args.once:
            outcome = "STILL_EXHAUSTED"
            break
        if datetime.now() >= deadline:
            outcome = "QUOTA_NOT_RECOVERED"
            print(f"  ⏱  已达 {args.max_hours} 小时上限，仍不可用 -> 停止监控")
            break
        time.sleep(args.interval)

    write_status({"outcome": outcome, "cycles": cycles, "baselines_done": runs_done,
                  "history": history, "updated": now()})
    print()
    print("=" * 78)
    print(f"结果: {outcome}（探测 {cycles} 次，补样本 {runs_done} 个）")
    print(f"状态文件: {STATUS.relative_to(ROOT)}")
    print("=" * 78)
    return 0 if outcome in {"QUOTA_RECOVERED", "STILL_EXHAUSTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
