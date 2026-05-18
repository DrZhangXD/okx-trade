"""option_vol_selling 单测：RV 计算 + ATM 选 strike + config 默认。"""
from __future__ import annotations

import math

import pytest

from okx_trade.strategies.option_vol_selling import (
    OptionVolConfig,
    realized_vol_annualized,
    select_atm_strike,
)


class TestRealizedVol:
    def test_zero_for_constant_series(self) -> None:
        # 价格不变 → log returns 全 0 → vol = 0
        closes = [100.0] * 30
        assert realized_vol_annualized(closes) == 0.0

    def test_positive_for_volatile_series(self) -> None:
        # 模拟 1% 日波动率 → 年化 ≈ 19%（σ × √365）
        import random
        rng = random.Random(42)
        closes = [100.0]
        for _ in range(60):
            closes.append(closes[-1] * (1 + rng.gauss(0, 0.01)))
        rv = realized_vol_annualized(closes, periods_per_year=365)
        # 应该接近 0.01 × √365 ≈ 0.191
        assert 0.10 < rv < 0.30

    def test_insufficient_history(self) -> None:
        assert realized_vol_annualized([]) == 0.0
        assert realized_vol_annualized([100.0]) == 0.0
        assert realized_vol_annualized([100.0, 101.0]) == 0.0


class TestSelectATMStrike:
    def test_picks_nearest_strike(self) -> None:
        strikes = [90000.0, 95000.0, 100000.0, 105000.0]
        assert select_atm_strike(strikes, spot=99000.0) == 100000.0
        assert select_atm_strike(strikes, spot=92000.0) == 90000.0

    def test_empty_returns_none(self) -> None:
        assert select_atm_strike([], spot=100000.0) is None

    def test_zero_spot_returns_none(self) -> None:
        assert select_atm_strike([100.0], spot=0.0) is None

    def test_negative_spot_returns_none(self) -> None:
        assert select_atm_strike([100.0], spot=-1.0) is None


class TestOptionVolConfig:
    def test_defaults(self) -> None:
        cfg = OptionVolConfig(
            underlying="BTC-USD",
            perp_instrument_id="BTC-USDT-SWAP.OKX",
            perp_bar_type="BTC-USDT-SWAP.OKX-1-HOUR-LAST-EXTERNAL",
        )
        assert cfg.tenor_target_days == 7
        assert cfg.iv_rv_ratio_min == 1.20
        assert cfg.max_notional_per_leg_usdt == 1000.0
        assert cfg.delta_hedge_threshold == 0.05
        assert cfg.close_days_before_expiry == 1
        assert cfg.stop_distance_strike_pct == 0.05
