"""契约冻结的验证用例（pytest）。

运行：
    pytest tests/test_contract_freeze.py -v

═══════════════════════════════════════════════════════════════════════════
 验证什么
═══════════════════════════════════════════════════════════════════════════
三方一致模型：
    需求 YAML  --生成-->  .arc/contracts/<req_id>.yaml（frozen）  --门禁-->  实现

本文件按**判据双向验证**的纪律编写：每条判据都同时给出
  ① 正确样本（证明不误报）  ② 错误样本（证明能拦住）
见架构文档 19.2.7。
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

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
    describe,
    load_frozen_calls,
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
             {"upstream": "REQ-1", "symbol": "listItems",
              "signature": "listItems(filter)",
              "semantics": "查询库存项列表"},
         ]},
    ]
}


@pytest.fixture()
def reqs():
    return _adapt(SPEC, source=Path("x")).requirements


@pytest.fixture()
def frozen(tmp_path, reqs):
    write_contracts(tmp_path, reqs)
    return tmp_path


# ═══════════════════════════════════════════════════════════════════════════
# 生成
# ═══════════════════════════════════════════════════════════════════════════


def test_only_declaring_requirements_get_contracts(tmp_path, reqs):
    """只给**声明了** cross_module_calls 的需求生成合同（可选能力的默认关闭语义）。"""
    written = write_contracts(tmp_path, reqs)
    names = sorted(p.stem for p in written)
    assert names == ["REQ-11"], f"应只为 REQ-11 生成合同，实际 {names}"
    assert not contract_path(tmp_path, "REQ-1").exists()
    assert not contract_path(tmp_path, "REQ-7").exists()


def test_contract_is_frozen_and_carries_declaration(frozen):
    """合同必须带 frozen: true，且内容与声明一致。"""
    payload = yaml.safe_load(contract_path(frozen, "REQ-11").read_text(encoding="utf-8"))
    assert payload["req_id"] == "REQ-11"
    assert payload["frozen"] is True
    assert payload["generated_at"]
    calls = {c["symbol"]: c for c in payload["cross_module_calls"]}
    assert set(calls) == {"updateQuantity", "listItems"}
    assert calls["updateQuantity"]["signature"] == "updateQuantity(sku, quantity, from, to)"
    assert calls["updateQuantity"]["declared_arity"] == 4
    assert calls["updateQuantity"]["side_effects"] == ["记录流水到 movements 数组"]


def test_regeneration_overwrites_stale_contract(tmp_path, reqs):
    """再次生成必须**覆盖**上一轮的旧合同（否则会拿旧合同跑新需求）。"""
    write_contracts(tmp_path, reqs)
    p = contract_path(tmp_path, "REQ-11")
    stale = yaml.safe_load(p.read_text(encoding="utf-8"))
    stale["cross_module_calls"][0]["signature"] = "updateQuantity(OLD, STALE)"
    p.write_text(yaml.safe_dump(stale, allow_unicode=True), encoding="utf-8")
    write_contracts(tmp_path, reqs)   # 重新冻结
    fresh = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "OLD" not in fresh["cross_module_calls"][0]["signature"]


# ═══════════════════════════════════════════════════════════════════════════
# 读取（提示词层只能读冻结件）
# ═══════════════════════════════════════════════════════════════════════════


def test_prompt_layer_reads_frozen_copy(frozen):
    """提示词层从冻结件读取，且能拿到声明的元数据。"""
    calls = load_frozen_calls(contracts_dir(frozen), "REQ-11")
    assert calls is not None
    by_symbol = {c.symbol: c for c in calls}
    assert by_symbol["updateQuantity"].declared_arity == 4
    assert by_symbol["listItems"].semantics == "查询库存项列表"


def test_prompt_layer_uses_contracts_dir_not_output_dir(tmp_path, reqs):
    """★ 防回归：读函数收的是**合同目录**，不是 output_dir。

    实测 bug：初版 load_frozen_calls 收 output_dir 并内部拼 `.arc/contracts`，
    而调用方传了 `contracts_dir(output_dir).parent` -> 路径多一层 `.arc/.arc/`
    -> 永远读不到 -> **静默回退到内存声明**。
    症状极具欺骗性：合同写出来了、门禁过了、提示词里也**有**合同内容
    （来自内存声明），只有「已冻结」这一性质没生效。

    本条同时钉住三个位置：
      ① 传合同目录 -> 读得到
      ② 传 output_dir（旧口径）-> 读不到（证明参数语义已改变）
      ③ 生成器实际传的就是合同目录
    """
    write_contracts(tmp_path, reqs)          # ★ 先写合同再断言读得到
    cdir = contracts_dir(tmp_path)
    assert load_frozen_calls(cdir, "REQ-11") is not None, "传合同目录应读得到"
    assert load_frozen_calls(tmp_path, "REQ-11") is None, (
        "传 output_dir 应读不到 —— 若这里读到了，说明参数语义又变回旧口径"
    )
    # ③ 生成器接线检查（源码级，防有人改回 .parent）
    import inspect
    from factory.generator import LLMGenerator
    src = inspect.getsource(LLMGenerator._requirement_brief)
    assert "load_frozen_calls(self.contracts_dir," in src, (
        "生成器必须直接传 self.contracts_dir，不得再出现 .parent"
    )
    assert "load_frozen_calls(self.contracts_dir.parent" not in src


def test_prompt_marks_contract_as_frozen(tmp_path, reqs):
    """接了冻结合同时，提示词必须显式标注「已冻结」—— 这是与内存声明的可观测差别。"""
    from factory.generator import LLMGenerator
    from factory.contracts import contracts_dir as cdir_of

    class _C:
        model = "x"

        def is_available(self):  # noqa: ANN201
            return True

    write_contracts(tmp_path, reqs)
    req11 = [r for r in reqs if r.req_id == "REQ-11"][0]
    off = LLMGenerator(_C(), "vitest", contracts_dir=None)._requirement_brief(req11)
    on = LLMGenerator(_C(), "vitest", contracts_dir=cdir_of(tmp_path))._requirement_brief(req11)
    assert "已冻结" not in off
    assert "已冻结" in on, "接了冻结合同却没标注「已冻结」—— 读路径可能又断了"
    assert len(on) > len(off), "冻结合同的提示词应比内存声明更长（多了冻结标注）"


def test_prompt_layer_returns_none_when_contract_absent(tmp_path):
    """合同不存在时返回 None（调用方据此回退并告警，而不是静默用错的内容）。"""
    assert load_frozen_calls(contracts_dir(tmp_path), "REQ-NOPE") is None


# ═══════════════════════════════════════════════════════════════════════════
# 门禁：CONTRACT_MISSING
# ═══════════════════════════════════════════════════════════════════════════


def test_gate_blocks_when_contract_missing(tmp_path, reqs):
    """① 合同不存在 -> 阻断（错误样本：证明能拦住）。"""
    from factory.contracts import check_contracts
    ok, checks = check_contracts(tmp_path, reqs)
    assert not ok, "合同缺失却放行 —— 门禁失效"
    bad = [c for c in checks if not c.ok]
    assert len(bad) == 1 and bad[0].req_id == "REQ-11"
    assert bad[0].reason == "CONTRACT_MISSING"


def test_gate_passes_when_contract_frozen_and_consistent(frozen, reqs):
    """② 合同存在且一致 -> 放行（正确样本：证明不误报）。"""
    from factory.contracts import check_contracts
    ok, checks = check_contracts(frozen, reqs)
    assert ok, f"合同完整却阻断: {[c.detail for c in checks if not c.ok]}"
    by_req = {c.req_id: c for c in checks}
    assert by_req["REQ-11"].reason == "CONTRACT_FROZEN_OK"
    # 未声明的需求也算 ok（不适用）
    assert by_req["REQ-1"].reason == "NO_CONTRACT_DECLARED"


def test_gate_blocks_when_not_frozen(frozen, reqs):
    """③ frozen != true -> 阻断（未冻结的合同不得使用）。"""
    from factory.contracts import check_contracts
    p = contract_path(frozen, "REQ-11")
    payload = yaml.safe_load(p.read_text(encoding="utf-8"))
    payload["frozen"] = False
    p.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    ok, checks = check_contracts(frozen, reqs)
    assert not ok
    assert any("未冻结" in c.detail for c in checks if not c.ok)


def test_gate_blocks_on_drift_between_contract_and_declaration(frozen, reqs):
    """④ 合同与需求声明**漂移** -> 阻断（并指出差了哪条）。"""
    from factory.contracts import check_contracts
    p = contract_path(frozen, "REQ-11")
    payload = yaml.safe_load(p.read_text(encoding="utf-8"))
    payload["cross_module_calls"] = payload["cross_module_calls"][:1]   # 少一条
    p.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    ok, checks = check_contracts(frozen, reqs)
    assert not ok
    bad = [c for c in checks if not c.ok][0]
    assert bad.reason == "CONTRACT_MISSING"
    assert "缺少" in bad.detail and "listItems" in bad.detail


def test_gate_blocks_on_malformed_contract(frozen, reqs):
    """⑤ 合同文件损坏 / 非映射 -> 阻断而不是崩溃。"""
    from factory.contracts import check_contracts
    contract_path(frozen, "REQ-11").write_text("- 这是个列表，不是映射\n", encoding="utf-8")
    ok, checks = check_contracts(frozen, reqs)
    assert not ok
    assert any(c.reason == "CONTRACT_MISSING" for c in checks if not c.ok)


def test_rejection_reason_is_executable(tmp_path, reqs):
    """拒绝理由必须**可执行**：不只是说错了，还要说下一步做什么。"""
    from factory.contracts import check_contracts
    _, checks = check_contracts(tmp_path, reqs)
    text = describe(checks)
    assert "修正指令" in text
    assert "不要改合同" in text
    assert "重新编译" in text


def test_contract_dir_layout(tmp_path):
    """合同目录固定为 .arc/contracts/。"""
    assert contracts_dir(tmp_path) == tmp_path / ".arc" / "contracts"
    assert contract_path(tmp_path, "REQ-9").name == "REQ-9.yaml"
