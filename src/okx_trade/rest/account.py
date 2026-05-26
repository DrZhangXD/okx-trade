"""账户与持仓接口（需鉴权）。"""
from __future__ import annotations

from ..enums import InstType, PosSide, TdMode
from ..models.account import Balance, Position
from .transport import Transport


class AccountEndpoints:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    async def get_balance(self, ccy: str | None = None) -> Balance:
        """``GET /api/v5/account/balance``。``ccy`` 留空返回所有币种汇总。"""
        params: dict[str, str] = {}
        if ccy:
            params["ccy"] = ccy
        data = await self._t.request(
            "GET", "/api/v5/account/balance",
            params=params or None, private=True, group="account.balance",
        )
        if not data:
            return Balance()
        return Balance.model_validate(data[0])

    async def get_positions(
        self,
        inst_type: InstType | None = None,
        inst_id: str | None = None,
    ) -> list[Position]:
        """获取所有持仓。OKX 即使无持仓也会返回行（``pos == 0``），上层用
        :pyattr:`Position.is_open` 过滤。"""
        params: dict[str, str] = {}
        if inst_type:
            params["instType"] = inst_type.value
        if inst_id:
            params["instId"] = inst_id
        data = await self._t.request(
            "GET", "/api/v5/account/positions",
            params=params or None, private=True, group="account.positions",
        )
        return [Position.model_validate(d) for d in data]

    async def get_open_position(self, inst_id: str) -> Position | None:
        """便捷方法：返回该品种当前有持仓的那一行（无仓返回 None）。"""
        positions = await self.get_positions(inst_id=inst_id)
        for p in positions:
            if p.is_open:
                return p
        return None

    async def set_leverage(
        self,
        inst_id: str,
        leverage: int,
        mgn_mode: TdMode,
        *,
        pos_side: PosSide | None = None,
    ) -> None:
        """``POST /api/v5/account/set-leverage``。

        - ``mgn_mode=ISOLATED``: OKX requires ``posSide`` even in net-position
          mode.  When ``pos_side`` is ``None`` it is automatically set to
          ``PosSide.NET``; pass an explicit value for hedge-mode accounts.
        - ``mgn_mode=CROSS`` 不传 ``pos_side``。
        """
        # OKX requires posSide even in net-position mode when mgnMode=isolated.
        # Auto-default to PosSide.NET so callers don't have to know this quirk.
        if mgn_mode == TdMode.ISOLATED and pos_side is None:
            pos_side = PosSide.NET
        body: dict[str, str] = {
            "instId": inst_id,
            "lever": str(leverage),
            "mgnMode": mgn_mode.value,
        }
        if pos_side is not None and mgn_mode == TdMode.ISOLATED:
            body["posSide"] = pos_side.value
        await self._t.request(
            "POST", "/api/v5/account/set-leverage",
            json_body=body, private=True, group="account.set_leverage",
        )


__all__ = ["AccountEndpoints"]
