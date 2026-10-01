"""判据双向验证框架：**判据本身**也必须被验证。

═══════════════════════════════════════════════════════════════════════════
 为什么需要它（不是纪律，是强制步骤）
═══════════════════════════════════════════════════════════════════════════
架构文档第 19 章记录了**四次同类自伤**，共同结构是：

    判据选错了  ->  报红/报绿  ->  归因到被测对象  ->  改错的地方

| # | 事故 | 判据错在哪 |
|---|---|---|
| 1 | T36 自伤 | 用裸子串匹配，命中了自己注释里引用的同一串文字 |
| 2 | fixture 转义 | 夹具里 `\\n` 写成字面量，整段变一行 |
| 3 | 元断言输出 | detail 无论对错都打印，写成失败语 |
| 4 | 提示词校验 | 用整串相等判断「只影响根节点」，但子节点本就会多一行 |

19.2 的前 6 条做法都是**事后**补的规则。本模块是**事前**机制：

    一个判据必须同时给出
      ① 正确样本 -> 判据**必须通过**（证明不误报）
      ② 错误样本 -> 判据**必须失败**（证明能拦住）
    只给一个方向的判据视为**未验证**。

═══════════════════════════════════════════════════════════════════════════
 用法
═══════════════════════════════════════════════════════════════════════════
    from tools.criterion import criterion, verify_all

    @criterion(
        "T36 审计在 if outcome.passed 之前",
        valid="\\n            if outcome.passed:\\n",
        invalid="\\n        if outcome.passed:\\n",
        note="必须匹配 12 空格缩进的代码行",
    )
    def probe(text: str) -> bool:
        return "\\n            if outcome.passed:\\n" in text

    results = verify_all()      # 每条判据跑两个方向
    assert all(ok for _, ok, _ in results)
═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("factory.criterion")


@dataclass
class Criterion:
    """一个带双向样本的判据。

    probe 接受任意「被测对象」（源码文本 / 数据结构 / 对象），返回判据是否通过。
    """

    name: str
    probe: Callable[[Any], bool]
    valid: Any
    invalid: Any
    note: str = ""
    # 可选：判据可能抛异常（如属性不存在）—— 抛异常同样视为「未通过」
    allow_exception: bool = True
    tags: tuple[str, ...] = field(default_factory=tuple)


REGISTRY: list[Criterion] = []


def criterion(
    name: str,
    *,
    valid: Any,
    invalid: Any,
    note: str = "",
    allow_exception: bool = True,
    tags: tuple[str, ...] = (),
) -> Callable[[Callable[[Any], bool]], Callable[[Any], bool]]:
    """注册一个判据。装饰器形式，便于在被测判据旁边就地声明两个样本。"""

    def deco(fn: Callable[[Any], bool]) -> Callable[[Any], bool]:
        REGISTRY.append(Criterion(name=name, probe=fn, valid=valid, invalid=invalid,
                                  note=note, allow_exception=allow_exception, tags=tags))
        return fn

    return deco


def _run(probe: Callable[[Any], bool], sample: Any, allow_exception: bool) -> tuple[bool, str]:
    try:
        return bool(probe(sample)), ""
    except Exception as exc:  # noqa: BLE001
        if allow_exception:
            return False, f"{type(exc).__name__}: {exc}"
        raise


def verify_one(c: Criterion) -> tuple[str, bool, str]:
    """双向验证单条判据。返回 (名称, 是否有效, 说明)。

    有效性 = **正确样本通过** 且 **错误样本失败**。
    任一不满足 -> 判据无效（不是被测对象有问题）。
    """
    ok_valid, err_valid = _run(c.probe, c.valid, c.allow_exception)
    ok_invalid, err_invalid = _run(c.probe, c.invalid, c.allow_exception)

    if not ok_valid and not ok_invalid:
        # 两个方向都不过 —— 判据恒为假，它拦不住任何东西，只会制造噪音
        return c.name, False, f"判据对**两个样本都失败**（恒假）: valid={err_valid or 'False'}"
    if ok_valid and ok_invalid:
        # 两个方向都过 —— 判据恒为真，漏检
        return c.name, False, "判据对**两个样本都通过**（恒真，漏检）—— 需要更严格的判据"
    if not ok_valid:
        return c.name, False, f"**正确样本未通过**（误报）{': ' + err_valid if err_valid else ''}"
    if ok_invalid:
        return c.name, False, "**错误样本却通过了**（漏检）—— 判据拦不住这个反例"
    return c.name, True, "双向验证通过（正确样本过 / 错误样本败）"


def verify_all(*, tags: tuple[str, ...] = ()) -> list[tuple[str, bool, str]]:
    """验证注册表里的全部判据（可按 tag 过滤）。"""
    items = [c for c in REGISTRY if not tags or (set(tags) & set(c.tags))]
    return [verify_one(c) for c in items]


def clear() -> None:
    """清空注册表（测试之间隔离用）。"""
    REGISTRY.clear()


def unverified_probe_names(probes: dict[str, Callable[..., Any]]) -> list[str]:
    """给定一批探针名，返回其中**未在注册表里声明双向样本**的。

    供构建流水线强制：新写的判据必须在 REGISTRY 里登记，
    否则报「未验证」而不是默默放行。
    """
    covered = {c.name for c in REGISTRY}
    return sorted(n for n in probes if n not in covered)
