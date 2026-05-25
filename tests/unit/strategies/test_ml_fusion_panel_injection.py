"""Tests for MLFusionStrategy.feed_funding_panel — mirrors funding_xs Plan 1 pattern."""
from __future__ import annotations

import pytest


def test_feed_funding_panel_switches_source_kind():
    pytest.importorskip("nautilus_trader")
    pytest.importorskip("xgboost")
    from okx_trade.backtest.funding_data import FundingPanel
    from okx_trade.strategies.ml_fusion import MLFusionConfig, MLFusionStrategy

    cfg = MLFusionConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX"],
    )
    strat = MLFusionStrategy(cfg)
    assert strat._funding_source_kind == "rest"
    assert strat._funding_panels == {}

    panels = {
        "BTC-USDT-SWAP": FundingPanel(
            inst_id="BTC-USDT-SWAP",
            ts_ms=[1_700_000_000_000, 1_700_028_800_000],
            rates=[0.0001, 0.0002],
        ),
    }
    strat.feed_funding_panel(panels)
    assert strat._funding_source_kind == "panel"
    assert "BTC-USDT-SWAP" in strat._funding_panels


def test_funding_for_inst_returns_current_and_history():
    pytest.importorskip("nautilus_trader")
    pytest.importorskip("xgboost")
    from okx_trade.backtest.funding_data import FundingPanel
    from okx_trade.strategies.ml_fusion import MLFusionConfig, MLFusionStrategy

    base_ts = 1_700_000_000_000
    panel = FundingPanel(
        inst_id="BTC-USDT-SWAP",
        ts_ms=[base_ts + i * 8 * 3_600_000 for i in range(100)],
        rates=[0.0001 + 0.00001 * i for i in range(100)],
    )
    cfg = MLFusionConfig(instrument_ids=["BTC-USDT-SWAP.OKX"])
    strat = MLFusionStrategy(cfg)
    strat.feed_funding_panel({"BTC-USDT-SWAP": panel})
    # Pretend the strategy has seen a bar at sample 50's timestamp
    strat._last_bar_ts_ms = panel.ts_ms[50]

    current, history = strat._funding_for_inst("BTC-USDT-SWAP")
    assert current == panel.rates[50]
    # history is the 90 prior samples (but only 50 exist), excluding current
    assert history == panel.rates[:50]


def test_funding_for_inst_returns_defaults_in_rest_mode():
    pytest.importorskip("nautilus_trader")
    pytest.importorskip("xgboost")
    from okx_trade.strategies.ml_fusion import MLFusionConfig, MLFusionStrategy

    cfg = MLFusionConfig(instrument_ids=["BTC-USDT-SWAP.OKX"])
    strat = MLFusionStrategy(cfg)
    # default _funding_source_kind == "rest"
    current, history = strat._funding_for_inst("BTC-USDT-SWAP")
    assert current == 0.0
    assert history is None


def test_funding_for_inst_returns_defaults_when_panel_missing():
    pytest.importorskip("nautilus_trader")
    pytest.importorskip("xgboost")
    from okx_trade.strategies.ml_fusion import MLFusionConfig, MLFusionStrategy

    cfg = MLFusionConfig(instrument_ids=["BTC-USDT-SWAP.OKX"])
    strat = MLFusionStrategy(cfg)
    strat.feed_funding_panel({})  # panel mode but no panels
    current, history = strat._funding_for_inst("BTC-USDT-SWAP")
    assert current == 0.0
    assert history is None
