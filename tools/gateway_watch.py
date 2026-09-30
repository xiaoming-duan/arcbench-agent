"""网关健康探测 + 有上限的自动重跑。

=======================  为什么需要它  =======================
两次 guard 隔离运行的失败归因**都是网关**：
  run1  REQ-7 重写时  `[Errno 110] Connection timed out`
  run2  REQ-1 设计时  `RemoteDisconnected`（重试 2 次全耗尽，成功率 0%）

网关不稳定是环境侧限制，**无法消除**。但可以做到三件事：
  1. **不浪费 token 在注定失败的重跑上** —— 先探测，再决定跑不跑
  2. **一旦网关恢复，立即抓住窗口** —— 探测到 3/3 立刻开跑
  3. **有上限，不陷入无限重跑** —— 累计 closure6 最多 3 次

=======================  探测策略  =======================
  每 5 分钟探测一次，每次 3 个简单调用
    3/3 成功 -> 立即跑 closure6（guard 隔离）
    1-2/3    -> 等下一个周期
    0/3      -> 等下一个周期

  累计 closure6 运行上限 3 次；达到上限仍无全通过 -> 停止并记录。

  提前停止条件：closure6 全链通过（依赖累积终于有干净数据）。

=======================  输出  =======================
  logs/gateway-watch/watch-<时间戳>.log     逐步日志
  logs/gateway-watch/status.json            当前状态（可随时查看）
  logs/gateway-watch/run-<N>.log            第 N 次 closure6 的完整输出

用法：
  python3 tools/gateway_watch.py                 # 默认：5 分钟间隔 / 3 次探测 / 上限 3
  python3 tools/gateway_watch.py --interval 300 --probes 3 --max-runs 3
  python3 tools/gateway_watch.py --status        # 只看当前状态，不启动
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

LOG_DIR = ROOT / "logs" / "gateway-watch"
STATUS = LOG_DIR / "status.json"
REQUIREMENTS = "requirements_probe_closure6"
SNAPSHOT_LABEL = "v2-run"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Watch:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        # 用启动时间戳做命名空间：重启后不再覆盖上一轮的日志与输出目录。
        # 初版用 self.state["closure6_runs"]+1 做索引，重启后索引从 1 重来，
        # 会把上一轮的 run-1.log 与 out-watch1/ 覆盖掉 —— 实测踩到。
        self.session = datetime.now().strftime('%Y%m%d-%H%M%S')
        self.log_path = LOG_DIR / f"watch-{self.session}.log"
        self.state: dict = {
            "started_at": now(),
            "interval_s": args.interval,
            "probes_per_check": args.probes,
            "max_runs": args.max_runs,
            "cycles": 0,
            "closure6_runs": 0,
            "probe_history": [],
            "run_results": [],
            "outcome": "running",
            "note": "",
        }

    # ---------------------------------------------------------------- 日志
    def log(self, msg: str) -> None:
        line = f"[{now()}] {msg}"
        print(line, flush=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def save(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- 探测
    def probe(self) -> tuple[int, list[str]]:
        """做 N 次简单模型调用，返回 (成功数, 每次的简述)。"""
        from factory.llm import ModelClient  # 延迟导入，避免无凭据时启动即失败

        client = ModelClient(temperature=0.0, max_tokens=32, timeout_s=60)
        if not client.is_available():
            return 0, ["模型不可用：缺少 OPENAI_API_KEY / OPENAI_BASE_URL / MODEL"] * self.args.probes

        ok = 0
        detail: list[str] = []
        for i in range(self.args.probes):
            t0 = time.time()
            try:
                client.complete(system="Reply with JSON only.", user='Return {"ok":true}')
                ok += 1
                detail.append(f"#{i + 1} ✅ {time.time() - t0:.1f}s")
            except Exception as exc:  # noqa: BLE001
                detail.append(f"#{i + 1} ❌ {time.time() - t0:.1f}s {type(exc).__name__}: {str(exc)[:80]}")
        return ok, detail

    # ---------------------------------------------------------------- 运行
    def run_closure6(self, index: int) -> dict:
        """用 guard 在冻结副本里跑 closure6，返回记录。"""
        out_dir = f"out-watch{self.session}-{index}"
        snap = ROOT / ".workstreams" / SNAPSHOT_LABEL
        if not snap.is_dir():
            return {"error": f"冻结快照不存在: {snap}（先跑 workstream.py snapshot）"}

        env = dict(os.environ)
        env.setdefault("FACTORY_BYPASS_BLOCK", "1")
        env.setdefault("FACTORY_MAX_TOKENS", "3000")
        env.setdefault("FACTORY_MODEL_TIMEOUT", "300")
        env.setdefault("FACTORY_NPM_CACHE", str(ROOT / ".npm-cache"))
        env.setdefault("XDG_CACHE_HOME", str(ROOT / ".cache"))
        env.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")

        cmd = [
            sys.executable, str(ROOT / "tools" / "workstream.py"),
            "guard", "--label", SNAPSHOT_LABEL, "--",
            sys.executable, "main.py", REQUIREMENTS,
            "--output-dir", out_dir, "--type", "web",
            "--generator", "llm", "--install-deps", "auto", "--max-repairs", "3",
        ]
        t0 = time.time()
        self.log(f"▶️  第 {index} 次 closure6 开跑（guard 隔离，cwd={snap}）")
        run_log = LOG_DIR / f"run-{self.session}-{index}.log"
        with run_log.open("w", encoding="utf-8") as fh:
            proc = subprocess.run(cmd, cwd=str(ROOT), env=env,
                                  stdout=fh, stderr=subprocess.STDOUT, text=True)
        elapsed = time.time() - t0

        record: dict = {
            "index": index, "session": self.session,
            "started": now(), "elapsed_s": round(elapsed, 1),
            "exit_code": proc.returncode, "run_log": str(run_log),
        }
        # 读报告
        report = snap / out_dir / ".arc" / "factory-report.json"
        if report.is_file():
            d = json.loads(report.read_text(encoding="utf-8"))
            states = {r["req_id"]: r["state"] for r in d["results"]}
            record.update({
                "states": states,
                "passed": sum(1 for v in states.values() if v == "PASSED"),
                "failed": sum(1 for v in states.values() if v == "FAILED"),
                "upstream_failed": d.get("upstream_failed", 0),
                "gateway_retries": d.get("cost", {}).get("gateway_retries"),
                "retries_exhausted": d.get("cost", {}).get("retries_exhausted"),
                "total_tokens": d.get("cost", {}).get("total_tokens"),
            })
            # 全链通过 = 依赖累积终于有干净数据
            record["all_passed"] = bool(states) and all(v == "PASSED" for v in states.values())
        # 隔离是否被证明
        try:
            text = run_log.read_text(encoding="utf-8", errors="replace")
            record["isolation_verified"] = "冻结副本逐文件未变" in text
        except OSError:
            record["isolation_verified"] = None
        return record

    # ---------------------------------------------------------------- 主循环
    def run(self) -> int:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log("=" * 78)
        self.log(f"网关探测启动：每 {self.args.interval}s 探测 {self.args.probes} 次；"
                 f"closure6 上限 {self.args.max_runs} 次")
        self.log(f"日志: {self.log_path}")
        self.log("=" * 78)
        self.save()

        deadline = time.time() + self.args.max_hours * 3600
        while True:
            if time.time() > deadline:
                self.state["outcome"] = "max_hours_reached"
                self.state["note"] = f"达到最长运行时限 {self.args.max_hours} 小时"
                self.log(f"⏹  {self.state['note']}")
                break
            if self.state["closure6_runs"] >= self.args.max_runs:
                self.state["outcome"] = "max_runs_reached"
                self.state["note"] = (f"已达 closure6 运行上限 {self.args.max_runs} 次，"
                                      "仍未取得全链通过")
                self.log(f"⏹  {self.state['note']}")
                break

            self.state["cycles"] += 1
            ok, detail = self.probe()
            self.state["probe_history"].append({"at": now(), "ok": ok, "detail": detail})
            self.log(f"探测（第 {self.state['cycles']} 周期）: {ok}/{self.args.probes} 成功  "
                     + " | ".join(detail))
            self.save()

            if ok == self.args.probes:
                self.log("✅ 网关 3/3 —— 立即开跑 closure6")
                rec = self.run_closure6(self.state["closure6_runs"] + 1)
                self.state["closure6_runs"] += 1
                self.state["run_results"].append(rec)
                summary = (f"通过 {rec.get('passed')} / 失败 {rec.get('failed')} / "
                           f"跳过 {rec.get('upstream_failed')}")
            else:
                self.log(f"⏳ 网关未达 3/3，等下一个周期（{self.args.interval}s）")

            self.save()
            if self.state.get("run_results") and self.state["run_results"][-1].get("all_passed"):
                self.state["outcome"] = "all_passed"
                self.state["note"] = "closure6 全链通过 —— 依赖累积终于有干净数据"
                self.log("🎉 " + self.state["note"])
                break
            if self.state["closure6_runs"] < self.args.max_runs:
                time.sleep(self.args.interval)

        self.state["finished_at"] = now()
        if self.state["outcome"] == "running":
            self.state["outcome"] = "stopped"
        self.save()
        self.log(f"结束：{self.state['outcome']} —— {self.state.get('note', '')}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="网关健康探测 + 有上限的自动重跑")
    parser.add_argument("--interval", type=int, default=300, help="探测间隔秒数（默认 300 = 5 分钟）")
    parser.add_argument("--probes", type=int, default=3, help="每次探测的调用数（默认 3）")
    parser.add_argument("--max-runs", type=int, default=3, help="closure6 累计运行上限（默认 3）")
    parser.add_argument("--max-hours", type=float, default=8.0, help="最长运行时限（小时，默认 8）")
    parser.add_argument("--status", action="store_true", help="只打印当前状态后退出")
    args = parser.parse_args()

    if args.status:
        if STATUS.is_file():
            print(STATUS.read_text(encoding="utf-8"))
        else:
            print(f"（尚无状态文件：{STATUS}）")
        return 0
    return Watch(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
