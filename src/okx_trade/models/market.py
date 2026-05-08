"""行情数据模型。

OKX 的 ``candles`` 返回数组（位置编码而非键值），需要 ``Candle.from_array`` 解析。
其他端点（ticker / books / trades）返回 dict，pydantic 直接搞定。
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from .common import OKXModel


class Candle(OKXModel):
    """K 线。OKX 原始顺序：[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]。

    其中 ``ts`` 是毫秒时间戳，``confirm`` 是 "1" 表示已收线。
    """

    ts: int                            # 毫秒时间戳
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal                    # 成交量（基础币种）
    volume_ccy: Decimal = Decimal("0")  # 成交额（计价币种）
    volume_ccy_quote: Decimal = Decimal("0")
    confirm: bool = True

    @classmethod
    def from_array(cls, row: list[str]) -> Candle:
        """从 OKX 原始数组构造。容忍长度不足（早期 API 字段更少）。"""
        ts, o, h, low_, c, vol, *rest = row
        vol_ccy = rest[0] if len(rest) >= 1 else "0"
        vol_ccy_q = rest[1] if len(rest) >= 2 else "0"
        confirm_str = rest[2] if len(rest) >= 3 else "1"
        return cls(
            ts=int(ts),
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(low_),
            close=Decimal(c),
            volume=Decimal(vol),
            volume_ccy=Decimal(vol_ccy),
            volume_ccy_quote=Decimal(vol_ccy_q),
            confirm=confirm_str == "1",
        )


class Ticker(OKXModel):
    """``GET /api/v5/market/ticker``。"""

    inst_id: str = Field(alias="instId")
    inst_type: str = Field(alias="instType")
    last: Decimal
    last_sz: Decimal = Field(alias="lastSz", default=Decimal("0"))
    ask_px: Decimal = Field(alias="askPx", default=Decimal("0"))
    ask_sz: Decimal = Field(alias="askSz", default=Decimal("0"))
    bid_px: Decimal = Field(alias="bidPx", default=Decimal("0"))
    bid_sz: Decimal = Field(alias="bidSz", default=Decimal("0"))
    open_24h: Decimal = Field(alias="open24h", default=Decimal("0"))
    high_24h: Decimal = Field(alias="high24h", default=Decimal("0"))
    low_24h: Decimal = Field(alias="low24h", default=Decimal("0"))
    vol_ccy_24h: Decimal = Field(alias="volCcy24h", default=Decimal("0"))
    vol_24h: Decimal = Field(alias="vol24h", default=Decimal("0"))
    ts: int


class OrderBookLevel(OKXModel):
    """单档深度：[price, size, deprecated, num_orders]。"""

    price: Decimal
    size: Decimal
    num_orders: int = 0

    @classmethod
    def from_array(cls, row: list[str]) -> OrderBookLevel:
        # 形如 ["price", "size", "0", "num_orders"]，第三位 deprecated
        price, size, _, num = row[:4]
        return cls(price=Decimal(price), size=Decimal(size), num_orders=int(num))


class OrderBook(OKXModel):
    """深度快照（books / books5 / books-l2-tbt 通用）。"""

    inst_id: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    ts: int

    @classmethod
    def from_payload(cls, inst_id: str, payload: dict) -> OrderBook:
        """OKX REST 返回 ``{"asks": [[...], ...], "bids": [[...], ...], "ts": "..."}``。"""
        return cls(
            inst_id=inst_id,
            bids=[OrderBookLevel.from_array(r) for r in payload.get("bids", [])],
            asks=[OrderBookLevel.from_array(r) for r in payload.get("asks", [])],
            ts=int(payload["ts"]),
        )


class Trade(OKXModel):
    """成交（``GET /api/v5/market/trades``）。"""

    inst_id: str = Field(alias="instId")
    trade_id: str = Field(alias="tradeId")
    price: Decimal = Field(alias="px")
    size: Decimal = Field(alias="sz")
    side: str  # buy / sell
    ts: int


__all__ = ["Candle", "OrderBook", "OrderBookLevel", "Ticker", "Trade"]
