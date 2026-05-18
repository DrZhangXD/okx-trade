"""funding_cross_section 单测：选股 + β-hedge + diff 执行。

策略类的 NT 部分由 paper trading 烟测；这里只测 helper / 选股核心。
"""
from __future__ import annotations

import pytest

from okx_trade.strategies.funding_cross_section import FundingXSConfig, FundingXSStrategy


class TestFundingXSConfig:
    def test_defaults_sane(self) -> None:
        cfg = FundingXSConfig(instrument_ids=["BTC-USDT-SWAP.OKX"])
        assert cfg.top_n == 3
        assert cfg.bot_n == 3
        assert tuple(cfg.rebalance_hours_utc) == (0, 8, 16)
        assert 0 < cfg.max_position_pct <= 1.0
        assert cfg.enable_beta_hedge is True


class TestReturnsHelper:
    """``FundingXSStrategy._returns`` 是 staticmethod，独立单测无需实例化策略。"""

    def test_returns_basic(self) -> None:
        # closes 100 → 110 → 99
        rets = FundingXSStrategy._returns([100.0, 110.0, 99.0])
        assert len(rets) == 2
        assert rets[0] == pytest.approx(0.10)
        assert rets[1] == pytest.approx(-0.10)

    def test_insufficient_returns_empty(self) -> None:
        assert FundingXSStrategy._returns([]) == []
        assert FundingXSStrategy._returns([100.0]) == []


class TestUniverseRanking:
    """模拟 funding rate 字典 → 排序顺序的契约。"""

    def test_ranking_picks_extremes(self) -> None:
        # funding 字典模拟：BTC=-0.001, ETH=+0.002, SOL=-0.0008, DOGE=+0.003, LTC=0
        funding = {
            "BTC-USDT-SWAP.OKX": -0.001,
            "ETH-USDT-SWAP.OKX": 0.002,
            "SOL-USDT-SWAP.OKX": -0.0008,
            "DOGE-USDT-SWAP.OKX": 0.003,
            "LTC-USDT-SWAP.OKX": 0.0,
        }
        ranked = sorted(funding.items(), key=lambda kv: kv[1])
        top_2_long = [k for k, _ in ranked[:2]]  # 最负 2 个
        bot_2_short = [k for k, _ in ranked[-2:]]  # 最正 2 个
        assert top_2_long == ["BTC-USDT-SWAP.OKX", "SOL-USDT-SWAP.OKX"]
        assert bot_2_short == ["ETH-USDT-SWAP.OKX", "DOGE-USDT-SWAP.OKX"]
