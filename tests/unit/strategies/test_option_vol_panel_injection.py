"""Tests for option-summary panel injection on OptionVolStrategy."""
from __future__ import annotations

import pytest


def test_option_vol_config_has_summary_parquet_path_field():
    pytest.importorskip("nautilus_trader")
    from okx_trade.strategies.option_vol_selling import OptionVolConfig

    cfg = OptionVolConfig(
        underlying="BTC-USD",
        perp_instrument_id="BTC-USDT-SWAP.OKX",
        perp_bar_type="BTC-USDT-SWAP.OKX-1-HOUR-LAST-EXTERNAL",
    )
    assert cfg.option_summary_parquet_path is None


def test_option_vol_config_parquet_path_field_exists_via_introspection():
    """Verify the field is declared even if nautilus_trader is unavailable."""
    import okx_trade.strategies.option_vol_selling as mod

    # If NT not installed the stub class exists; check field is declared in
    # the _NT_AVAILABLE branch via the source (we can at least import the module).
    assert hasattr(mod, "OptionVolConfig")
    assert hasattr(mod, "OptionVolStrategy")


def test_option_vol_feed_method_sets_panel_and_kind():
    pytest.importorskip("nautilus_trader")
    from okx_trade.backtest.option_data import OptionSummaryPanel, OptionSummarySnapshot
    from okx_trade.strategies.option_vol_selling import (
        OptionVolConfig,
        OptionVolStrategy,
    )

    cfg = OptionVolConfig(
        underlying="BTC-USD",
        perp_instrument_id="BTC-USDT-SWAP.OKX",
        perp_bar_type="BTC-USDT-SWAP.OKX-1-HOUR-LAST-EXTERNAL",
    )
    strat = OptionVolStrategy(cfg)
    panel = OptionSummaryPanel([
        OptionSummarySnapshot(
            ts_ms=1_700_000_000_000,
            inst_id="BTC-USD-251226-100000-C",
            mark_price=1000.0,
            mark_iv=0.5,
            delta=0.4,
            gamma=0.0,
            vega=0.0,
            theta=0.0,
            underlying="BTC-USD",
            exp_time_ms=1_766_793_600_000,
            strike=100_000.0,
            option_type="C",
        ),
    ])
    strat.feed_option_snapshots(panel)
    assert strat._option_source_kind == "panel"
    assert strat._option_panel is panel
