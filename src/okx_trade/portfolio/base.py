"""Allocator 抽象 + ``allocate`` 协议。

策略组合优化的最小接口：给一组 strategy id + 总 equity → 每策略分到的 USDT。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..pnl import PnLTracker


class Allocator(ABC):
    """策略资金分配器。

    输入：所有 enabled 策略 id + ``PnLTracker``（读历史 PnL）+ 总 equity。
    输出：每个策略分到的 USDT 配额（``sum == total_equity_usdt``）。

    具体实现：
    - ``EqualWeightAllocator``：1/N，冷启动期默认。
    - ``RiskBudgetingAllocator``：inverse-vol 加权 + correlation 惩罚；
      样本不足回退 equal weight。
    """

    @abstractmethod
    def allocate(
        self,
        strategy_ids: list[str],
        tracker: PnLTracker,
        *,
        total_equity_usdt: Decimal,
    ) -> dict[str, Decimal]:
        raise NotImplementedError


__all__ = ["Allocator"]
