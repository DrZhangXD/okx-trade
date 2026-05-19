"""Tests for FactorPortfolioStrategy pure synthesis."""
from __future__ import annotations

import importlib

import numpy as np
import pytest

from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, register_factor
from okx_trade.strategies.factor_portfolio import (
    FactorWeight,
    cross_section_zscore,
    select_top_bot,
    synthesize_score,
)


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    import okx_trade.research.factors.momentum as m
    importlib.reload(m)  # bring back built-ins for these tests
    yield
    clear_registry()


def _panel(close: np.ndarray) -> FactorPanel:
    T, N = close.shape
    return FactorPanel(
        inst_ids=tuple(f"I{i}" for i in range(N)),
        timestamps_ms=tuple(range(T)),
        close=close, volume_usdt=np.ones((T, N)),
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_cross_section_zscore_zero_mean_unit_var() -> None:
    vals = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    z = cross_section_zscore(vals)
    assert z.mean() == pytest.approx(0.0)
    assert z.std(ddof=0) == pytest.approx(1.0)


def test_cross_section_zscore_returns_nan_when_no_variance() -> None:
    vals = np.array([3.0, 3.0, 3.0])
    z = cross_section_zscore(vals)
    assert np.all(np.isnan(z))


def test_synthesize_score_combines_factors_by_weight() -> None:
    # Create close prices with 25 bars, 3 instruments, different momentum profiles
    closes = np.zeros((25, 3))
    closes[:, 0] = np.linspace(100, 124, 25)  # uptrend
    closes[:, 1] = np.linspace(100, 110, 25)  # slower uptrend
    closes[:, 2] = np.linspace(100, 95, 25)   # downtrend
    panel = _panel(closes)
    weights = [
        FactorWeight(id="momentum_1d", weight=1.0),
    ]
    score, missing = synthesize_score(panel, weights)
    assert score.shape == (panel.n,)
    assert missing == []
    # Verify that instruments with better momentum get higher scores
    assert score[0] > score[2]  # uptrend > downtrend


def test_synthesize_score_skips_unregistered_with_warning() -> None:
    panel = _panel(np.ones((30, 3)) * 100.0)
    weights = [FactorWeight(id="nonexistent", weight=1.0)]
    score, missing = synthesize_score(panel, weights)
    assert "nonexistent" in missing
    # Score is all-NaN when all weights are missing
    assert np.all(np.isnan(score))


def test_select_top_bot_returns_indices_by_score() -> None:
    score = np.array([0.5, -1.2, 0.8, -0.3, 1.5])
    longs, shorts = select_top_bot(score, top_k_long=2, top_k_short=2)
    assert longs == [4, 2]   # 1.5, 0.8 — descending
    assert shorts == [1, 3]  # -1.2, -0.3 — ascending


def test_select_top_bot_skips_nan_scores() -> None:
    score = np.array([0.5, np.nan, 0.8, -0.3, 1.5])
    longs, shorts = select_top_bot(score, top_k_long=3, top_k_short=3)
    assert 1 not in longs and 1 not in shorts


nt = pytest.importorskip("nautilus_trader")


def test_factor_portfolio_config_loads_from_yaml() -> None:
    """Verify the dataclass-mode StrategyConfig accepts our yaml shape."""
    from okx_trade.strategies.factor_portfolio import FactorPortfolioConfig
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4,
        top_k_long=2, top_k_short=2,
        risk_pct=0.002,
        account_equity_usdt=10_000.0,
        factor_weights=[("momentum_7d", 0.5), ("funding_z_30d", 0.5)],
    )
    assert cfg.rebalance_hours == 4
    assert len(cfg.factor_weights) == 2


def test_factor_portfolio_strategy_initializes_without_error() -> None:
    from okx_trade.strategies.factor_portfolio import (
        FactorPortfolioConfig, FactorPortfolioStrategy,
    )
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4,
        top_k_long=1, top_k_short=1,
        risk_pct=0.002, account_equity_usdt=10_000.0,
        factor_weights=[("momentum_7d", 1.0)],
    )
    strategy = FactorPortfolioStrategy(cfg)
    assert strategy.config.rebalance_hours == 4


def test_derive_spot_inst_id_handles_variants() -> None:
    from okx_trade.strategies.factor_portfolio import _derive_spot_inst_id
    assert _derive_spot_inst_id("BTC-USDT-SWAP.OKX") == "BTC-USDT.OKX"
    assert _derive_spot_inst_id("ETH-USDT-SWAP") == "ETH-USDT"
    # Already spot — None
    assert _derive_spot_inst_id("BTC-USDT") is None
    # Delivery futures — None
    assert _derive_spot_inst_id("BTC-USD-260925") is None


def test_factor_portfolio_strategy_subscribes_spot_pairs_by_default() -> None:
    from okx_trade.strategies.factor_portfolio import (
        FactorPortfolioConfig, FactorPortfolioStrategy,
    )
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4,
        top_k_long=1, top_k_short=1,
        risk_pct=0.002, account_equity_usdt=10_000.0,
        factor_weights=[("basis_z_30d", 1.0)],
    )
    s = FactorPortfolioStrategy(cfg)
    # spot routing is set up on init
    assert s._spot_to_perp == {
        "BTC-USDT.OKX": "BTC-USDT-SWAP.OKX",
        "ETH-USDT.OKX": "ETH-USDT-SWAP.OKX",
    }
    assert len(s._spot_bar_types) == 2


def test_factor_portfolio_strategy_disable_spot_subscription() -> None:
    from okx_trade.strategies.factor_portfolio import (
        FactorPortfolioConfig, FactorPortfolioStrategy,
    )
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4,
        top_k_long=1, top_k_short=1,
        risk_pct=0.002, account_equity_usdt=10_000.0,
        factor_weights=[("momentum_1d_reversal", 1.0)],
        subscribe_spot_for_basis=False,
    )
    s = FactorPortfolioStrategy(cfg)
    assert s._spot_to_perp == {}
    assert s._spot_bar_types == {}
