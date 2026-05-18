"""basis_arb 单测：纯函数 + helper（NT 层 e2e 由 paper trading 验证）。"""
from __future__ import annotations

import pytest

from okx_trade.strategies._signals import (
    BasisAction,
    BasisArbParams,
    annualized_basis,
    basis_decision,
)
from okx_trade.strategies.basis_arb import _carry_position_size


class TestCarryPositionSize:
    """两腿 USDT notional 对齐。"""

    def test_basic_alignment(self) -> None:
        # spot 60000，equity 10000，pct 30% → budget 3000 → spot_qty 0.05
        # futures_ct_val=0.01 → raw_contracts = 0.05 / 0.01 = 5
        spot_qty, contracts = _carry_position_size(
            account_equity_usdt=10000.0,
            max_position_pct=0.30,
            spot_price=60000.0,
            futures_ct_val=0.01,
            futures_lot_sz=1.0,
        )
        assert contracts == 5.0
        # spot_qty 反推 = 5 × 0.01 = 0.05
        assert spot_qty == pytest.approx(0.05)

    def test_rounding_down_to_lot(self) -> None:
        # raw=5.7 张，lot=1 → 5 张
        spot_qty, contracts = _carry_position_size(
            account_equity_usdt=11400.0,  # → budget 3420 → spot_qty 0.057 → raw 5.7
            max_position_pct=0.30,
            spot_price=60000.0,
            futures_ct_val=0.01,
            futures_lot_sz=1.0,
        )
        assert contracts == 5.0  # floor 5.7 → 5
        assert spot_qty == pytest.approx(0.05)

    def test_returns_zero_when_undersized(self) -> None:
        # equity 太小 → raw < 1 lot → 全部 0
        spot_qty, contracts = _carry_position_size(
            account_equity_usdt=100.0,
            max_position_pct=0.30,
            spot_price=60000.0,
            futures_ct_val=0.01,
            futures_lot_sz=1.0,
        )
        assert spot_qty == 0.0
        assert contracts == 0.0

    def test_zero_inputs_returns_zero(self) -> None:
        assert _carry_position_size(
            account_equity_usdt=0.0, max_position_pct=0.30,
            spot_price=60000.0, futures_ct_val=0.01, futures_lot_sz=1.0,
        ) == (0.0, 0.0)
        assert _carry_position_size(
            account_equity_usdt=10000.0, max_position_pct=0.30,
            spot_price=0.0, futures_ct_val=0.01, futures_lot_sz=1.0,
        ) == (0.0, 0.0)


class TestBasisDecisionEndToEnd:
    """basis_decision 在典型场景下的状态机演化。"""

    def test_enter_then_hold_then_exit_on_convergence(self) -> None:
        params = BasisArbParams(entry_basis_apr=0.12, exit_basis_apr=0.02, force_close_days_before_expiry=2)
        # T0: basis 高，无仓 → ENTER
        assert basis_decision(
            ann_basis=annualized_basis(60500.0, 60000.0, 30.0),  # ~10.1% APR — 不够
            days_to_expiry=30.0, has_position=False, params=params,
        ) == BasisAction.HOLD
        # 把 futures 价拉高 → ann_basis ~17% APR → ENTER
        ann = annualized_basis(61000.0, 60000.0, 30.0)
        assert ann > 0.15
        assert basis_decision(ann, 30.0, False, params) == BasisAction.ENTER
        # T1: 有仓，basis 还高 → HOLD
        assert basis_decision(ann, 25.0, True, params) == BasisAction.HOLD
        # T2: basis 收敛 → EXIT
        ann_low = annualized_basis(60030.0, 60000.0, 20.0)  # ~0.9% APR
        assert ann_low < 0.02
        assert basis_decision(ann_low, 20.0, True, params) == BasisAction.EXIT

    def test_force_close_overrides_basis(self) -> None:
        params = BasisArbParams(entry_basis_apr=0.12, exit_basis_apr=0.02, force_close_days_before_expiry=2)
        # basis 还高，但已经到期前 1.5 天 → 强平
        assert basis_decision(0.20, 1.5, True, params) == BasisAction.EXIT


def test_strategy_registry_includes_basis_arb() -> None:
    """basis_arb 应该被 live_node 自动注册（lazy import）。"""
    from okx_trade.runtime.live_node import _strategy_registry
    registry = _strategy_registry()
    assert "basis_arb" in registry
    cfg_cls, strat_cls = registry["basis_arb"]
    assert cfg_cls.__name__ == "BasisArbConfig"
    assert strat_cls.__name__ == "BasisArbStrategy"
