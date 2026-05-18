"""funding_skew_momentum 单测：纯函数 + ATR + 配置默认。"""
from __future__ import annotations

import pytest

from okx_trade.strategies.base import BarSnapshot
from okx_trade.strategies.funding_skew_momentum import (
    FundingSkewConfig,
    compute_atr,
    funding_skew_decision,
)


class TestFundingSkewDecision:
    def test_short_when_funding_too_positive(self) -> None:
        # history 均值 0.0001, std 0.00005，current 0.0003 → z = (0.0003-0.0001)/0.00005 = 4
        history = [0.0001] * 20 + [0.00005, 0.00015] * 10  # mean ≈ 0.0001
        decision = funding_skew_decision(0.0010, history, z_threshold=2.0)
        # 0.001 远高于 mean，z > 2 → short
        assert decision == "short"

    def test_long_when_funding_too_negative(self) -> None:
        history = [0.0001] * 20 + [0.00005, 0.00015] * 10
        decision = funding_skew_decision(-0.0010, history, z_threshold=2.0)
        assert decision == "long"

    def test_hold_when_within_band(self) -> None:
        history = [0.0001] * 20 + [0.00005, 0.00015] * 10
        # current = 0.00012，与 mean 接近 → 无信号
        assert funding_skew_decision(0.00012, history, z_threshold=2.0) is None

    def test_insufficient_history(self) -> None:
        assert funding_skew_decision(0.001, [0.0001, 0.0002], z_threshold=2.0) is None
        assert funding_skew_decision(0.001, [], z_threshold=2.0) is None


class TestComputeATR:
    def test_atr_basic(self) -> None:
        bars = [
            BarSnapshot(ts=i * 60000, open=100.0, high=101.0, low=99.0, close=100.0, volume=100.0)
            for i in range(20)
        ]
        atr = compute_atr(bars, period=14)
        # 每根 bar high-low = 2，TR = 2，ATR ≈ 2
        assert atr == pytest.approx(2.0, abs=0.1)

    def test_atr_insufficient_bars(self) -> None:
        bars = [BarSnapshot(ts=i, open=1, high=2, low=0.5, close=1, volume=1) for i in range(5)]
        assert compute_atr(bars, period=14) == 0.0

    def test_atr_with_gaps(self) -> None:
        # close 100 → open gap to 110 → TR = max(110-99, |110-100|, |99-100|) = 11
        bars = [
            BarSnapshot(ts=0, open=100, high=101, low=99, close=100, volume=1),
            BarSnapshot(ts=1, open=110, high=112, low=109, close=111, volume=1),
        ]
        # 只有 1 个 TR，period=14 不够
        assert compute_atr(bars, period=14) == 0.0


class TestFundingSkewConfig:
    def test_defaults(self) -> None:
        cfg = FundingSkewConfig(
            instrument_id="BTC-USDT-SWAP.OKX",
            bar_type="BTC-USDT-SWAP.OKX-1-HOUR-LAST-EXTERNAL",
        )
        assert cfg.z_threshold == 2.0
        assert cfg.history_periods == 90
        assert cfg.atr_sl_mult == 2.0
        assert cfg.tp_rr_ratio == 1.5
        assert cfg.hold_max_hours == 72
        assert cfg.risk_pct == 0.005
