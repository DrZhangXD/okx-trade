"""1/N 平均权重 allocator（冷启动默认）。"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from .base import Allocator

if TYPE_CHECKING:
    from ..pnl import PnLTracker


class EqualWeightAllocator(Allocator):
    """每个 enabled 策略均分 ``total_equity_usdt``。

    - 30 日 PnL 数据未到位时，``RiskBudgetingAllocator`` 也会回退到这个口径；
    - 不读 ``tracker``，但仍保留参数以满足 ``Allocator`` 协议。
    """

    def allocate(
        self,
        strategy_ids: list[str],
        tracker: PnLTracker,
        *,
        total_equity_usdt: Decimal,
    ) -> dict[str, Decimal]:
        if not strategy_ids:
            raise ValueError("strategy_ids must be non-empty")
        n = len(strategy_ids)
        per = total_equity_usdt / Decimal(n)
        out = {sid: per for sid in strategy_ids}
        # 处理舍入：把残差加到最后一个策略
        residual = total_equity_usdt - per * Decimal(n)
        if residual != 0:
            last = strategy_ids[-1]
            out[last] = out[last] + residual
        return out


__all__ = ["EqualWeightAllocator"]
