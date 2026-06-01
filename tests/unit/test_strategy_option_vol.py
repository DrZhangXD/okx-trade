"""option_vol_selling 单测：RV 计算 + ATM 选 strike + config 默认。"""
from __future__ import annotations

import math

import pytest

from okx_trade.strategies.option_vol_selling import (
    OptionVolConfig,
    needs_rehedge,
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


class TestNeedsRehedge:
    """size-aware delta re-hedge 阈值(2026-06-01 修 bug:旧式 0.05×strike/10000
    ≈0.35 BTC,而 $1000/腿 跨式 net_delta 物理上最多 ~0.014 BTC → re-hedge 永不
    触发,对冲形同虚设)。新逻辑:net_delta 的 USD 价值 > 单腿名义 × threshold_frac。"""

    def test_below_threshold_no_rehedge(self) -> None:
        # 0.001 BTC × 70000 = $70 漂移 < 0.2 × $1000 = $200 → 不 re-hedge
        assert needs_rehedge(0.001, 70000.0, threshold_frac=0.2,
                             leg_notional_usd=1000.0) is False

    def test_above_threshold_triggers(self) -> None:
        # 0.005 BTC × 70000 = $350 > $200 → re-hedge
        assert needs_rehedge(0.005, 70000.0, threshold_frac=0.2,
                             leg_notional_usd=1000.0) is True

    def test_realistic_max_drift_now_triggers(self) -> None:
        # 回归:$1000/腿 跨式的最大物理漂移 ~0.014 BTC,旧阈值(0.35 BTC)永不触发;
        # 新逻辑下 0.014×70000=$980 > $200 → 正确触发
        assert needs_rehedge(0.014, 70000.0, threshold_frac=0.2,
                             leg_notional_usd=1000.0) is True

    def test_symmetric_for_negative_delta(self) -> None:
        assert needs_rehedge(-0.005, 70000.0, threshold_frac=0.2,
                             leg_notional_usd=1000.0) is True

    def test_no_spot_no_rehedge(self) -> None:
        # spot<=0(尚无价)→ 不盲目 re-hedge
        assert needs_rehedge(0.014, 0.0, threshold_frac=0.2,
                             leg_notional_usd=1000.0) is False


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
        assert cfg.delta_hedge_threshold == 0.2  # 2026-06-01: size-aware 语义新默认
        assert cfg.close_days_before_expiry == 1
        assert cfg.stop_distance_strike_pct == 0.05
