"""UI 契约正文提取的验证用例（粒度 A：元素清单 + 可访问名）。

运行：pytest tests/test_ui_contract_extraction.py -v

═══════════════════════════════════════════════════════════════════════════
 为什么有这个文件
═══════════════════════════════════════════════════════════════════════════
平台**从不发** `ui_contracts` 字段（6 个真实 app 的 requirements.yaml 全无），
于是第五道 UI 门禁在真实输入上永远走 NO_UI_CONTRACT_DECLARED —— 从未活过。
本文件验证「把契约来源改成正文提取」之后的提取质量。

权威参照（`.probe-upstream/arc-bench/webapp/*/`）：
    tests/REQ-*.spec.ts   真实基准测试（读需求读不到，但可用来**校验**提取）
    tests/helpers.ts      可访问名契约的定义 —— UI 门禁应对齐它

**语料缺失时整体跳过**：`.probe-upstream/` 由打包器排除，
包内 `check_all` 不应因为缺它而失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.adapter import _adapt, _parse_ui_contracts  # noqa: E402
from factory.uicontract import (  # noqa: E402
    contracts_from_text,
    extract_elements,
    requirement_text,
)

def _find_upstream():
    """定位真实语料目录。

    它可能**不在**本工作区里：worktree（`.wt/factory-verify`）的上一级
    （`.wt`）再上一级才是主工作区，`.probe-upstream/` 在主工作区根。
    从本文件向上逐级找，找到即用；找不到就整体跳过（打包时它被排除）。
    """
    for base in (ROOT, *ROOT.parents):
        candidate = base / ".probe-upstream" / "arc-bench" / "webapp"
        if candidate.is_dir():
            return candidate
    return None


UPSTREAM = _find_upstream()
APPS = ("12306", "bookstack", "ctrip", "keep", "prestashop", "stackoverflow")

# 实测基准（2026-10-02，见交付报告的测量表）。阈值取在实测值之下留余量。
MIN_TOTAL_COVERAGE = 0.60      # 实测 70.7%
MIN_PRECISION = 0.85           # 实测 90.8%
PER_APP_COVERAGE_FLOOR = {
    "12306": 0.85, "bookstack": 0.90, "ctrip": 0.55,
    "keep": 0.90, "prestashop": 0.30, "stackoverflow": 0.50,
}


def _need_corpus() -> None:
    if UPSTREAM is None:
        pytest.skip("真实语料缺失，跳过（.probe-upstream/ 被打包器排除）")


def _requirements(app: str):
    path = UPSTREAM / app / "requirements" / "requirements.yaml"
    return _adapt(yaml.safe_load(path.read_text(encoding="utf-8")),
                  source=path).requirements


def _atomics(app: str):
    return [r for r in _requirements(app) if not r.children_ids]


def _test_corpus(app: str) -> str:
    return "\n".join(
        f.read_text(encoding="utf-8", errors="replace")
        for f in sorted((UPSTREAM / app / "tests").glob("*.ts"))
    )


# ---------------------------------------------------------------------------
# ① 提取规则本身（不需要语料）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "want_type", "want_name"),
    [
        # 任务书点名的三条
        ("The page contains exactly one visible heading with accessible name `BookStack`.",
         "heading", "BookStack"),
        ("It shows one textbox labeled 'Username'.",
         "textbox", "Username"),
        ("There is a password input labeled \"Password\".",
         "password", "Password"),
        # 真实语料里的其它主导句式
        ("The system displays the shelf details page with a visible heading \"Shelf 4.2.1\".",
         "heading", "Shelf 4.2.1"),
        ("the input with the placeholder \"Order number\"",
         "textbox", "Order number"),
        ("a textbox with placeholder `Page title`",
         "textbox", "Page title"),
        ("the `Save Page` button is visible",
         "button", "Save Page"),
        ("the \"History orders\" tab with the placeholder \"Order number\"",
         "tab", "History orders"),
        # 全角引号 + 动作动词（ctrip 通篇这种写法）
        ("Click “注册” in the top-right corner of the homepage",
         "button", "注册"),
    ],
)
def test_extraction_rules(text, want_type, want_name):
    got = extract_elements(text)
    assert got, f"没提取到任何元素: {text!r}"
    assert any(e["type"] == want_type and e["name"] == want_name for e in got), \
        f"期望 {want_type}:{want_name!r}，实际 {[(e['type'], e['name']) for e in got]}"


@pytest.mark.parametrize(
    "text",
    [
        # 邮箱里的点号曾把句子劈开 -> 悬空引号配对 -> 产出 ', enters ' 这种"名字"
        'Seed data: account with nickname "BookStack User", email "bookstack_user@example.com".',
        "verification code REQ-1.1 was shown",
        '"the whole clause, with a comma, is quoted"',
        '"<placeholder>"',
    ],
)
def test_no_clause_fragments_or_values_as_names(text):
    """整句片段 / 数据值 / 占位符都不能被当成可访问名（宁缺勿滥）。"""
    for e in extract_elements(text):
        name = e["name"]
        assert "," not in name and len(name) <= 40, f"提取到片段: {name!r}"


def test_values_are_not_names():
    """实测误报：`enters \\`Password123!\\` in the ... textbox \\`Password\\``。

    左边那个是**要输入的取值**，右边才是元素名。加窗后只应提取到名字。
    """
    text = ("The user enters `bookstack_user@example.com` in the uniquely labelled "
            "email textbox `Email address`.")
    names = [e["name"] for e in extract_elements(text)]
    assert "Email address" in names, names
    assert "bookstack_user@example.com" not in names, f"把取值当成了名字: {names}"


def test_field_path_still_wins():
    """**字段优先**：显式声明了 ui_contracts 的需求，不得被正文提取覆盖。"""
    node = {
        "id": "R", "description": "exactly one visible heading with accessible name `X`",
        "ui_contracts": [{"page": "/p", "elements": [{"id": "declared", "type": "button",
                                                      "label": "Declared"}]}],
    }
    declared = _parse_ui_contracts(node)
    assert declared and declared[0].elements[0].element_id == "declared"
    # adapter 的规则是 `if not requirement.ui_contracts:` 才提取
    spec = {"requirements": [{**node, "req_id": "R"}]}
    reqs = _adapt(spec, source=Path("x")).requirements
    assert reqs[0].ui_contracts[0].elements[0].element_id == "declared", \
        "字段被正文提取覆盖了 —— 违反字段优先"


def test_extraction_is_opt_in_on_real_shape():
    """真实形状（无 ui_contracts 字段）经 adapter 后应带上提取出的契约。"""
    spec = {"id": "ROOT", "name": "T", "type": "FOLDER", "children": [
        {"id": "REQ-1", "name": "Home", "type": "ATOMIC",
         "description": "The page shows exactly one visible heading with accessible name `Alpha`."},
    ]}
    reqs = _adapt(spec, source=Path("x")).requirements
    leaf = [r for r in reqs if r.req_id == "REQ-1"][0]
    assert leaf.ui_contracts, "真实形状没有触发正文提取"
    assert leaf.ui_contracts[0].elements[0].label == "Alpha"


# ---------------------------------------------------------------------------
# ② 真实语料上的覆盖率与精确率
# ---------------------------------------------------------------------------

def test_coverage_on_real_requirements():
    _need_corpus()
    with_ui = total = 0
    per_app: dict[str, tuple[int, int]] = {}
    for app in APPS:
        atoms = _atomics(app)
        hit = sum(1 for r in atoms if extract_elements(requirement_text(r)))
        per_app[app] = (hit, len(atoms))
        with_ui += hit
        total += len(atoms)
    assert with_ui / total >= MIN_TOTAL_COVERAGE, f"总覆盖率过低: {per_app}"
    for app, (hit, n) in per_app.items():
        assert hit / n >= PER_APP_COVERAGE_FLOOR[app], f"{app} 覆盖率 {hit}/{n} 低于下限"


def test_precision_against_real_benchmark_tests():
    """★提取的可访问名，有多少真的出现在平台基准测试 / helpers 里。

    这是**精确率**代理指标：名字被平台真实使用，说明不是我们凭空造的。
    召回率测不准（helpers 大量用 role + 正则），故只断言精确率。
    """
    _need_corpus()
    total = hit = 0
    for app in APPS:
        corpus = _test_corpus(app).lower()
        names = {e["name"] for r in _atomics(app) for e in extract_elements(requirement_text(r))}
        total += len(names)
        hit += sum(1 for n in names if n.lower() in corpus)
    assert total > 100, f"样本太小: {total}"
    assert hit / total >= MIN_PRECISION, f"精确率 {hit}/{total} = {hit/total:.1%}"


def test_helpers_contract_is_role_based():
    """对齐依据：真实 helpers 的定位方式以 **getByRole** 为主。

    如果这一点不成立，我们「按可访问名判覆盖」的方向就是错的 —— 所以钉住它。
    """
    _need_corpus()
    corpus = "\n".join(_test_corpus(app) for app in APPS)
    counts = {k: corpus.count(k) for k in
              ("getByRole", "getByLabel", "getByText", "getByPlaceholder")}
    assert counts["getByRole"] > 0, counts
    assert counts["getByRole"] > counts["getByLabel"] + counts["getByText"], counts


def test_ctrip_regression_fullwidth_quotes():
    """★回归：ctrip 通篇用全角引号 `` “注册” ``，只认 ASCII 引号时它 **0/133**。"""
    _need_corpus()
    atoms = _atomics("ctrip")
    hit = sum(1 for r in atoms if extract_elements(requirement_text(r)))
    assert hit > 50, f"ctrip 提取数回落到 {hit}/{len(atoms)}（全角引号支持被破坏？）"


def test_bookstack_req_1_1_exact():
    """最干净的一条：REQ-1.1 应恰好提取出 heading `BookStack`。"""
    _need_corpus()
    req = [r for r in _atomics("bookstack") if r.req_id == "REQ-1.1"][0]
    els = extract_elements(requirement_text(req))
    assert [(e["type"], e["name"]) for e in els] == [("heading", "BookStack")], els


def test_contracts_from_text_shape():
    """产出的 UIContract 形状要能被门禁直接吃（element_id 唯一、label 即可访问名）。"""
    contracts = contracts_from_text(
        "a textbox labeled 'Username' and a password input labeled 'Username'",
        req_id="REQ-X", title="T")
    assert contracts, "未产出契约"
    ids = [e.element_id for e in contracts[0].elements]
    assert len(ids) == len(set(ids)), f"element_id 重复: {ids}"
    assert all(e.label for e in contracts[0].elements)
