"""测试 strategies._signals 纯函数。"""
from __future__ import annotations

import math

import pytest

from okx_trade.strategies._signals import (
    BasisAction,
    BasisArbParams,
    annualized_basis,
    basis_decision,
    book_imbalance,
    microprice,
    ob_signal,
)


# ---------------------------------------------------------------------------
# microprice
# ---------------------------------------------------------------------------


class TestMicroprice:
    def test_equal_depth_returns_mid(self) -> None:
        # bid_qty == ask_qty → microprice == (bid + ask) / 2
        assert microprice(100.0, 10.0, 101.0, 10.0) == pytest.approx(100.5)

    def test_bid_heavy_pushes_up(self) -> None:
        # bid_qty >> ask_qty → microprice 偏向 ask 侧（卖方稀缺，价格上推）
        mp = microprice(100.0, 100.0, 101.0, 1.0)
        # 公式：(100 * 1 + 101 * 100) / 101 = 100.99
        assert mp == pytest.approx(100.99, abs=1e-3)
        assert mp > 100.5  # 比 mid 高

    def test_ask_heavy_pushes_down(self) -> None:
        mp = microprice(100.0, 1.0, 101.0, 100.0)
        # (100 * 100 + 101 * 1) / 101 = 100.01
        assert mp == pytest.approx(100.01, abs=1e-3)
        assert mp < 100.5

    def test_zero_depth_falls_back_to_mid(self) -> None:
        assert microprice(100.0, 0.0, 101.0, 0.0) == pytest.approx(100.5)

    def test_no_quotes_returns_zero(self) -> None:
        assert microprice(0.0, 0.0, 0.0, 0.0) == 0.0


# ---------------------------------------------------------------------------
# book_imbalance
# ---------------------------------------------------------------------------


class TestBookImbalance:
    def test_balanced_returns_zero(self) -> None:
        assert book_imbalance([10, 10, 10], [10, 10, 10]) == 0.0

    def test_all_bid_returns_plus_one(self) -> None:
        assert book_imbalance([10, 5, 3], [0, 0, 0]) == 1.0

    def test_all_ask_returns_minus_one(self) -> None:
        assert book_imbalance([0, 0, 0], [10, 5, 3]) == -1.0

    def test_depth_truncation(self) -> None:
        # depth=2 时只看前 2 档：bid=20+10=30, ask=10+10=20 → (30-20)/50 = 0.2
        assert book_imbalance([20, 10, 999], [10, 10, 999], depth=2) == pytest.approx(0.2)

    def test_empty_returns_zero(self) -> None:
        assert book_imbalance([], []) == 0.0


# ---------------------------------------------------------------------------
# ob_signal
# ---------------------------------------------------------------------------


class TestObSignal:
    def test_long_when_bid_imbalance_and_microprice_above_mid(self) -> None:
        # imb=0.5 > 0.35, microprice 比 mid 高 5bp > 3bp
        assert ob_signal(
            imbalance=0.5,
            mid_px=100.0,
            micro_px=100.05,
            imbalance_threshold=0.35,
            microprice_premium_bps=3.0,
        ) == "long"

    def test_short_when_ask_imbalance_and_microprice_below_mid(self) -> None:
        assert ob_signal(
            imbalance=-0.5,
            mid_px=100.0,
            micro_px=99.95,
            imbalance_threshold=0.35,
            microprice_premium_bps=3.0,
        ) == "short"

    def test_no_signal_when_imbalance_below_threshold(self) -> None:
        assert ob_signal(0.20, 100.0, 100.05) is None

    def test_no_signal_when_microprice_not_confirming(self) -> None:
        # imbalance 大但 microprice 没动 → 可能是 spoof，过滤掉
        assert ob_signal(0.5, 100.0, 100.0) is None

    def test_no_signal_when_microprice_disagrees(self) -> None:
        # imbalance 偏 bid 但 microprice 反而偏 ask → 矛盾，过滤
        assert ob_signal(0.5, 100.0, 99.95) is None

    def test_zero_mid_returns_none(self) -> None:
        assert ob_signal(0.5, 0.0, 100.0) is None


# ---------------------------------------------------------------------------
# annualized_basis
# ---------------------------------------------------------------------------


class TestAnnualizedBasis:
    def test_contango(self) -> None:
        # futures=$100 spot=$98, 30 days → ((2/98) * 365/30) ≈ 0.2483 = ~24.8% APR
        ann = annualized_basis(100.0, 98.0, 30.0)
        assert ann == pytest.approx(0.2483, abs=1e-3)

    def test_backwardation_negative(self) -> None:
        ann = annualized_basis(98.0, 100.0, 30.0)
        assert ann < 0
        assert ann == pytest.approx(-0.2433, abs=1e-3)

    def test_zero_days_returns_zero(self) -> None:
        assert annualized_basis(100.0, 98.0, 0.0) == 0.0

    def test_negative_days_returns_zero(self) -> None:
        assert annualized_basis(100.0, 98.0, -1.0) == 0.0

    def test_zero_spot_returns_zero(self) -> None:
        assert annualized_basis(100.0, 0.0, 30.0) == 0.0


# ---------------------------------------------------------------------------
# basis_decision
# ---------------------------------------------------------------------------


class TestBasisDecision:
    def _params(self) -> BasisArbParams:
        return BasisArbParams(
            entry_basis_apr=0.12,
            exit_basis_apr=0.02,
            force_close_days_before_expiry=2,
        )

    def test_enter_when_basis_above_threshold_no_position(self) -> None:
        action = basis_decision(0.15, days_to_expiry=30, has_position=False, params=self._params())
        assert action == BasisAction.ENTER

    def test_hold_when_basis_below_threshold_no_position(self) -> None:
        action = basis_decision(0.05, days_to_expiry=30, has_position=False, params=self._params())
        assert action == BasisAction.HOLD

    def test_exit_when_basis_below_exit_threshold_with_position(self) -> None:
        action = basis_decision(0.01, days_to_expiry=30, has_position=True, params=self._params())
        assert action == BasisAction.EXIT

    def test_hold_when_basis_above_exit_with_position(self) -> None:
        action = basis_decision(0.08, days_to_expiry=30, has_position=True, params=self._params())
        assert action == BasisAction.HOLD

    def test_force_close_near_expiry(self) -> None:
        # 距到期 < 2 天 → 即使 basis 仍高也强制平仓
        action = basis_decision(0.20, days_to_expiry=1.5, has_position=True, params=self._params())
        assert action == BasisAction.EXIT

    def test_no_force_close_when_no_position(self) -> None:
        # 空仓时不会因临近到期触发 EXIT，只是不入场
        action = basis_decision(0.20, days_to_expiry=1.5, has_position=False, params=self._params())
        assert action == BasisAction.ENTER
        # （注意：basis_arb 策略层应过滤掉到期太近的合约不入场，这是策略职责而非纯函数职责）

    def test_negative_basis_no_entry(self) -> None:
        action = basis_decision(-0.10, days_to_expiry=30, has_position=False, params=self._params())
        assert action == BasisAction.HOLD
