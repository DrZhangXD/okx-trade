"""Pydantic v2 data models for OKX API requests/responses/events."""
from .account import Balance, BalanceDetail, Position
from .common import Instrument, OKXModel
from .market import Candle, OrderBook, OrderBookLevel, Ticker, Trade
from .trade import AttachAlgoOrder, OrderRequest, OrderResult

__all__ = [
    # base
    "OKXModel",
    # common
    "Instrument",
    # market
    "Candle",
    "Ticker",
    "OrderBook",
    "OrderBookLevel",
    "Trade",
    # account
    "Balance",
    "BalanceDetail",
    "Position",
    # trade
    "AttachAlgoOrder",
    "OrderRequest",
    "OrderResult",
]
