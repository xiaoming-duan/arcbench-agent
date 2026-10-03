"""RED 三态判定（步骤⑤）的验证用例。

运行：pytest tests/test_valid_red_unverified.py -v

═══════════════════════════════════════════════════════════════════════════
 为什么需要第三态
═══════════════════════════════════════════════════════════════════════════
此前只有二值：`weak`（阻断）或「通过」。于是两件不同的事在结果上不可区分：

  「失败原因**已确认**是实现尚未生产（import 不到）」
  「它失败了，但**我不知道为什么**」

后者被静默当成前者放行 —— 读日志的人无法发现成因未归类。
新增 `VALID_RED_UNVERIFIED` 把这件事**显式记下来**（不算 WEAK_TEST）。
═══════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "arcbench-agent-runtime" / "src"))

from factory.models import REQUIREMENT_RESULT_FIELDS, RequirementResult  # noqa: E402

THREE_STATES = {"VALID_RED", "VALID_RED_UNVERIFIED", "WEAK_TEST", "TEST_BROKEN"}


def test_field_exists_and_defaults_empty():
    r = RequirementResult(req_id="R", state="PENDING")
    assert hasattr(r, "red_verdict")
    assert r.red_verdict == "", "默认应为空串（未进入 RED 判定的需求）"


def test_field_is_serialized():
    """必须进序列化清单，否则报告里看不到 —— 与「机制工作但不可观测」同类。"""
    assert "red_verdict" in REQUIREMENT_RESULT_FIELDS


def test_three_states_are_distinct():
    """三态必须是**不同**的字符串（同义字符串会让判定形同虚设）。"""
    vals = ["VALID_RED", "VALID_RED_UNVERIFIED", "WEAK_TEST"]
    assert len(set(vals)) == 3
    assert "VALID_RED" != "VALID_RED_UNVERIFIED", (
        "前缀相同但必须不等 —— 否则「已确认」与「未归类」无法区分"
    )


def test_loop_records_all_three_states():
    """源码级：loop 必须为三种情形分别赋值（防止有人只改两处）。"""
    import inspect
    from factory.loop import TddLoop
    src = inspect.getsource(TddLoop.run)
    for state in ("VALID_RED", "VALID_RED_UNVERIFIED", "WEAK_TEST", "TEST_BROKEN"):
        assert f'"{state}"' in src, f"loop 未记录 {state}"
    # 关键：未确认态必须与已确认态**分支不同**
    assert 'elif _expected is not None:' in src, (
        "必须有 `elif _expected is not None` 分支 —— 否则两态被合并"
    )
    assert 'result.red_verdict = "VALID_RED_UNVERIFIED"' in src


def test_unverified_state_is_logged_not_silent():
    """未确认态必须**留痕**（日志），否则它与已确认态在日志上也一样。"""
    import inspect
    from factory.loop import TddLoop
    src = inspect.getsource(TddLoop.run)
    i = src.find('result.red_verdict = "VALID_RED_UNVERIFIED"')
    assert i != -1
    window = src[i:i + 600]
    assert "logger.warning" in window, "未确认态应记 warning 供复核"
    assert "red_verdict=" in src, "应打印最终判定，便于从日志直接读到三态"


def test_does_not_change_blocking_behaviour():
    """★ 反向：新增状态**不改变**阻断行为 —— WEAK_TEST 仍阻断，未确认态仍放行。

    这是本次改动的边界：只增加**可观测性**，不改变通过/阻断判定。
    """
    import inspect
    from factory.loop import TddLoop
    src = inspect.getsource(TddLoop.run)
    # weak 才进入阻断分支；red_verdict 不参与 weak 的计算
    assert "if weak:" in src
    # 断言 red_verdict 不出现在 weak 的赋值表达式里
    import re
    m = re.search(r"weak = (.+)", src)
    assert m and "red_verdict" not in m.group(1), (
        "red_verdict 不得参与 weak 计算 —— 否则会改变阻断行为"
    )
