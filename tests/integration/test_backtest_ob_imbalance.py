"""End-to-end integration: orderbook parquet ↔ replay stream ↔ strategy hook.

Bridges unit tests (mock-based) and the live CLI (which goes through NT engine).
Verifies the disk → read_orderbook_parquet() → OrderbookReplayStream → process_orderbook()
pipeline used by scripts/backtest.py --strategy ob_imbalance.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def synthetic_orderbook_frames():
    """200 synthetic books5 frames at 1Hz spanning ~3.3 minutes.

    Mid oscillates as a sine wave; spread varies slightly. Used for replay
    smoke tests where we just need realistic-shaped data.
    """
    from okx_trade.backtest.orderbook_data import OrderbookFrame
    frames = []
    base_ts = 1_700_000_000_000  # Nov 14 2023 UTC
    for i in range(200):
        mid = 60_000 + 50 * math.sin(i / 20)
        spread = 0.5
        frames.append(OrderbookFrame(
            inst_id="BTC-USDT-SWAP",
            ts_ms=base_ts + i * 1_000,
            bids=[[mid - spread - 0.5 * j, 1.0 + (j % 3)] for j in range(5)],
            asks=[[mid + spread + 0.5 * j, 1.0 + (j % 3)] for j in range(5)],
        ))
    return frames


def test_orderbook_parquet_roundtrips_and_drains_through_replay_stream(
    tmp_path, synthetic_orderbook_frames,
):
    """Write→read→replay roundtrip for 200 synthetic frames."""
    from okx_trade.backtest.orderbook_data import (
        OrderbookReplayStream, read_orderbook_parquet, write_orderbook_parquet,
    )
    written = write_orderbook_parquet(synthetic_orderbook_frames, catalog_path=tmp_path)
    assert len(written) >= 1
    assert (tmp_path / "books5" / "BTC-USDT-SWAP").exists()

    loaded = read_orderbook_parquet("BTC-USDT-SWAP", catalog_path=tmp_path)
    assert len(loaded) == 200
    assert loaded[0].ts_ms == synthetic_orderbook_frames[0].ts_ms
    assert loaded[-1].ts_ms == synthetic_orderbook_frames[-1].ts_ms

    stream = OrderbookReplayStream(loaded)
    assert len(stream) == 200
    # Drain everything up to past the last frame's timestamp
    drained = list(stream.drain_until(synthetic_orderbook_frames[-1].ts_ms + 1))
    assert len(drained) == 200
    assert stream.remaining() == 0


def test_orderbook_replay_fires_process_orderbook_on_real_strategy(
    tmp_path, synthetic_orderbook_frames,
):
    """The full pipeline up to the strategy hook (no NT engine instantiation)."""
    pytest.importorskip("nautilus_trader")
    from okx_trade.backtest.orderbook_data import (
        OrderbookReplayStream, read_orderbook_parquet, write_orderbook_parquet,
    )
    from okx_trade.backtest.orderbook_runner import _replay_books_for_bar
    from unittest.mock import MagicMock

    write_orderbook_parquet(synthetic_orderbook_frames, catalog_path=tmp_path)
    loaded = read_orderbook_parquet("BTC-USDT-SWAP", catalog_path=tmp_path)
    stream = OrderbookReplayStream(loaded)

    # Stand in for OBImbalanceStrategy — we just need a mock that records calls
    strategy = MagicMock()
    # Simulate a bar at frame 100 — should drain first 100 frames
    bar_ts_at_frame_100 = synthetic_orderbook_frames[100].ts_ms
    _replay_books_for_bar(strategy, stream, bar_ts_ms_exclusive=bar_ts_at_frame_100)
    assert strategy.process_orderbook.call_count == 100
    # The frames passed should be OrderBook instances (not raw OrderbookFrame)
    first_book_arg = strategy.process_orderbook.call_args_list[0].args[0]
    assert first_book_arg.inst_id == "BTC-USDT-SWAP"
    assert first_book_arg.ts == synthetic_orderbook_frames[0].ts_ms
