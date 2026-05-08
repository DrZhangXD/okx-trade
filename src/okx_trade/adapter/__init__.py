"""NautilusTrader 适配器：把 ``okx_trade`` SDK 翻译为 NT 的 LiveDataClient / LiveExecutionClient。

NT (NautilusTrader) 是 Phase 2 的事件引擎，回测和实盘共享 Strategy 代码。OKX 没有官方
adapter，因此本包自研。

------------------------------------------------------------------
NT 概念 ↔ OKX 概念映射表（实现时严格遵守）
------------------------------------------------------------------

| NT                                          | OKX                                            |
| ------------------------------------------- | ---------------------------------------------- |
| ``Venue("OKX")``                            | （隐含——所有产品都在 OKX 平台）                |
| ``InstrumentId("BTC-USDT-SWAP.OKX")``       | ``instId="BTC-USDT-SWAP"``                     |
| ``CryptoPerpetual``                         | ``instType=SWAP`` 且 ``settleCcy != ""``       |
| ``CurrencyPair``                            | ``instType=SPOT``                              |
| ``OrderSide.BUY / SELL``                    | ``side=buy / sell``                            |
| ``OrderType.MARKET``                        | ``ordType=market``                             |
| ``OrderType.LIMIT``                         | ``ordType=limit``                              |
| ``TimeInForce.IOC``                         | ``ordType=ioc``                                |
| ``TimeInForce.FOK``                         | ``ordType=fok``                                |
| ``TimeInForce.GTC``                         | ``ordType=limit`` (默认)                       |
| ``ClientOrderId``                           | ``clOrdId``                                    |
| ``VenueOrderId``                            | ``ordId``                                      |
| 双向持仓 long/short                         | ``posSide=long / short``（账户开了对冲模式）   |
| 单向持仓 net                                | ``posSide=net`` 或不传                         |
| 杠杆账户 cross                              | ``tdMode=cross``                               |
| 杠杆账户 isolated                           | ``tdMode=isolated``                            |
| 现货账户                                    | ``tdMode=cash``                                |
| ``QuoteTick(bid, ask, bid_size, ask_size)`` | ``books5`` / ``tickers`` 频道首档              |
| ``TradeTick``                               | ``trades`` / ``trades-all`` 频道               |
| ``Bar(open,high,low,close,volume)``         | ``candle{period}`` business 频道（K 线）       |
| ``OrderFilled``                             | ``orders`` 频道 ``state=filled / partially_filled``  |
| ``OrderRejected``                           | place_order REST/WS 返回 ``sCode != "0"``      |
| ``OrderCanceled``                           | ``orders`` 频道 ``state=canceled``             |

------------------------------------------------------------------
模块结构
------------------------------------------------------------------
- ``constants.py`` —— 常量（OKX_VENUE / OKX_CLIENT_ID / 频道 routing 表等）
- ``parsing.py`` —— 纯函数翻译层（OKX dict ↔ NT 对象），便于独立单测
- ``instrument_provider.py`` —— ``OKXInstrumentProvider``：启动时拉 instruments 注册到 NT cache
- ``data.py`` —— ``OKXLiveDataClient``：订阅行情，frame → NT data event
- ``execution.py`` —— ``OKXLiveExecutionClient``：下单/撤单，OKX response → NT order event
- ``config.py`` —— msgspec Struct 配置类
- ``factories.py`` —— ``LiveDataClientFactory`` / ``LiveExecClientFactory`` 实现，供 NT TradingNode 注册
"""
from __future__ import annotations

from .config import OKXDataClientConfig, OKXExecClientConfig
from .constants import OKX_CLIENT_ID, OKX_VENUE
from .data import OKXLiveDataClient
from .execution import OKXLiveExecutionClient
from .factories import OKXLiveDataClientFactory, OKXLiveExecClientFactory
from .instrument_provider import OKXInstrumentProvider

__all__ = [
    # constants
    "OKX_VENUE",
    "OKX_CLIENT_ID",
    # configs
    "OKXDataClientConfig",
    "OKXExecClientConfig",
    # clients
    "OKXLiveDataClient",
    "OKXLiveExecutionClient",
    "OKXInstrumentProvider",
    # factories（NT TradingNode 注册用）
    "OKXLiveDataClientFactory",
    "OKXLiveExecClientFactory",
]
