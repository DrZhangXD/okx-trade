"""stat_arb_pairs 单测：spread 计算 + config 默认。"""
from __future__ import annotations

import math

import pytest

from okx_trade.strategies.stat_arb_pairs import StatArbConfig, compute_spread


class TestComputeSpread:
    def test_basic(self) -> None:
        # log(s1) - β·log(s2)
        log_s1 = [4.0, 4.1, 4.2]
        log_s2 = [3.0, 3.1, 3.2]
        spread = compute_spread(log_s1, log_s2, hedge_ratio=1.0)
        # 对完美相关、β=1，spread 是常数 1.0（浮点误差容忍）
        assert spread == pytest.approx([1.0, 1.0, 1.0])

    def test_unequal_length(self) -> None:
        log_s1 = [1.0, 2.0, 3.0]
        log_s2 = [1.0, 2.0]
        spread = compute_spread(log_s1, log_s2, hedge_ratio=0.5)
        assert len(spread) == 2

    def test_beta_two(self) -> None:
        log_s1 = [4.0, 4.0]
        log_s2 = [2.0, 2.0]
        spread = compute_spread(log_s1, log_s2, hedge_ratio=2.0)
        # 4 - 2*2 = 0
        assert spread == [0.0, 0.0]


class TestStatArbConfig:
    def test_defaults(self) -> None:
        cfg = StatArbConfig(
            pair_left="BTC-USDT-SWAP.OKX",
            pair_right="ETH-USDT-SWAP.OKX",
        )
        assert cfg.coint_check_interval_h == 24
        assert cfg.coint_pvalue_threshold == 0.05
        assert cfg.spread_z_entry == 2.0
        assert cfg.spread_z_exit == 0.5
        assert cfg.spread_z_stop == 3.5
        assert cfg.risk_pct == 0.003
        assert cfg.lookback_bars == 1440
        assert cfg.warmup_via_rest is True

    def test_z_ordering(self) -> None:
        """exit < entry < stop 是必须的逻辑约束。"""
        cfg = StatArbConfig(
            pair_left="BTC-USDT-SWAP.OKX",
            pair_right="ETH-USDT-SWAP.OKX",
        )
        assert cfg.spread_z_exit < cfg.spread_z_entry < cfg.spread_z_stop
