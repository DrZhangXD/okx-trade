"""行情接口。"""
from __future__ import annotations

from ..enums import BarSize
from ..models.market import Candle, OrderBook, Ticker, Trade
from .transport import Transport


class MarketEndpoints:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    # ---- K 线 ----

    async def get_candles(
        self,
        inst_id: str,
        bar: BarSize | str = BarSize.H1,
        *,
        limit: int = 100,
        before: int | None = None,
        after: int | None = None,
    ) -> list[Candle]:
        """获取最近 K 线（OKX 默认按时间倒序，本方法返回正序）。

        ``before`` / ``after`` 是分页游标（毫秒时间戳，open API 语义）：
        - ``after``: 返回比此时间戳更早的数据
        - ``before``: 返回比此时间戳更新的数据
        """
        bar_str = bar.value if isinstance(bar, BarSize) else bar
        params: dict[str, str] = {"instId": inst_id, "bar": bar_str, "limit": str(limit)}
        if before is not None:
            params["before"] = str(before)
        if after is not None:
            params["after"] = str(after)
        data = await self._t.request(
            "GET", "/api/v5/market/candles",
            params=params, group="market.candles",
        )
        candles = [Candle.from_array(row) for row in data]
        candles.sort(key=lambda c: c.ts)
        return candles

    async def get_history_candles(
        self,
        inst_id: str,
        bar: BarSize | str = BarSize.H1,
        *,
        limit: int = 100,
        after: int | None = None,
    ) -> list[Candle]:
        """获取更早的历史 K 线（OKX 把 candles 与 history-candles 拆成两个端点）。"""
        bar_str = bar.value if isinstance(bar, BarSize) else bar
        params: dict[str, str] = {"instId": inst_id, "bar": bar_str, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        data = await self._t.request(
            "GET", "/api/v5/market/history-candles",
            params=params, group="market.history_candles",
        )
        candles = [Candle.from_array(row) for row in data]
        candles.sort(key=lambda c: c.ts)
        return candles

    async def get_candles_extended(
        self,
        inst_id: str,
        bar: BarSize | str,
        total: int = 1500,
    ) -> list[Candle]:
        """分页拉取更多历史 K 线（最近 ``total`` 根，按时间正序）。

        策略与 scalp 一致：先用 candles 拿最近 300 根，再用 history-candles 向前分页。
        """
        bar_str = bar.value if isinstance(bar, BarSize) else bar
        first_limit = min(total, 300)
        recent = await self.get_candles(inst_id, bar_str, limit=first_limit)

        all_candles: list[Candle] = list(recent)
        seen: set[int] = {c.ts for c in all_candles}

        while len(all_candles) < total and all_candles:
            earliest_ts = min(c.ts for c in all_candles)
            page_size = min(100, total - len(all_candles))
            batch = await self.get_history_candles(
                inst_id, bar_str, limit=page_size, after=earliest_ts,
            )
            new_rows = [c for c in batch if c.ts not in seen]
            if not new_rows:
                break
            all_candles.extend(new_rows)
            seen.update(c.ts for c in new_rows)

        all_candles.sort(key=lambda c: c.ts)
        return all_candles

    # ---- Ticker / 深度 / 成交 ----

    async def get_ticker(self, inst_id: str) -> Ticker:
        data = await self._t.request(
            "GET", "/api/v5/market/ticker",
            params={"instId": inst_id}, group="market.tickers",
        )
        if not data:
            from ..exceptions import OKXAPIError
            raise OKXAPIError("not_found", f"ticker {inst_id} not found", endpoint="/market/ticker")
        return Ticker.model_validate(data[0])

    async def get_books(self, inst_id: str, *, sz: int = 5) -> OrderBook:
        """获取深度。``sz`` 为深度档数（1/5/10/20/50/400 等，按 OKX 文档支持值）。"""
        data = await self._t.request(
            "GET", "/api/v5/market/books",
            params={"instId": inst_id, "sz": str(sz)},
            group="market.books",
        )
        if not data:
            from ..exceptions import OKXAPIError
            raise OKXAPIError("not_found", f"books {inst_id} not found", endpoint="/market/books")
        return OrderBook.from_payload(inst_id, data[0])

    async def get_trades(self, inst_id: str, *, limit: int = 100) -> list[Trade]:
        data = await self._t.request(
            "GET", "/api/v5/market/trades",
            params={"instId": inst_id, "limit": str(limit)},
            group="market.trades",
        )
        return [Trade.model_validate(d) for d in data]


__all__ = ["MarketEndpoints"]
