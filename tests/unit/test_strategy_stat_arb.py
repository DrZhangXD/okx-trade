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

    def test_cooldown_after_exit_default_sane(self) -> None:
        """2026-05-25 12:38–15:27 三小时打 4394 笔，平均 -$0.32/笔 = -$1,412 USDT。

        z_entry=2.0 / z_exit=0.5 在低波动期会反复触发 enter→exit→enter，
        每个 round-trip 4 legs × taker fee 把 1.5σ 毛收益吃光。

        必须有 cooldown_sec_after_exit gate（默认 ≥ 1 小时），平仓后强制等
        一段时间再判信号；和 1H bar 节奏对齐。
        """
        cfg = StatArbConfig(
            pair_left="BTC-USDT-SWAP.OKX",
            pair_right="ETH-USDT-SWAP.OKX",
        )
        # 默认必须 ≥ 1 小时（3600s）。3 小时 4394 笔事件平均 24 笔/分钟，
        # 一根 1H bar 内最多 1 个 round-trip 已经足够稀疏。
        assert cfg.cooldown_sec_after_exit >= 3600, (
            f"cooldown {cfg.cooldown_sec_after_exit}s too short — "
            f"5/25 churn ran 4394 trades / 3h"
        )

    def test_cooldown_after_exit_configurable(self) -> None:
        """可以在 yaml 里覆盖（回测想要更激进时降到 60s）。"""
        cfg = StatArbConfig(
            pair_left="BTC-USDT-SWAP.OKX",
            pair_right="ETH-USDT-SWAP.OKX",
            cooldown_sec_after_exit=60,
        )
        assert cfg.cooldown_sec_after_exit == 60
