"""信号确认门控 —— 给主策略下单前过一道过滤器。

模块本身不独立交易，作为 ``Strategy`` 的 helper 使用。

可用门控：

- ``ofi`` —— OrderFlow Imbalance：基于 OKX ``trades`` 频道的 taker 净流；
  反向时降仓位 50% 或跳过。
"""
from __future__ import annotations

from .ofi import (
    OFIDecision,
    OFIFilter,
    TradeFlow,
    ofi_filter_decision,
    ofi_score,
)

__all__ = [
    "OFIDecision",
    "OFIFilter",
    "TradeFlow",
    "ofi_filter_decision",
    "ofi_score",
]
