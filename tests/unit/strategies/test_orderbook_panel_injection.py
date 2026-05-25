"""Tests for orderbook panel injection on ob_imbalance."""
from __future__ import annotations

import pytest


def test_obimbalance_config_has_orderbook_parquet_path_field():
    pytest.importorskip("nautilus_trader")
    from okx_trade.strategies.ob_imbalance import OBImbalanceConfig

    # Default: field exists and is None
    cfg = OBImbalanceConfig(
        instrument_id="BTC-USDT-SWAP.OKX",
        bar_type="BTC-USDT-SWAP.OKX-1-MINUTE-LAST-EXTERNAL",
    )
    assert cfg.orderbook_parquet_path is None

    # Can be set to a path string
    cfg2 = OBImbalanceConfig(
        instrument_id="BTC-USDT-SWAP.OKX",
        bar_type="BTC-USDT-SWAP.OKX-1-MINUTE-LAST-EXTERNAL",
        orderbook_parquet_path="/data",
    )
    assert cfg2.orderbook_parquet_path == "/data"


def test_obimbalance_config_subscribe_books5_can_be_false():
    """Critical: backtest must be able to disable the live WS subscription."""
    pytest.importorskip("nautilus_trader")
    from okx_trade.strategies.ob_imbalance import OBImbalanceConfig

    cfg = OBImbalanceConfig(
        instrument_id="BTC-USDT-SWAP.OKX",
        bar_type="BTC-USDT-SWAP.OKX-1-MINUTE-LAST-EXTERNAL",
        subscribe_books5=False,
        orderbook_parquet_path="/backtest/catalog",
    )
    assert cfg.subscribe_books5 is False
    assert cfg.orderbook_parquet_path == "/backtest/catalog"


def test_orderbook_replay_stream_drain_until():
    """OrderbookReplayStream drains frames correctly by timestamp."""
    from okx_trade.backtest.orderbook_data import OrderbookFrame, OrderbookReplayStream

    frames = [
        OrderbookFrame(inst_id="BTC-USDT-SWAP", ts_ms=1000,
                       bids=[[60000.0, 1.0]], asks=[[60001.0, 1.0]]),
        OrderbookFrame(inst_id="BTC-USDT-SWAP", ts_ms=2000,
                       bids=[[60000.5, 1.0]], asks=[[60001.5, 1.0]]),
        OrderbookFrame(inst_id="BTC-USDT-SWAP", ts_ms=3000,
                       bids=[[60001.0, 1.0]], asks=[[60002.0, 1.0]]),
    ]
    stream = OrderbookReplayStream(frames)

    # Drain frames strictly before ts_ms=2500
    drained = list(stream.drain_until(2500))
    assert len(drained) == 2
    assert drained[0].ts_ms == 1000
    assert drained[1].ts_ms == 2000

    # Cursor advanced: remaining frame is at 3000
    assert stream.remaining() == 1

    # Drain remaining
    drained2 = list(stream.drain_until(9999))
    assert len(drained2) == 1
    assert drained2[0].ts_ms == 3000
    assert stream.remaining() == 0


def test_replay_books_for_bar_calls_process_orderbook():
    """_replay_books_for_bar fires process_orderbook() for each drained frame."""
    from okx_trade.backtest.orderbook_data import OrderbookFrame, OrderbookReplayStream
    from okx_trade.backtest.orderbook_runner import _replay_books_for_bar
    from okx_trade.models.market import OrderBook

    frames = [
        OrderbookFrame(inst_id="BTC-USDT-SWAP", ts_ms=500,
                       bids=[[60000.0, 1.0]], asks=[[60001.0, 1.0]]),
        OrderbookFrame(inst_id="BTC-USDT-SWAP", ts_ms=1500,
                       bids=[[60000.5, 1.0]], asks=[[60001.5, 1.0]]),
    ]
    stream = OrderbookReplayStream(frames)

    received: list[OrderBook] = []

    class FakeStrategy:
        def process_orderbook(self, book: OrderBook) -> None:
            received.append(book)

    _replay_books_for_bar(FakeStrategy(), stream, bar_ts_ms_exclusive=1200)

    assert len(received) == 1
    assert received[0].ts == 500
    assert stream.remaining() == 1  # frame at 1500 not yet consumed
