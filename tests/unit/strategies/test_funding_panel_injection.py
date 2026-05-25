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


def test_funding_cross_section_accepts_multi_inst_panels():
    pytest.importorskip("nautilus_trader")
    from okx_trade.strategies.funding_cross_section import (
        FundingXSStrategy, FundingXSConfig,
    )
    # NB: the strategy class is FundingXSConfig/FundingXSStrategy (not
    # FundingCrossSectionConfig/FundingCrossSectionStrategy); adapted from spec.
    cfg = FundingXSConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX"],
        beta_bar_type_template="{inst}-1-DAY-LAST-EXTERNAL",
    )
    strat = FundingXSStrategy(cfg)
    panels = {
        "BTC-USDT-SWAP": FundingPanel(
            inst_id="BTC-USDT-SWAP",
            ts_ms=[1_700_000_000_000], rates=[0.0001],
        ),
        "ETH-USDT-SWAP": FundingPanel(
            inst_id="ETH-USDT-SWAP",
            ts_ms=[1_700_000_000_000], rates=[0.0002],
        ),
    }
    strat.feed_funding_panel(panels)
    assert strat._funding_source_kind == "panel"
    assert set(strat._funding_panels) == set(panels)
