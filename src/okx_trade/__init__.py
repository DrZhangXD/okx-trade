"""okx-trade: Async OKX API client for crypto quant trading."""
from __future__ import annotations

from .config import OKXSettings
from .enums import BarSize, InstType, OrdType, PosSide, Side, TdMode, TriggerPxType
from .exceptions import (
    OKXAPIError,
    OKXAuthError,
    OKXError,
    OKXInsufficientBalance,
    OKXNetworkError,
    OKXOrderNotFound,
    OKXRateLimitError,
    OKXSubscriptionError,
    OKXWSConnectionError,
    OKXWSError,
)
from .models import (
    Balance,
    Candle,
    Instrument,
    OrderBook,
    OrderRequest,
    OrderResult,
    Position,
    Ticker,
    Trade,
)
from .rest.client import OKXRestClient
from .ws.client import OKXWSClient
from .ws.subscription import SubscriptionKey

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # config
    "OKXSettings",
    # client
    "OKXRestClient",
    "OKXWSClient",
    "SubscriptionKey",
    # enums
    "InstType",
    "TdMode",
    "Side",
    "PosSide",
    "OrdType",
    "BarSize",
    "TriggerPxType",
    # exceptions
    "OKXError",
    "OKXAPIError",
    "OKXAuthError",
    "OKXInsufficientBalance",
    "OKXOrderNotFound",
    "OKXNetworkError",
    "OKXRateLimitError",
    "OKXWSError",
    "OKXWSConnectionError",
    "OKXSubscriptionError",
    # models
    "Instrument",
    "Candle",
    "Ticker",
    "OrderBook",
    "Trade",
    "Balance",
    "Position",
    "OrderRequest",
    "OrderResult",
]
