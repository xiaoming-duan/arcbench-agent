"""软件工厂（Software Factory）—— ARC-Bench 最小可用闭环实现。

分层对应架构文档：
  控制面 -> pipeline.py / loop.py
  正确面 -> testrunner.py / loop.py 的 RED-GREEN 门禁
  数据面 -> store.py（复用 ARC-Bench SDK 的 traceability / events / git）
"""

from .config import FactoryConfig
from .pipeline import run_factory

__all__ = ["FactoryConfig", "run_factory"]
