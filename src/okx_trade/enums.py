"""OKX API enums.

只覆盖 SWAP 永续 + SPOT 现货所需的枚举，按 OKX v5 文档命名（lowerCamel 字符串）。
"""
from __future__ import annotations

from enum import Enum


class InstType(str, Enum):
    SPOT = "SPOT"
    SWAP = "SWAP"
    FUTURES = "FUTURES"   # 交割合约（basis_arb 使用）
    # 未来扩展
    # OPTION = "OPTION"
    # MARGIN = "MARGIN"


class TdMode(str, Enum):
    """trade mode / margin mode."""
    CASH = "cash"          # 现货
    CROSS = "cross"        # 全仓
    ISOLATED = "isolated"  # 逐仓


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PosSide(str, Enum):
    """double-side mode 下的持仓方向；net 模式传 ``NET``。"""
    LONG = "long"
    SHORT = "short"
    NET = "net"


class OrdType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    POST_ONLY = "post_only"
    FOK = "fok"
    IOC = "ioc"
    OPTIMAL_LIMIT_IOC = "optimal_limit_ioc"


class BarSize(str, Enum):
    """K 线周期。OKX 用大小写区分分钟（m）和月（M）。"""
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1H"
    H2 = "2H"
    H4 = "4H"
    H6 = "6H"
    H12 = "12H"
    D1 = "1D"
    D2 = "2D"
    D3 = "3D"
    W1 = "1W"
    MO1 = "1M"
    MO3 = "3M"


class TriggerPxType(str, Enum):
    """止盈止损触发价类型。"""
    LAST = "last"
    INDEX = "index"
    MARK = "mark"


__all__ = [
    "BarSize",
    "InstType",
    "OrdType",
    "PosSide",
    "Side",
    "TdMode",
    "TriggerPxType",
]
