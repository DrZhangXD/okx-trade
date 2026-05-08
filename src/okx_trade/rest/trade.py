"""交易接口（下单 / 撤单 / 改单 / 平仓 / 算法单）。"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..enums import OrdType, PosSide, Side, TdMode, TriggerPxType
from ..exceptions import OKXAPIError
from ..models.trade import AttachAlgoOrder, OrderRequest, OrderResult
from ..utils.precision import fmt_price, fmt_size
from .transport import Transport


class TradeEndpoints:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    # ---- 普通下单 ----

    async def place_order(self, req: OrderRequest) -> OrderResult:
        """``POST /api/v5/trade/order``。

        OKX 即使 HTTP/业务码都是 0，单笔订单仍可能因风控失败（``sCode != "0"``）。
        本方法在这种情况下抛 ``OKXAPIError``——避免上层误以为下单成功。
        """
        body = req.model_dump(by_alias=True, exclude_none=True, mode="json")
        data = await self._t.request(
            "POST", "/api/v5/trade/order",
            json_body=body, private=True, group="trade.order",
        )
        if not data:
            raise OKXAPIError("empty", "place_order returned empty data", endpoint="/trade/order")
        result = OrderResult.model_validate(data[0])
        if not result.ok:
            raise OKXAPIError(
                code=result.s_code,
                message=result.s_msg,
                data=data[0],
                endpoint="/api/v5/trade/order",
            )
        return result

    async def cancel_order(self, inst_id: str, *, ord_id: str | None = None,
                           cl_ord_id: str | None = None) -> OrderResult:
        if not ord_id and not cl_ord_id:
            raise ValueError("either ord_id or cl_ord_id must be provided")
        body: dict[str, str] = {"instId": inst_id}
        if ord_id:
            body["ordId"] = ord_id
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        data = await self._t.request(
            "POST", "/api/v5/trade/cancel-order",
            json_body=body, private=True, group="trade.cancel_order",
        )
        if not data:
            raise OKXAPIError("empty", "cancel_order returned empty", endpoint="/trade/cancel-order")
        result = OrderResult.model_validate(data[0])
        if not result.ok:
            raise OKXAPIError(
                code=result.s_code, message=result.s_msg, data=data[0],
                endpoint="/api/v5/trade/cancel-order",
            )
        return result

    # ---- 高频便捷方法（沿用 scalp 接口语义）----

    async def place_market_with_tp_sl(
        self,
        inst_id: str,
        *,
        side: Side,
        pos_side: PosSide,
        size_contracts: Decimal | float | str,
        tp_price: Decimal | float | str,
        sl_price: Decimal | float | str,
        td_mode: TdMode = TdMode.CROSS,
        tick_sz: Decimal | float | str = "0.1",
        lot_sz: Decimal | float | str = "1",
        cl_ord_id: str | None = None,
    ) -> OrderResult:
        """市价单 + 附加止盈止损（条件单）一键下出。

        - 价格按 ``tick_sz`` 对齐；数量按 ``lot_sz`` 格式化为字符串。
        - ``ord_px = "-1"`` 表示触发后市价平仓（OKX 特殊语义）。
        """
        sz_str = fmt_size(size_contracts, lot_sz)
        tp_str = fmt_price(tp_price, tick_sz)
        sl_str = fmt_price(sl_price, tick_sz)

        attach = AttachAlgoOrder(
            tp_trigger_px=Decimal(tp_str),
            tp_ord_px="-1",
            tp_trigger_px_type=TriggerPxType.LAST,
            sl_trigger_px=Decimal(sl_str),
            sl_ord_px="-1",
            sl_trigger_px_type=TriggerPxType.LAST,
        )
        req = OrderRequest(
            inst_id=inst_id,
            td_mode=td_mode,
            side=side,
            pos_side=pos_side,
            ord_type=OrdType.MARKET,
            sz=sz_str,
            cl_ord_id=cl_ord_id,
            attach_algo_ords=[attach],
        )
        return await self.place_order(req)

    async def close_position(
        self,
        inst_id: str,
        *,
        pos_side: PosSide,
        mgn_mode: TdMode = TdMode.CROSS,
        ccy: str | None = None,
    ) -> dict[str, Any]:
        """``POST /api/v5/trade/close-position``——按持仓方向市价平仓。"""
        body: dict[str, str] = {
            "instId": inst_id,
            "mgnMode": mgn_mode.value,
            "posSide": pos_side.value,
        }
        if ccy:
            body["ccy"] = ccy
        data = await self._t.request(
            "POST", "/api/v5/trade/close-position",
            json_body=body, private=True, group="trade.close_position",
        )
        return data[0] if data else {}

    # ---- 订单查询（reconciliation 用）----

    async def get_pending_orders(
        self,
        *,
        inst_type: str | None = None,
        inst_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /api/v5/trade/orders-pending``——返回 ``live`` / ``partially_filled``。

        启动时 reconciliation 用：把 OKX 当前挂着的单拉回来对账。
        ``inst_type`` 形如 ``"SWAP"`` / ``"SPOT"``；``limit`` 默认 100、最大 100。
        """
        params: dict[str, str] = {}
        if inst_type:
            params["instType"] = inst_type
        if inst_id:
            params["instId"] = inst_id
        if limit:
            params["limit"] = str(limit)
        return await self._t.request(
            "GET", "/api/v5/trade/orders-pending",
            params=params or None, private=True, group="trade.orders_pending",
        )

    async def get_orders_history(
        self,
        inst_type: str,
        *,
        inst_id: str | None = None,
        begin_ms: int | None = None,
        end_ms: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /api/v5/trade/orders-history-archive``——已结束的历史单（最近 3 月）。

        ``inst_type`` 必填（OKX 强制）；``begin_ms`` / ``end_ms`` 是 OKX 的 ``begin``
        ``end`` 字段（毫秒）。reconciliation 走 ``open_only=False`` 时拉这条配合
        ``orders-pending`` 一起返回完整快照。
        """
        params: dict[str, str] = {"instType": inst_type}
        if inst_id:
            params["instId"] = inst_id
        if begin_ms is not None:
            params["begin"] = str(begin_ms)
        if end_ms is not None:
            params["end"] = str(end_ms)
        if limit:
            params["limit"] = str(limit)
        return await self._t.request(
            "GET", "/api/v5/trade/orders-history-archive",
            params=params, private=True, group="trade.orders_history",
        )

    async def get_order(
        self,
        inst_id: str,
        *,
        ord_id: str | None = None,
        cl_ord_id: str | None = None,
    ) -> dict[str, Any] | None:
        """``GET /api/v5/trade/order``——按 ``ordId`` 或 ``clOrdId`` 查单。

        ``ord_id`` / ``cl_ord_id`` 必给一个；OKX 同时给会优先按 ``ord_id``。
        无匹配返回 ``None``。
        """
        if not ord_id and not cl_ord_id:
            raise ValueError("either ord_id or cl_ord_id must be provided")
        params: dict[str, str] = {"instId": inst_id}
        if ord_id:
            params["ordId"] = ord_id
        if cl_ord_id:
            params["clOrdId"] = cl_ord_id
        data = await self._t.request(
            "GET", "/api/v5/trade/order",
            params=params, private=True, group="trade.order_query",
        )
        return data[0] if data else None

    async def get_fills(
        self,
        *,
        inst_type: str | None = None,
        inst_id: str | None = None,
        ord_id: str | None = None,
        begin_ms: int | None = None,
        end_ms: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /api/v5/trade/fills``——最近 3 天成交明细。

        reconciliation 用来把启动期错过的成交补回来。如果需要 3 天以前的，要走
        ``/trade/fills-history``——M5 不实现。
        """
        params: dict[str, str] = {}
        if inst_type:
            params["instType"] = inst_type
        if inst_id:
            params["instId"] = inst_id
        if ord_id:
            params["ordId"] = ord_id
        if begin_ms is not None:
            params["begin"] = str(begin_ms)
        if end_ms is not None:
            params["end"] = str(end_ms)
        if limit:
            params["limit"] = str(limit)
        return await self._t.request(
            "GET", "/api/v5/trade/fills",
            params=params or None, private=True, group="trade.fills",
        )

    # ---- 算法单（条件单 / 止盈止损单）----

    async def get_pending_algo_orders(
        self, inst_id: str | None = None, *, ord_type: str = "conditional",
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"ordType": ord_type}
        if inst_id:
            params["instId"] = inst_id
        return await self._t.request(
            "GET", "/api/v5/trade/orders-algo-pending",
            params=params, private=True, group="trade.algo_orders",
        )

    async def cancel_algo_orders(self, inst_id: str) -> int:
        """撤销该品种所有挂着的条件单（止盈止损）。返回撤销条数。"""
        pending = await self.get_pending_algo_orders(inst_id=inst_id)
        if not pending:
            return 0
        body = [{"instId": inst_id, "algoId": d["algoId"]} for d in pending if d.get("algoId")]
        if not body:
            return 0
        await self._t.request(
            "POST", "/api/v5/trade/cancel-algos",
            json_body=body, private=True, group="trade.cancel_algo_order",
        )
        return len(body)


__all__ = ["TradeEndpoints"]
