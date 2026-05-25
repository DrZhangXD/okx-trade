"""Tests for orderbook-replay backtest runner."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from okx_trade.backtest.orderbook_data import OrderbookFrame, OrderbookReplayStream


def test_replay_runner_fires_process_orderbook_for_frames_within_bar_window():
    from okx_trade.backtest.orderbook_runner import _replay_books_for_bar

    frames = [
        OrderbookFrame("X", 1_000, [[1.0, 1.0]], [[2.0, 1.0]]),
        OrderbookFrame("X", 1_500, [[1.1, 1.0]], [[2.1, 1.0]]),
        OrderbookFrame("X", 2_500, [[1.2, 1.0]], [[2.2, 1.0]]),
    ]
    stream = OrderbookReplayStream(frames)
    strategy = MagicMock()
    # Drain books with ts_ms < 2000
    _replay_books_for_bar(strategy, stream, bar_ts_ms_exclusive=2_000)
    assert strategy.process_orderbook.call_count == 2
