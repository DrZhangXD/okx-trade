"""M5 EqualWeightAllocator 单测。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from okx_trade.pnl import PnLTracker
from okx_trade.portfolio import EqualWeightAllocator


def test_equal_weight_two_strategies() -> None:
    tracker = PnLTracker(":memory:")
    alloc = EqualWeightAllocator()
    out = alloc.allocate(
        ["s1", "s2"], tracker, total_equity_usdt=Decimal("10000"),
    )
    assert out == {"s1": Decimal("5000"), "s2": Decimal("5000")}
    tracker.close()


def test_equal_weight_four_strategies() -> None:
    tracker = PnLTracker(":memory:")
    alloc = EqualWeightAllocator()
    out = alloc.allocate(
        ["s1", "s2", "s3", "s4"], tracker, total_equity_usdt=Decimal("10000"),
    )
    assert sum(out.values()) == Decimal("10000")
    assert all(v == Decimal("2500") for v in out.values())
    tracker.close()


def test_equal_weight_one_strategy_gets_everything() -> None:
    tracker = PnLTracker(":memory:")
    alloc = EqualWeightAllocator()
    out = alloc.allocate(["s1"], tracker, total_equity_usdt=Decimal("10000"))
    assert out == {"s1": Decimal("10000")}
    tracker.close()


def test_equal_weight_handles_residual_rounding() -> None:
    tracker = PnLTracker(":memory:")
    alloc = EqualWeightAllocator()
    # 10000 / 3 = 3333.33... ; 残差应加到最后一个
    out = alloc.allocate(
        ["s1", "s2", "s3"], tracker, total_equity_usdt=Decimal("10000"),
    )
    assert sum(out.values()) == Decimal("10000")
    tracker.close()


def test_equal_weight_empty_raises() -> None:
    tracker = PnLTracker(":memory:")
    alloc = EqualWeightAllocator()
    with pytest.raises(ValueError):
        alloc.allocate([], tracker, total_equity_usdt=Decimal("10000"))
    tracker.close()
