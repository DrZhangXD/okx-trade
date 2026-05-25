"""End-to-end integration: option summary parquet ↔ panel ↔ strategy hook.

Bridges unit tests (mock-based) and the live CLI. Verifies the
disk → read_option_parquet() → OptionSummaryPanel → feed_option_snapshots()
pipeline used by scripts/backtest.py --strategy option_vol_selling.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def synthetic_option_snapshots():
    """50 instruments × 24 polls = 1200 snapshots."""
    from okx_trade.backtest.option_data import OptionSummarySnapshot
    base_ts = 1_700_000_000_000
    exp_ts = 1_766_793_600_000  # ~Dec 2025
    snaps = []
    for poll in range(24):  # hourly polls over 24h
        ts = base_ts + poll * 3_600_000
        for strike_idx in range(25):
            strike = 50_000 + strike_idx * 2_000
            for opt_type in ("C", "P"):
                snaps.append(OptionSummarySnapshot(
                    ts_ms=ts,
                    inst_id=f"BTC-USD-251226-{strike}-{opt_type}",
                    mark_price=1000 + strike_idx * 50,
                    mark_iv=0.50 + 0.001 * poll,  # IV drifts up over time
                    delta=0.5 - 0.02 * strike_idx if opt_type == "C" else -0.5 + 0.02 * strike_idx,
                    gamma=0.0001, vega=120, theta=-15,
                    underlying="BTC-USD",
                    exp_time_ms=exp_ts, strike=strike, option_type=opt_type,
                ))
    return snaps


def test_option_parquet_roundtrips_and_panel_lookup(tmp_path, synthetic_option_snapshots):
    """Roundtrip 1200 snapshots and verify panel.chain_at_or_before works."""
    from okx_trade.backtest.option_data import (
        OptionSummaryPanel, read_option_parquet, write_option_parquet,
    )
    written = write_option_parquet(synthetic_option_snapshots, catalog_path=tmp_path)
    assert len(written) >= 1
    assert (tmp_path / "option_summary" / "BTC-USD").exists()

    loaded = read_option_parquet("BTC-USD", catalog_path=tmp_path)
    assert len(loaded) == 1200

    panel = OptionSummaryPanel(loaded)
    # Look up chain at the 12th poll (mid-data)
    mid_ts = synthetic_option_snapshots[0].ts_ms + 12 * 3_600_000
    chain = panel.chain_at_or_before(mid_ts)
    # 50 instruments × all returning snapshot
    assert len(chain) == 50
    # IV at mid_ts should be 0.50 + 0.001 * 12 = 0.512
    sample = chain[0]
    assert abs(sample.mark_iv - 0.512) < 0.001


def test_option_panel_feeds_real_strategy(tmp_path, synthetic_option_snapshots):
    """End-to-end: parquet → panel → strategy hook."""
    pytest.importorskip("nautilus_trader")
    from okx_trade.backtest.option_data import (
        OptionSummaryPanel, read_option_parquet, write_option_parquet,
    )
    from okx_trade.strategies.option_vol_selling import OptionVolConfig, OptionVolStrategy

    write_option_parquet(synthetic_option_snapshots, catalog_path=tmp_path)
    loaded = read_option_parquet("BTC-USD", catalog_path=tmp_path)
    panel = OptionSummaryPanel(loaded)

    cfg = OptionVolConfig(
        underlying="BTC-USD",
        perp_instrument_id="BTC-USDT-SWAP.OKX",
        perp_bar_type="BTC-USDT-SWAP.OKX-1-HOUR-LAST-EXTERNAL",
        option_summary_parquet_path=str(tmp_path),
    )
    strat = OptionVolStrategy(cfg)
    strat.feed_option_snapshots(panel)
    assert strat._option_source_kind == "panel"
    assert strat._option_panel is panel
