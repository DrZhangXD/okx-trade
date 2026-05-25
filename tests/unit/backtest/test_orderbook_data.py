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


def test_orderbook_replay_stream_yields_frames_within_window(tmp_path):
    from okx_trade.backtest.orderbook_data import OrderbookReplayStream

    frames = [
        OrderbookFrame("BTC-USDT-SWAP", 1_000, [[1.0, 1.0]], [[2.0, 1.0]]),
        OrderbookFrame("BTC-USDT-SWAP", 2_500, [[1.1, 1.0]], [[2.1, 1.0]]),
        OrderbookFrame("BTC-USDT-SWAP", 3_999, [[1.2, 1.0]], [[2.2, 1.0]]),
        OrderbookFrame("BTC-USDT-SWAP", 5_000, [[1.3, 1.0]], [[2.3, 1.0]]),
    ]
    stream = OrderbookReplayStream(frames)
    # Drain frames with ts_ms in [-inf, 4000)
    in_window = list(stream.drain_until(4_000))
    assert [f.ts_ms for f in in_window] == [1_000, 2_500, 3_999]
    # Next drain continues from cursor
    assert [f.ts_ms for f in stream.drain_until(10_000)] == [5_000]
