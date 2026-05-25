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
