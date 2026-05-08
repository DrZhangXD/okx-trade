"""OKX adapter 常量。

集中管理 venue / client_id / 频道路由等魔术字符串，便于全局替换。
"""
from __future__ import annotations

from nautilus_trader.model.identifiers import ClientId, Venue

# OKX 在 NT 中只有一个 venue —— 所有产品（SWAP / SPOT / FUTURES）共用。
OKX_VENUE: Venue = Venue("OKX")

# 给 NT TradingNode 注册时用的 client_id；data 和 exec 共享同一个名字，方便配置文件简化。
OKX_CLIENT_ID: ClientId = ClientId("OKX")

# OKX K 线粒度（NT BarSpec aggregation） → OKX bar 字符串
# NT BarSpec 用 (step, aggregation, price_type) 三元组，aggregation 是 enum
# 这里只列出 SWAP/SPOT 常用粒度，参考 okx_trade.enums.BarSize
OKX_BAR_SIZES: dict[str, str] = {
    "1-MINUTE-LAST": "1m",
    "3-MINUTE-LAST": "3m",
    "5-MINUTE-LAST": "5m",
    "15-MINUTE-LAST": "15m",
    "30-MINUTE-LAST": "30m",
    "1-HOUR-LAST": "1H",
    "2-HOUR-LAST": "2H",
    "4-HOUR-LAST": "4H",
    "6-HOUR-LAST": "6H",
    "12-HOUR-LAST": "12H",
    "1-DAY-LAST": "1D",
    "1-WEEK-LAST": "1W",
}


__all__ = [
    "OKX_VENUE",
    "OKX_CLIENT_ID",
    "OKX_BAR_SIZES",
]
