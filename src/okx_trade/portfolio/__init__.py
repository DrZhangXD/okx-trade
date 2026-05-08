"""策略组合优化（M5）：Allocator 抽象 + equal-weight + risk-budget 实现。

冷启动用 ``EqualWeightAllocator``；30 日 PnL 数据到位后切 ``RiskBudgetingAllocator``。
"""
from __future__ import annotations

from .base import Allocator
from .equal_weight import EqualWeightAllocator
from .risk_budget import (
    RiskBudgetingAllocator,
    correlation_penalty,
    inverse_vol_weights,
    risk_budget_weights,
)

__all__ = [
    "Allocator",
    "EqualWeightAllocator",
    "RiskBudgetingAllocator",
    "correlation_penalty",
    "inverse_vol_weights",
    "risk_budget_weights",
]
