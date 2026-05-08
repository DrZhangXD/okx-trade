"""公共数据接口（不需鉴权）：交易品种规格、funding rate 等。"""
from __future__ import annotations

from ..enums import InstType
from ..exceptions import OKXAPIError
from ..models.common import FundingRate, Instrument
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


__all__ = ["PublicEndpoints"]
