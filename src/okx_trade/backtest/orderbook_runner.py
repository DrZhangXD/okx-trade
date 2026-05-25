"""Orderbook-replay backtest helpers.

This module provides primitives for interleaving captured orderbook frames
with bar callbacks. NT's BacktestNode does not natively support custom
event types beyond Bar/Trade/Quote, so we expose two patterns:

1. ``_replay_books_for_bar(strategy, stream, bar_ts_ms_exclusive)`` -- core
   primitive for any custom runner. Fires ``strategy.process_orderbook(book)``
   for every frame with ts_ms < bar_ts_ms_exclusive, advancing the stream cursor.

2. ``run_orderbook_replay_backtest(...)`` -- full bar-driven backtest using
   the existing NT BacktestNode plus a post-build monkey-patch on the
   strategy's ``on_bar`` to fire books before the original handler.
"""
from __future__ import annotations

from decimal import Decimal

from ..models.market import OrderBook, OrderBookLevel
from .orderbook_data import OrderbookFrame, OrderbookReplayStream


def _frame_to_orderbook(frame: OrderbookFrame) -> OrderBook:
    """Convert a parquet OrderbookFrame back to the strategy-facing OrderBook.

    OrderBook fields: inst_id (str), bids/asks (list[OrderBookLevel]), ts (int ms).
    OrderBookLevel fields: price (Decimal), size (Decimal), num_orders (int, default 0).
    """
    return OrderBook(
        inst_id=frame.inst_id,
        ts=frame.ts_ms,
        bids=[
            OrderBookLevel(price=Decimal(str(p)), size=Decimal(str(s)))
            for p, s in frame.bids
        ],
        asks=[
            OrderBookLevel(price=Decimal(str(p)), size=Decimal(str(s)))
            for p, s in frame.asks
        ],
    )


def _replay_books_for_bar(
    strategy,
    stream: OrderbookReplayStream,
    *,
    bar_ts_ms_exclusive: int,
) -> None:
    """Fire ``process_orderbook(book)`` for every frame with ts_ms < ``bar_ts_ms_exclusive``.

    Advances the stream cursor so subsequent calls to this function within the
    same backtest run do not replay already-consumed frames.

    Args:
        strategy: Any object exposing a ``process_orderbook(book: OrderBook)`` method.
        stream: The replay stream to drain from.
        bar_ts_ms_exclusive: Upper bound (exclusive) on frame timestamps to fire.
    """
    for frame in stream.drain_until(bar_ts_ms_exclusive):
        strategy.process_orderbook(_frame_to_orderbook(frame))
