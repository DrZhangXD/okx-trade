"""公共数据接口（不需鉴权）：交易品种规格、funding rate 等。"""
from __future__ import annotations

from ..enums import InstType
from ..exceptions import OKXAPIError
from ..models.common import FundingRate, Instrument, OptionSummary
from ..models.market import OpenInterest, OpenInterestPoint
from .transport import Transport


class PublicEndpoints:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    async def get_instruments(
        self,
        inst_type: InstType,
        inst_id: str | None = None,
        uly: str | None = None,
    ) -> list[Instrument]:
        """获取交易品种规格列表。

        最常见用法是查单个品种：``get_instruments(InstType.SWAP, "BTC-USDT-SWAP")``。
        不传 ``inst_id`` 时返回该 instType 下所有品种。
        """
        params: dict[str, str] = {"instType": inst_type.value}
        if inst_id:
            params["instId"] = inst_id
        if uly:
            params["uly"] = uly
        data = await self._t.request(
            "GET", "/api/v5/public/instruments",
            params=params, group="public.instruments",
        )
        return [Instrument.model_validate(d) for d in data]

    async def get_instrument(self, inst_type: InstType, inst_id: str) -> Instrument:
        """便捷方法：拿单个品种规格，找不到则抛 ``OKXAPIError``。"""
        items = await self.get_instruments(inst_type, inst_id)
        if not items:
            raise OKXAPIError(
                code="not_found",
                message=f"instrument {inst_id} not found",
                endpoint="/api/v5/public/instruments",
            )
        return items[0]

    async def get_funding_rate(self, inst_id: str) -> FundingRate:
        """``GET /api/v5/public/funding-rate``。

        每 8h 结算一次。``inst_id`` 必须是永续合约（如 ``BTC-USDT-SWAP``）。
        """
        data = await self._t.request(
            "GET", "/api/v5/public/funding-rate",
            params={"instId": inst_id}, group="public.funding_rate",
        )
        if not data:
            raise OKXAPIError(
                code="not_found",
                message=f"no funding rate for {inst_id}",
                endpoint="/api/v5/public/funding-rate",
            )
        return FundingRate.model_validate(data[0])

    async def get_funding_rate_history(
        self,
        inst_id: str,
        *,
        limit: int = 100,
        before: int | None = None,
        after: int | None = None,
    ) -> list[FundingRate]:
        """``GET /api/v5/public/funding-rate-history``。

        每 8h 结算一次。OKX 单页最多 100 条；用 ``after`` 游标向更早翻页。
        返回按 funding_time 升序。
        """
        params: dict[str, str] = {"instId": inst_id, "limit": str(limit)}
        if before is not None:
            params["before"] = str(before)
        if after is not None:
            params["after"] = str(after)
        data = await self._t.request(
            "GET", "/api/v5/public/funding-rate-history",
            params=params, group="public.funding_rate_history",
        )
        rates = [FundingRate.model_validate(d) for d in data]
        rates.sort(key=lambda r: r.funding_time)
        return rates

    async def get_funding_rate_history_extended(
        self,
        inst_id: str,
        *,
        total: int = 1095,  # 默认 1 年（8h × 365 ≈ 1095）
    ) -> list[FundingRate]:
        """分页拉取历史 funding rate，按时间升序返回。"""
        all_rates: list[FundingRate] = []
        seen: set[int] = set()
        cursor: int | None = None
        while len(all_rates) < total:
            page_size = min(100, total - len(all_rates))
            batch = await self.get_funding_rate_history(
                inst_id, limit=page_size, after=cursor,
            )
            new_rows = [r for r in batch if r.funding_time not in seen]
            if not new_rows:
                break
            all_rates.extend(new_rows)
            seen.update(r.funding_time for r in new_rows)
            cursor = min(r.funding_time for r in new_rows)
        all_rates.sort(key=lambda r: r.funding_time)
        return all_rates


    async def get_option_summary(
        self,
        uly: str,
        exp_time_ms: int | None = None,
    ) -> list[OptionSummary]:
        """``GET /api/v5/public/opt-summary``。

        返回某标的（如 ``BTC-USD``）下所有 live 期权的 mark IV + Greeks。
        ``exp_time_ms`` 可选，按到期日过滤；不传则返回所有到期日。

        OKX 返回 ``deltaBS``/``gammaBS``/``vegaBS``/``thetaBS``（BS 模型计算的 Greeks），
        ``markVol`` 是 mark price 反推的 IV，``volLv`` 是 ATM 近似 IV。
        """
        params: dict[str, str] = {"uly": uly}
        if exp_time_ms is not None:
            params["expTime"] = str(exp_time_ms)
        data = await self._t.request(
            "GET", "/api/v5/public/opt-summary",
            params=params, group="public.opt_summary",
        )
        return [OptionSummary.model_validate(d) for d in data]

    async def get_open_interest(
        self,
        inst_id: str,
        inst_type: str = "SWAP",
    ) -> OpenInterest:
        """``GET /api/v5/public/open-interest`` —— 即时持仓量。

        ``inst_type`` 默认 ``SWAP``（永续）；交割合约传 ``FUTURES``。
        """
        params = {"instType": inst_type, "instId": inst_id}
        data = await self._t.request(
            "GET", "/api/v5/public/open-interest",
            params=params, group="public.open_interest",
        )
        if not data:
            raise OKXAPIError(
                code="not_found",
                message=f"no open interest for {inst_id}",
                endpoint="/api/v5/public/open-interest",
            )
        return OpenInterest.model_validate(data[0])

    async def get_open_interest_history(
        self,
        inst_id: str,
        *,
        period: str = "1H",
        limit: int = 100,
        before: int | None = None,
        after: int | None = None,
    ) -> list[OpenInterestPoint]:
        """``GET /api/v5/rubik/stat/contracts/open-interest-volume`` —— 历史 OI + volume。

        参数说明：
        - ``inst_id``: rubik 端点用 ``ccy``（base 币种），这里接受 ``BTC-USDT`` 形式，
          内部取首段当 ccy；也接受裸 ``BTC``。
        - ``period``: ``5m / 1H / 1D``（rubik 限定枚举）。
        - 翻页：``after``=毫秒时间戳，向更早翻页。

        返回按 ``ts`` 升序。
        """
        ccy = inst_id.split("-", 1)[0] if "-" in inst_id else inst_id
        params: dict[str, str] = {"ccy": ccy, "period": period, "limit": str(limit)}
        if before is not None:
            params["begin"] = str(before)
        if after is not None:
            params["end"] = str(after)
        data = await self._t.request(
            "GET", "/api/v5/rubik/stat/contracts/open-interest-volume",
            params=params, group="public.open_interest_history",
        )
        points = [OpenInterestPoint.from_array(row) for row in data]
        points.sort(key=lambda p: p.ts)
        return points

    async def get_open_interest_history_extended(
        self,
        inst_id: str,
        *,
        period: str = "1H",
        total: int = 720,
    ) -> list[OpenInterestPoint]:
        """分页拉历史 OI（每页 100 条），按时间升序返回。

        与 ``get_funding_rate_history_extended`` 同样的去重 + 游标逻辑。
        """
        all_pts: list[OpenInterestPoint] = []
        seen: set[int] = set()
        cursor: int | None = None
        while len(all_pts) < total:
            page_size = min(100, total - len(all_pts))
            try:
                batch = await self.get_open_interest_history(
                    inst_id, period=period, limit=page_size, after=cursor,
                )
            except OKXAPIError as exc:
                # Rubik OI history only covers a recent window (~30-90 days). Going
                # further back returns code=50030 "Illegal time range" — graceful
                # break with whatever we've already gathered.
                if exc.code == "50030":
                    break
                raise
            new_rows = [p for p in batch if p.ts not in seen]
            if not new_rows:
                break
            all_pts.extend(new_rows)
            seen.update(p.ts for p in new_rows)
            cursor = min(p.ts for p in new_rows)
        all_pts.sort(key=lambda p: p.ts)
        return all_pts


__all__ = ["PublicEndpoints"]
