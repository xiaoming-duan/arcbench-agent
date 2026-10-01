"""合同**完整性**校验的专项验证（pytest）。

运行：
    pytest tests/test_contract_integrity.py -v

═══════════════════════════════════════════════════════════════════════════
 两路校验，互补
═══════════════════════════════════════════════════════════════════════════
  ① **内嵌 `integrity: {sha256, algorithm}`** —— 可移植
     合同文件单独拿走也能自证**正文**未被改。
     不能覆盖自身：哈希写进文件后再算哈希值就变了（自指无解）。

  ② **侧车 `.arc/contracts/<req>.sha256`** —— 权威
     覆盖**含 integrity 块的完整文件字节**，连「只改了 integrity 字段」也能发现。

  ③ 权限位 `chmod 0444` —— **纵深防御，不是安全边界**
     同用户可 chmod 回来；真正的保证是 ①②。

三者关系：权限位让「随手改」不容易发生，哈希让「改了也白改」。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.adapter import _adapt  # noqa: E402
from factory.contracts import (  # noqa: E402
    contract_path,
    contracts_dir,
    hash_of_body,
    hash_record_path,
    write_contracts,
)

SPEC = {
    "requirements": [
        {"id": "REQ-1", "name": "条目", "depends_on": []},
        {"id": "REQ-7", "name": "更新", "depends_on": ["REQ-1"]},
        {"id": "REQ-11", "name": "流水", "depends_on": ["REQ-1", "REQ-7"],
         "cross_module_calls": [
             {"upstream": "REQ-7", "symbol": "updateQuantity",
              "signature": "updateQuantity(sku, quantity, from, to)",
              "semantics": "更新库存数量并写入一条流水记录",
              "side_effects": ["记录流水到 movements 数组"]},
             # ★ 必须**两条**：篡改用 `[:1]` 截断，若本来只有一条就是空操作，
             #   于是「篡改后哈希应当变化」的前提不成立 —— 断言会假绿。
             #   （第 7 次同类自伤：fixture 设计不满足断言的前提）
             {"upstream": "REQ-1", "symbol": "listItems",
              "signature": "listItems(filter)",
              "semantics": "查询库存项列表"},
         ]},
    ]
}


def tamper(path, mutate):
    """模拟真实篡改：合同是 0444，改它必须先 chmod（`tamper` 自带这一步）。"""
    os.chmod(path, 0o644)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


@pytest.fixture()
def reqs():
    return _adapt(SPEC, source=Path("x")).requirements


@pytest.fixture()
def frozen(tmp_path, reqs):
    write_contracts(tmp_path, reqs)
    return tmp_path


def test_integrity_field_recorded_and_consistent(frozen):
    """合同内嵌 `integrity: {sha256, algorithm}`，且与正文哈希一致。

    内嵌字段的价值是**可移植**：合同文件单独拿走也能自证正文未被改。
    权威完整性仍由侧车保证（它覆盖含 integrity 块的**完整文件字节**）。
    """
    from factory.contracts import hash_of_body
    payload = yaml.safe_load(contract_path(frozen, "REQ-11").read_text(encoding="utf-8"))
    integ = payload.get("integrity")
    assert isinstance(integ, dict), "缺少 integrity 字段"
    assert integ.get("algorithm") == "sha256"
    assert len(str(integ.get("sha256") or "")) == 64
    assert integ["sha256"] == payload["hash"], "integrity.sha256 与 hash 字段应一致"
    assert integ["sha256"] == hash_of_body(payload), "内嵌哈希应与正文重算结果一致"


def test_integrity_detects_body_tamper_even_without_sidecar(frozen, reqs):
    """★ 可移植性：**删掉侧车**后，仅凭内嵌 integrity 也能发现正文被改。"""
    from factory.contracts import check_contracts
    p = contract_path(frozen, "REQ-11")
    before = p.read_bytes()
    tamper(p, lambda d: d.__setitem__("cross_module_calls", d["cross_module_calls"][:1]))
    # ★ 前提校验：篡改必须真的改了字节，否则后续断言毫无意义
    assert p.read_bytes() != before, (
        "篡改是空操作（fixture 的截断没改到任何东西）—— 断言前提不成立，"
        "会得到一个假绿"
    )
    # 删侧车：把「完整文件字节」这条路径断掉，只剩内嵌校验
    rec = hash_record_path(frozen, "REQ-11")
    os.chmod(rec, 0o644)
    rec.unlink()

    ok, checks = check_contracts(frozen, reqs)
    assert not ok, "侧车不在时内嵌 integrity 必须兜住"
    bad = [c for c in checks if not c.ok][0]
    assert bad.reason == "CONTRACT_TAMPERED"
    assert "正文" in bad.detail, f"应指出是正文被改: {bad.detail}"


def test_integrity_rejects_unknown_algorithm(frozen, reqs):
    """integrity.algorithm 不是 sha256 -> 阻断（无法校验就不能放行）。"""
    from factory.contracts import check_contracts
    p = contract_path(frozen, "REQ-11")
    tamper(p, lambda d: d["integrity"].__setitem__("algorithm", "md5"))
    ok, checks = check_contracts(frozen, reqs)
    assert not ok
    assert any("algorithm" in c.detail for c in checks if not c.ok)

