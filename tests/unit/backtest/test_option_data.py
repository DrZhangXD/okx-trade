"""Tests for option summary capture/replay parquet."""
from __future__ import annotations

import pytest

from okx_trade.backtest.option_data import (
    OptionSummarySnapshot, write_option_parquet, read_option_parquet,
)


def test_option_summary_parquet_roundtrip(tmp_path):
    snaps = [
        OptionSummarySnapshot(
            ts_ms=1_700_000_000_000, inst_id="BTC-USD-251226-100000-P",
            mark_price=1234.5, mark_iv=0.55, delta=-0.4, gamma=0.0001,
            vega=120.0, theta=-15.0, underlying="BTC-USD",
            exp_time_ms=1_766_793_600_000, strike=100_000.0, option_type="P",
        ),
        OptionSummarySnapshot(
            ts_ms=1_700_000_060_000, inst_id="BTC-USD-251226-100000-P",
            mark_price=1240.0, mark_iv=0.56, delta=-0.41, gamma=0.0001,
            vega=121.0, theta=-15.1, underlying="BTC-USD",
            exp_time_ms=1_766_793_600_000, strike=100_000.0, option_type="P",
        ),
    ]
    write_option_parquet(snaps, catalog_path=tmp_path)
    loaded = read_option_parquet("BTC-USD", catalog_path=tmp_path)
    assert len(loaded) == 2
    assert loaded[0].mark_iv == 0.55


def test_option_panel_returns_snapshots_at_or_before_ts():
    from okx_trade.backtest.option_data import OptionSummaryPanel

    snaps = [
        OptionSummarySnapshot(
            ts_ms=1_000, inst_id="A", mark_price=1, mark_iv=0.5,
            delta=0, gamma=0, vega=0, theta=0, underlying="BTC-USD",
            exp_time_ms=2_000_000, strike=100, option_type="C",
        ),
        OptionSummarySnapshot(
            ts_ms=2_000, inst_id="A", mark_price=1.1, mark_iv=0.51,
            delta=0, gamma=0, vega=0, theta=0, underlying="BTC-USD",
            exp_time_ms=2_000_000, strike=100, option_type="C",
        ),
        OptionSummarySnapshot(
            ts_ms=1_000, inst_id="B", mark_price=2, mark_iv=0.5,
            delta=0, gamma=0, vega=0, theta=0, underlying="BTC-USD",
            exp_time_ms=2_000_000, strike=110, option_type="C",
        ),
    ]
    panel = OptionSummaryPanel(snaps)
    snap_a = panel.snapshot_at_or_before("A", 1_500)
    assert snap_a is not None and snap_a.mark_price == 1.0
    snap_a_later = panel.snapshot_at_or_before("A", 2_500)
    assert snap_a_later.mark_price == 1.1
    assert panel.snapshot_at_or_before("A", 500) is None
    chain = panel.chain_at_or_before(1_500)
    assert {s.inst_id for s in chain} == {"A", "B"}
