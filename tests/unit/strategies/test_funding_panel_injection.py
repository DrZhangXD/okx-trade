"""Tests for funding-panel injection across funding-aware strategies."""
from __future__ import annotations

import pytest

from okx_trade.backtest.funding_data import FundingPanel


def test_funding_carry_strategy_accepts_panel_via_feed_method():
    pytest.importorskip("nautilus_trader")
    from okx_trade.strategies.funding_carry import FundingCarryStrategy, FundingCarryConfig

    cfg = FundingCarryConfig(
        spot_instrument_id="BTC-USDT.OKX",
        perp_instrument_id="BTC-USDT-SWAP.OKX",
        spot_bar_type="BTC-USDT.OKX-1-HOUR-LAST-EXTERNAL",
    )
    strat = FundingCarryStrategy(cfg)
    panel = FundingPanel(
        inst_id="BTC-USDT-SWAP",
        ts_ms=[1_700_000_000_000, 1_700_028_800_000],
        rates=[0.0001, 0.00015],
    )
    strat.feed_funding_panel(panel)
    # Internal lookup goes through panel, not REST
    assert strat._funding_source_kind == "panel"
    assert strat._funding_panel is panel
