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


def test_funding_skew_momentum_panel_preloads_history_window():
    pytest.importorskip("nautilus_trader")
    from okx_trade.strategies.funding_skew_momentum import (
        FundingSkewStrategy as FundingSkewMomentumStrategy,
        FundingSkewConfig as FundingSkewMomentumConfig,
    )
    # NB: actual class names are FundingSkewConfig / FundingSkewStrategy (single-instrument).
    # instrument_id (singular) + bar_type (not bar_type_template).
    cfg = FundingSkewMomentumConfig(
        instrument_id="BTC-USDT-SWAP.OKX",
        bar_type="BTC-USDT-SWAP.OKX-1-HOUR-LAST-EXTERNAL",
    )
    strat = FundingSkewMomentumStrategy(cfg)
    # 100 funding samples — strategy needs 90 minimum for z-score
    ts_ms = [1_700_000_000_000 + i * 8 * 3_600_000 for i in range(100)]
    rates = [0.0001 + (i % 5) * 0.00001 for i in range(100)]
    panel = FundingPanel(inst_id="BTC-USDT-SWAP", ts_ms=ts_ms, rates=rates)
    strat.feed_funding_panel({"BTC-USDT-SWAP": panel})
    # The strategy's internal history deque should be pre-populated from the panel
    # _funding_history is a plain deque (single-instrument strategy), not a dict
    history_deque = strat._funding_history
    assert history_deque is not None
    assert len(history_deque) == 90  # capped at maxlen=90
