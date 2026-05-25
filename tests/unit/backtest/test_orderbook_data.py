"""Tests for orderbook snapshot capture + replay infrastructure."""
from __future__ import annotations

import pytest

from okx_trade.backtest.orderbook_data import (
    OrderbookFrame, write_orderbook_parquet, read_orderbook_parquet,
)


def test_orderbook_frame_parquet_roundtrip(tmp_path):
    frames = [
        OrderbookFrame(
            inst_id="BTC-USDT-SWAP", ts_ms=1_700_000_000_000,
            bids=[[60000.0, 1.5], [59999.5, 2.0]], asks=[[60000.5, 1.2], [60001.0, 0.8]],
        ),
        OrderbookFrame(
            inst_id="BTC-USDT-SWAP", ts_ms=1_700_000_001_000,
            bids=[[60001.0, 1.0]], asks=[[60001.5, 1.1]],
        ),
    ]
    paths = write_orderbook_parquet(frames, catalog_path=tmp_path)
    assert all(p.exists() for p in paths)

    loaded = read_orderbook_parquet("BTC-USDT-SWAP", catalog_path=tmp_path)
    assert len(loaded) == 2
    assert loaded[0].ts_ms == frames[0].ts_ms
    assert loaded[0].bids == frames[0].bids
    assert loaded[1].asks == frames[1].asks
