"""End-to-end integration: FundingPanel parquet <-> strategy on_start auto-load.

These tests bridge unit tests (which mock the REST client and Strategy lifecycle)
and the live backtest CLI (which goes through the full NT engine). They verify
the actual disk -> read_funding_parquet() -> feed_funding_panel() pipeline used
by all 4 funding-aware strategies.

Run via:
    pytest tests/integration/test_backtest_funding_strategies.py -v -m integration

The `-m integration` filter is optional; these tests don't hit the network or
require credentials, but they take longer than the unit suite (real NT objects,
file I/O).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def synthetic_funding_panel():
    """100 funding samples spanning ~33 days, 8h cadence, varying rates."""
    from okx_trade.backtest.funding_data import FundingPanel
    base_ts = 1_700_000_000_000  # Nov 14 2023 UTC
    ts_ms = [base_ts + i * 8 * 3_600_000 for i in range(100)]
    # Rates oscillate between -0.0001 and +0.0003 with some drift
    rates = [0.0001 + 0.0002 * (i % 7 - 3) / 3 for i in range(100)]
    return FundingPanel(inst_id="BTC-USDT-SWAP", ts_ms=ts_ms, rates=rates)


def test_panel_roundtrips_through_parquet_and_funding_carry_strategy(
    tmp_path, synthetic_funding_panel,
):
    """Write panel -> parquet -> read it back via strategy.on_start chain.

    This is the integration path that the backtest CLI follows:
        prepare_funding_panel() writes parquet
        ImportableStrategyConfig(funding_panel_parquet_path=catalog)
        on_start() reads parquet via read_funding_parquet() + feed_funding_panel()
    """
    pytest.importorskip("nautilus_trader")
    from okx_trade.backtest.funding_data import (
        FundingPanel, read_funding_parquet, write_funding_parquet,
    )
    from okx_trade.strategies.funding_carry import FundingCarryConfig, FundingCarryStrategy

    # 1) Write parquet (mirrors prepare_funding_panel cache-miss path)
    written = write_funding_parquet(synthetic_funding_panel, catalog_path=tmp_path)
    assert len(written) >= 1
    expected_dir = tmp_path / "funding" / "BTC-USDT-SWAP"
    assert expected_dir.exists()
    assert any(p.suffix == ".parquet" for p in expected_dir.iterdir())

    # 2) Read it back via the function the strategy will call
    loaded = read_funding_parquet("BTC-USDT-SWAP", catalog_path=tmp_path)
    assert loaded.ts_ms == synthetic_funding_panel.ts_ms
    assert loaded.rates == synthetic_funding_panel.rates

    # 3) Verify the strategy config accepts the path and the hook works directly
    cfg = FundingCarryConfig(
        spot_instrument_id="BTC-USDT.OKX",
        perp_instrument_id="BTC-USDT-SWAP.OKX",
        spot_bar_type="BTC-USDT.OKX-1-HOUR-LAST-EXTERNAL",
        funding_panel_parquet_path=str(tmp_path),
    )
    assert cfg.funding_panel_parquet_path == str(tmp_path)

    strat = FundingCarryStrategy(cfg)
    # Simulate what on_start does -- read parquet and feed
    strat.feed_funding_panel(loaded)
    assert strat._funding_source_kind == "panel"
    assert strat._funding_panel is loaded
    # And the panel works for lookup at a synthesized bar timestamp
    mid_ts = synthetic_funding_panel.ts_ms[50]
    assert strat._funding_panel.rate_at_or_before(mid_ts) == synthetic_funding_panel.rates[50]


def test_multi_inst_panel_roundtrip_for_funding_cross_section(tmp_path):
    """Same pipeline for the multi-inst FundingXS strategy."""
    pytest.importorskip("nautilus_trader")
    from okx_trade.backtest.funding_data import (
        FundingPanel, read_funding_parquet, write_funding_parquet,
    )
    from okx_trade.strategies.funding_cross_section import (
        FundingXSConfig, FundingXSStrategy,
    )

    panels_by_inst = {
        "BTC-USDT-SWAP": FundingPanel(
            inst_id="BTC-USDT-SWAP",
            ts_ms=[1_700_000_000_000, 1_700_028_800_000],
            rates=[0.0001, 0.00015],
        ),
        "ETH-USDT-SWAP": FundingPanel(
            inst_id="ETH-USDT-SWAP",
            ts_ms=[1_700_000_000_000, 1_700_028_800_000],
            rates=[0.0002, 0.00025],
        ),
    }
    for panel in panels_by_inst.values():
        write_funding_parquet(panel, catalog_path=tmp_path)

    loaded_btc = read_funding_parquet("BTC-USDT-SWAP", catalog_path=tmp_path)
    loaded_eth = read_funding_parquet("ETH-USDT-SWAP", catalog_path=tmp_path)
    assert loaded_btc.rates == [0.0001, 0.00015]
    assert loaded_eth.rates == [0.0002, 0.00025]

    cfg = FundingXSConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX"],
        beta_bar_type_template="{inst}-1-DAY-LAST-EXTERNAL",
        funding_panel_parquet_path=str(tmp_path),
    )
    strat = FundingXSStrategy(cfg)
    strat.feed_funding_panel({
        "BTC-USDT-SWAP": loaded_btc, "ETH-USDT-SWAP": loaded_eth,
    })
    assert strat._funding_source_kind == "panel"
    assert set(strat._funding_panels) == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
