"""契约冻结（Contract Freeze）：把跨模块调用契约从「提示词建议」升级为**验收合同**。

═══════════════════════════════════════════════════════════════════════════
 为什么要冻结
═══════════════════════════════════════════════════════════════════════════
`cross_module_calls` 此前只是注入提示词的一段文字 —— **模型可以忽略**。

实测证据（closure6）：
  REQ-11 拿到 **3 次** `[依赖门禁·归因]` 提示 + 1 次阻断理由，
  仍然没有真实 import 上游 REQ-7，4 轮耗尽后失败。
  → 提示词的约束力**不够**。

借鉴 Consort / Agentic Foundry 的「冻结验收合同」：
  合同在编译前从需求 YAML 生成，落到 `.arc/contracts/<req_id>.yaml`；
  实现阶段**只读**，模型改不动；门禁校验合同存在性与一致性。

三方一致模型：
    需求 YAML（人写的声明）
        │  编译前生成（本模块 generate）
        ▼
    .arc/contracts/<req_id>.yaml   ← frozen: true，实现阶段只读
        │  生成时读取（本模块 load，供 prompt 用）
        ▼
    模型产出的实现 / 测试
        │  门禁校验（本模块 verify）
        ▼
    一致 -> 放行 ; 缺失或不一致 -> CONTRACT_MISSING 阻断

═══════════════════════════════════════════════════════════════════════════
 与 CONTRACT_MISMATCH 的分工
═══════════════════════════════════════════════════════════════════════════
  CONTRACT_MISSING   —— **合同本身**缺失/未冻结/与 YAML 漂移（编译期问题）
  CONTRACT_MISMATCH  —— 合同在，但**实现/调用**不符合它（运行期问题）

两者互补：前者保证「有约定的合同」，后者保证「按合同做」。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from .models import CrossModuleCall, Requirement

logger = logging.getLogger("factory.contracts")

CONTRACTS_SUBDIR = Path(".arc") / "contracts"
#: 合同文件里标记冻结的字段。缺失或不为真 -> 视为未冻结。
FROZEN_FIELD = "frozen"


def contracts_dir(output_dir: Path) -> Path:
    return Path(output_dir) / CONTRACTS_SUBDIR


def contract_path(output_dir: Path, req_id: str) -> Path:
    return contracts_dir(output_dir) / f"{req_id}.yaml"


# ---------------------------------------------------------------------------
# 生成（编译前）
# ---------------------------------------------------------------------------


def contract_payload(requirement: Requirement, *, generated_at: str | None = None) -> dict[str, Any]:
    """产出一个需求的合同内容。

    只包含 `cross_module_calls` 非空的需求 —— 没有跨模块约定的需求不需要合同，
    门禁对它们也不做校验（**可选能力的默认关闭语义**）。
    """
    return {
        "req_id": requirement.req_id,
        FROZEN_FIELD: True,
        "generated_at": generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cross_module_calls": [c.to_dict() for c in requirement.cross_module_calls],
    }


def write_contracts(output_dir: Path, requirements: Iterable[Requirement]) -> list[Path]:
    """为所有声明了 `cross_module_calls` 的需求写冻结合同。返回写出的路径列表。

    幂等：目录已存在则复用；同名合同**覆盖重写**（每次编译重新冻结，
    保证合同与当次需求 YAML 一致 —— 否则会拿上一轮的旧合同跑）。
    """
    target = contracts_dir(output_dir)
    written: list[Path] = []
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for req in requirements:
        if not req.cross_module_calls:
            continue
        target.mkdir(parents=True, exist_ok=True)
        path = contract_path(output_dir, req.req_id)
        path.write_text(
            yaml.safe_dump(contract_payload(req, generated_at=stamp),
                           allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        written.append(path)
    if written:
        logger.info("[合同冻结] 已生成 %d 份冻结合同 -> %s", len(written), target)
    return written


# ---------------------------------------------------------------------------
# 读取（供提示词使用 —— 只读冻结件，不读需求 YAML）
# ---------------------------------------------------------------------------


def _calls_from_payload(payload: dict[str, Any]) -> tuple[CrossModuleCall, ...]:
    out: list[CrossModuleCall] = []
    for item in payload.get("cross_module_calls") or []:
        if not isinstance(item, dict):
            continue
        upstream = str(item.get("upstream") or "").strip()
        symbol = str(item.get("symbol") or "").strip()
        if not upstream or not symbol:
            continue
        effects = item.get("side_effects") or ()
        if isinstance(effects, str):
            effects = [effects]
        out.append(CrossModuleCall(
            upstream=upstream,
            symbol=symbol,
            signature=str(item.get("signature") or "").strip(),
            semantics=str(item.get("semantics") or "").strip(),
            side_effects=tuple(str(x) for x in effects if str(x).strip()),
        ))
    return tuple(out)


def load_frozen_calls(output_dir: Path, req_id: str) -> tuple[CrossModuleCall, ...] | None:
    """读冻结合同里的 `cross_module_calls`。

    返回 None 表示**合同不存在或不可用** —— 调用方（提示词层）应据此
    回退到内存声明并记警告，而不是静默用错的内容。
    """
    path = contract_path(output_dir, req_id)
    if not path.is_file():
        return None
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[合同冻结] %s 读取失败: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        return None
    return _calls_from_payload(payload)


# ---------------------------------------------------------------------------
# 校验（门禁）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractCheck:
    """单个需求的合同完整性结论。"""

    req_id: str
    ok: bool
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"req_id": self.req_id, "ok": self.ok,
                "reason": self.reason, "detail": self.detail}


def check_contract(
    output_dir: Path,
    requirement: Requirement,
) -> ContractCheck:
    """校验单个需求的冻结合同。

    只有在需求**声明了** `cross_module_calls` 时才需要合同 ——
    未声明时返回 ok（可选能力的默认关闭语义）。

    三项检查（任一不过即 not ok）：
      ① 合同文件存在
      ② `frozen` 字段为真
      ③ 合同内容与需求 YAML 的声明一致（逐条比对 upstream/symbol/signature）
    """
    declared = requirement.cross_module_calls
    if not declared:
        return ContractCheck(req_id=requirement.req_id, ok=True, reason="NO_CONTRACT_DECLARED")

    path = contract_path(output_dir, requirement.req_id)
    if not path.is_file():
        return ContractCheck(
            req_id=requirement.req_id, ok=False, reason="CONTRACT_MISSING",
            detail=f"合同文件不存在: {path.relative_to(output_dir) if output_dir in path.parents else path}",
        )
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        return ContractCheck(req_id=requirement.req_id, ok=False,
                             reason="CONTRACT_MISSING", detail=f"合同无法解析: {exc}")
    if not isinstance(payload, dict):
        return ContractCheck(req_id=requirement.req_id, ok=False,
                             reason="CONTRACT_MISSING", detail="合同内容不是映射")

    if payload.get(FROZEN_FIELD) is not True:
        return ContractCheck(
            req_id=requirement.req_id, ok=False, reason="CONTRACT_MISSING",
            detail=f"合同未冻结（{FROZEN_FIELD} != true）—— 实现阶段不得使用未冻结的合同",
        )

    frozen = _calls_from_payload(payload)
    # 逐条比对：把声明与冻结件都归一成可比较的元组再比集合
    def key(c: CrossModuleCall) -> tuple[str, str, str]:
        return (c.upstream, c.symbol, c.signature)

    want = {key(c) for c in declared}
    got = {key(c) for c in frozen}
    if want != got:
        missing = want - got
        extra = got - want
        parts = []
        if missing:
            parts.append("冻结件缺少: " + ", ".join(f"{u}/{s}({g})" for u, s, g in sorted(missing)))
        if extra:
            parts.append("冻结件多出: " + ", ".join(f"{u}/{s}({g})" for u, s, g in sorted(extra)))
        return ContractCheck(
            req_id=requirement.req_id, ok=False, reason="CONTRACT_MISSING",
            detail="合同与需求声明不一致（漂移）—— " + "；".join(parts),
        )

    return ContractCheck(req_id=requirement.req_id, ok=True, reason="CONTRACT_FROZEN_OK",
                         detail=f"{len(frozen)} 条调用契约已冻结且一致")


def check_contracts(
    output_dir: Path,
    requirements: Sequence[Requirement],
) -> tuple[bool, list[ContractCheck]]:
    """批量校验。返回 (是否全过, 逐条结论)。"""
    checks = [check_contract(output_dir, r) for r in requirements]
    return all(c.ok for c in checks), checks


def describe(checks: Sequence[ContractCheck]) -> str:
    """把未通过的合同检查渲染成**可执行**的拒绝理由。"""
    bad = [c for c in checks if not c.ok]
    if not bad:
        return ""
    lines = ["需求**声明了**跨模块调用契约，但冻结合同不完整:"]
    for c in bad:
        lines.append(f"  - [{c.reason}] {c.req_id}: {c.detail}")
    lines.append("  合同是**验收合同**：它在编译前由需求 YAML 生成，实现阶段只读、不可修改。")
    lines.append("  **修正指令**：不要改合同；应使代码符合合同里的签名与语义。")
    lines.append("  若确属需求变更，请改需求 YAML 的 cross_module_calls 后重新编译（会重新冻结）。")
    return "\n".join(lines)
