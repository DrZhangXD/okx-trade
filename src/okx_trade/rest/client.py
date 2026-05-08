"""OKXRestClient 门面：聚合 market / public / account / trade。

用聚合而非继承——业务方法分散在多个子类便于阅读、单测时也可以单独 mock 子组件。

典型用法：

    settings = OKXSettings()
    async with OKXRestClient(settings) as client:
        ticker = await client.market.get_ticker("BTC-USDT-SWAP")
        balance = await client.account.get_balance("USDT")
"""
from __future__ import annotations

from ..config import OKXSettings
from .account import AccountEndpoints
from .market import MarketEndpoints
from .public import PublicEndpoints
from .ratelimit import RateLimiter
from .trade import TradeEndpoints
from .transport import Transport


class OKXRestClient:
    """聚合所有 REST 端点的门面。"""

    def __init__(
        self,
        settings: OKXSettings | None = None,
        *,
        transport: Transport | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if transport is None:
            settings = settings if settings is not None else OKXSettings()
            transport = Transport(settings, rate_limiter=rate_limiter)
            self._owns_transport = True
        else:
            self._owns_transport = False
        self._transport = transport

        self.public = PublicEndpoints(transport)
        self.market = MarketEndpoints(transport)
        self.account = AccountEndpoints(transport)
        self.trade = TradeEndpoints(transport)

    @property
    def settings(self) -> OKXSettings:
        return self._transport.settings

    @property
    def transport(self) -> Transport:
        return self._transport

    async def __aenter__(self) -> OKXRestClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_transport:
            await self._transport.aclose()


__all__ = ["OKXRestClient"]
