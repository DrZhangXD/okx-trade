"""OKX WebSocket 高层客户端：管理 public/private/business 三个物理连接 + 分发。

设计要点
--------
- **三连接懒启动**：`/ws/v5/public`、`/private`、`/business` 物理上分离，本类持有
  三个 ``WSConnection``，但只在第一次有该类型的订阅请求时才 ``start()``；
- **频道路由**：根据 channel 名前缀判定走哪个 endpoint，用户无感；
- **统一 dispatcher**：三连接的 ``on_message`` 都桥接到同一 ``Dispatcher`` 实例，
  callback / async iterator 两套 API 共享路由表；
- **凭证可选**：未配置凭证时 private/business 连接禁用，订阅这些频道会抛
  ``OKXWSConnectionError``；
- **优雅关闭**：``async with`` 或显式 ``close()``——逐个停掉已启动的连接。

频道归类（基于 OKX v5 文档）::

    public    : tickers, books*, trades, mark-price, index-*, instruments,
                price-limit, open-interest, funding-rate, estimated-price,
                opt-summary, liquidation-orders, status, ...
    private   : account, positions, balance_and_position, orders, orders-algo,
                liquidation-warning, account-greeks
    business  : candle* (K 线推送), mark-price-candle*, index-candle*,
                public-struct-block-trades, deposit-info, withdrawal-info,
                grid-orders-*, algo-advance, ...

无法识别的 channel 默认走 ``public``——OKX 也是把"杂项"放在 public 端。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from ..config import OKXSettings
from ..exceptions import OKXWSConnectionError
from ..utils.logging import get_logger
from .connection import WSConnection
from .dispatcher import Dispatcher, EventHandler
from .subscription import SubscriptionKey

log = get_logger(__name__)

EndpointKind = Literal["public", "private", "business"]


# ---------------------------------------------------------------------------
# 频道 → endpoint 路由表
# ---------------------------------------------------------------------------

_PRIVATE_CHANNELS = frozenset(
    {
        "account",
        "positions",
        "balance_and_position",
        "orders",
        "orders-algo",
        "algo-advance",
        "liquidation-warning",
        "account-greeks",
        "rfqs",
        "quotes",
        "struct-block-trades",
    }
)

_BUSINESS_CHANNEL_PREFIXES: tuple[str, ...] = (
    "candle",            # candle1m / candle5m / candle1H ... 全在 business
    "mark-price-candle",
    "index-candle",
    "deposit-info",
    "withdrawal-info",
    "grid-orders-",
    "grid-positions",
    "grid-sub-orders",
)

_BUSINESS_CHANNELS = frozenset({"public-struct-block-trades"})


def resolve_endpoint(channel: str) -> EndpointKind:
    """根据 channel 名判定 endpoint。"""
    if channel in _PRIVATE_CHANNELS:
        return "private"
    if channel in _BUSINESS_CHANNELS:
        return "business"
    for prefix in _BUSINESS_CHANNEL_PREFIXES:
        if channel.startswith(prefix):
            return "business"
    return "public"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OKXWSClient:
    """OKX WebSocket 客户端门面。

    典型用法：

        async with OKXWSClient(settings) as ws:
            async for event in ws.subscribe("books5", instId="BTC-USDT-SWAP"):
                print(event["data"])

    或低层 callback：

        async def on_tick(frame):
            print(frame)

        ws.on("tickers", on_tick, instId="BTC-USDT")
        await ws.start()  # 显式启动
        ...
        await ws.close()
    """

    def __init__(
        self,
        settings: OKXSettings,
        *,
        dispatcher: Dispatcher | None = None,
    ) -> None:
        self._settings = settings
        self.dispatcher = dispatcher if dispatcher is not None else Dispatcher()
        self._proxy = settings.https_proxy or settings.http_proxy

        # 三连接懒创建
        self._connections: dict[EndpointKind, WSConnection] = {}
        self._closed = False

        # WS trade 懒创建（占位；属性 access 时才构造，避免循环 import）
        self._trade: Any = None

    @property
    def trade(self) -> Any:
        """WS 下单客户端（懒加载，复用 private 连接）。

        典型用法::

            res = await ws.trade.place_order(req)  # 走 WS，不走 REST
        """
        if self._trade is None:
            from .trade import WSTradeClient  # 延迟导入避免循环
            self._trade = WSTradeClient(self)
        return self._trade

    async def _on_frame(self, frame: dict[str, Any]) -> None:
        """所有连接共用的入口。先尝试当作 trade 响应处理，否则交 dispatcher。"""
        # WS trade 响应识别：我们发出去的请求带 id；OKX 推送数据帧不带 id
        if self._trade is not None and "id" in frame and "op" in frame:
            if self._trade.try_complete(frame):
                return
        await self.dispatcher.dispatch(frame)

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------

    async def __aenter__(self) -> OKXWSClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """关闭所有已启动的连接。"""
        self._closed = True
        for conn in list(self._connections.values()):
            await conn.stop()
        self._connections.clear()

    # ------------------------------------------------------------------
    # 高层 API
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def subscribe(
        self,
        channel: str,
        **filters: str,
    ) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        """订阅一个频道并返回 async iterator。``async with`` 自动取消订阅。

            async with ws.subscribe("books5", instId="BTC-USDT-SWAP") as stream:
                async for event in stream:
                    ...
        """
        key = SubscriptionKey.make(channel, **filters)
        conn = await self._get_or_start(resolve_endpoint(channel))
        await conn.subscribe([key])
        gen = self.dispatcher.iter_events(key)
        try:
            yield gen
        finally:
            await gen.aclose()
            await conn.unsubscribe([key])

    def on(
        self,
        channel: str,
        handler: EventHandler,
        **filters: str,
    ) -> SubscriptionKey:
        """注册 callback。返回 SubscriptionKey 供后续 ``off`` 用。

        注意：仅注册 callback，不会自动启动连接也不会自动 subscribe。
        请在合适时机调用 ``await client.activate(channel, **filters)``，
        或直接用 ``subscribe()`` 高层 API（推荐）。
        """
        key = SubscriptionKey.make(channel, **filters)
        self.dispatcher.add_callback(key, handler)
        return key

    def off(self, key: SubscriptionKey, handler: EventHandler) -> None:
        """注销 callback。"""
        self.dispatcher.remove_callback(key, handler)

    async def activate(self, channel: str, **filters: str) -> SubscriptionKey:
        """显式启动连接并发出 subscribe 帧（callback 模式专用）。"""
        key = SubscriptionKey.make(channel, **filters)
        conn = await self._get_or_start(resolve_endpoint(channel))
        await conn.subscribe([key])
        return key

    async def deactivate(self, key: SubscriptionKey) -> None:
        """显式 unsubscribe（callback 模式配套）。"""
        endpoint = resolve_endpoint(key.channel)
        conn = self._connections.get(endpoint)
        if conn is None:
            return
        await conn.unsubscribe([key])

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _get_or_start(self, kind: EndpointKind) -> WSConnection:
        """懒启动指定类型的连接，等首次连接成功后返回。"""
        if self._closed:
            raise OKXWSConnectionError("client is closed")
        conn = self._connections.get(kind)
        if conn is not None:
            return conn

        url = self._url_for(kind)
        creds: tuple[str, str, str] | None = None
        if kind in ("private", "business"):
            if not self._settings.has_credentials():
                raise OKXWSConnectionError(
                    f"missing OKX credentials for {kind} websocket"
                )
            creds = self._settings.credentials()

        conn = WSConnection(
            url,
            self._on_frame,
            login_creds=creds,
            proxy=self._proxy,
        )
        self._connections[kind] = conn
        await conn.start()
        await conn.wait_connected(timeout=self._settings.request_timeout_sec)
        log.info("ws_endpoint_ready", kind=kind, url=url)
        return conn

    def _url_for(self, kind: EndpointKind) -> str:
        if kind == "public":
            return self._settings.effective_ws_public_url
        if kind == "private":
            return self._settings.effective_ws_private_url
        return self._settings.effective_ws_business_url


__all__ = ["EndpointKind", "OKXWSClient", "resolve_endpoint"]
