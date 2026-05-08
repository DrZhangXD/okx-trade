"""``funding_carry`` 纯函数 + 仓位计算单测。"""
from __future__ import annotations

import pytest

from okx_trade.strategies.funding_carry import (
    CarryAction,
    FundingCarryParams,
    carry_position_size,
    funding_carry_decision,
)

# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------


class TestFundingDecision:
    def setup_method(self) -> None:
        # entry=8% APR, exit=2% APR
        # 8% APR ≈ 0.0000730 / 8h；2% APR ≈ 0.0000183 / 8h
        self.params = FundingCarryParams(
            entry_apr_threshold=0.08,
            exit_apr_threshold=0.02,
            max_position_pct=0.30,
        )

    def test_no_position_low_funding_holds(self) -> None:
        # 1% APR < 8% entry → HOLD
        rate = 0.01 / (3 * 365)
        assert funding_carry_decision(rate, has_position=False, params=self.params) \
            == CarryAction.HOLD

    def test_no_position_high_funding_enters(self) -> None:
        # 15% APR > 8% entry → ENTER
        rate = 0.15 / (3 * 365)
        assert funding_carry_decision(rate, has_position=False, params=self.params) \
            == CarryAction.ENTER

    def test_has_position_high_funding_holds(self) -> None:
        # 已持仓且 APR=15% > exit=2% → HOLD
        rate = 0.15 / (3 * 365)
        assert funding_carry_decision(rate, has_position=True, params=self.params) \
            == CarryAction.HOLD

    def test_has_position_low_funding_exits(self) -> None:
        # 已持仓且 APR=1% < exit=2% → EXIT
        rate = 0.01 / (3 * 365)
        assert funding_carry_decision(rate, has_position=True, params=self.params) \
            == CarryAction.EXIT

    def test_negative_funding_exits_existing_position(self) -> None:
        # funding 转负 → 立刻 EXIT（因为我们是 long spot + short perp，funding 转负
        # 意味着空头要付费给多头，对策略不利）
        rate = -0.0001  # -0.01%/8h ≈ -11% APR
        assert funding_carry_decision(rate, has_position=True, params=self.params) \
            == CarryAction.EXIT

    def test_no_position_negative_funding_holds(self) -> None:
        # 空仓状态下 funding 负不开仓（开了反而亏）
        rate = -0.0001
        assert funding_carry_decision(rate, has_position=False, params=self.params) \
            == CarryAction.HOLD

    @pytest.mark.parametrize("apr,expected", [
        (0.05, CarryAction.HOLD),    # 5% 在 entry 阈值之下
        (0.08, CarryAction.ENTER),   # 8% 恰好在 entry 阈值（>=）
        (0.50, CarryAction.ENTER),   # 极端高 funding
    ])
    def test_entry_threshold_boundary(self, apr: float, expected: CarryAction) -> None:
        rate = apr / (3 * 365)
        assert funding_carry_decision(rate, has_position=False, params=self.params) \
            == expected


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


class TestCarryPositionSize:
    def test_basic_delta_neutral(self) -> None:
        # equity=10000, max_pct=30% → budget=3000 USDT
        # spot_price=60000 → 0.05 BTC
        # ct_val=0.01 BTC → 5 contracts；lot_sz=1 → floor 5
        # adjusted_spot = 5 × 0.01 = 0.05 BTC（无调整）
        spot, perp = carry_position_size(
            account_equity_usdt=10000,
            max_position_pct=0.30,
            spot_price=60000,
            perp_ct_val=0.01,
            perp_lot_sz=1,
        )
        assert perp == 5.0
        assert spot == 0.05  # 等 USDT notional：5 * 0.01 * 60000 = 3000

    def test_lot_size_rounding(self) -> None:
        # 算出 5.7 contracts → floor=5；spot 跟着调成 5*ct_val
        spot, perp = carry_position_size(
            account_equity_usdt=11400,
            max_position_pct=0.30,
            spot_price=60000,
            perp_ct_val=0.01,
            perp_lot_sz=1,
        )
        assert perp == 5.0
        assert spot == 0.05

    def test_zero_when_invalid(self) -> None:
        assert carry_position_size(account_equity_usdt=0, max_position_pct=0.3,
                                   spot_price=60000, perp_ct_val=0.01,
                                   perp_lot_sz=1) == (0.0, 0.0)
        assert carry_position_size(account_equity_usdt=10000, max_position_pct=0.3,
                                   spot_price=0, perp_ct_val=0.01,
                                   perp_lot_sz=1) == (0.0, 0.0)
