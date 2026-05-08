"""WS 下单 / 撤单 / 改单——复用 private WSConnection。

OKX 协议
--------
请求::

    {"id": "<client-correlation>", "op": "order", "args": [{"instId": ...}]}

响应（同步返回到同一连接）::

    {"id": "<same id>", "op": "order", "code": "0", "msg": "",
     "data": [{"sCode": "0", "ordId": "...", ...}]}

支持的 op:
- ``order`` / ``batch-orders``
- ``cancel-order`` / ``batch-cancel-orders``
- ``amend-order`` / ``batch-amend-orders``
- ``mass-cancel``

延迟比 REST 低（无 HTTP 层签名 + 持久连接），适合高频。

实现要点
--------
- 每个请求生成一个唯一 ``id``，注册一个 ``asyncio.Future``；
- 收到带相同 ``id`` 的响应（由 ``OKXWSClient._on_frame`` 路由进来），完成 Future；
- 默认 5s 超时，避免连接异常时调用方永久阻塞。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

from ..exceptions import OKXAPIError, OKXWSError
from ..models.trade import OrderRequest, OrderResult
from ..utils.logging import get_logger

if TYPE_CHECKING:
    from .client import OKXWSClient

log = get_logger(__name__)

_DEFAULT_TIMEOUT = 5.0


class WSTradeClient:
    """通过 WS private 连接下单。"""

    def __init__(self, ws_client: OKXWSClient) -> None:
        self._client = ws_client
        # id -> Future[response_frame]
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # 公开操作
    # ------------------------------------------------------------------

    async def place_order(self, req: OrderRequest, *, timeout: float = _DEFAULT_TIMEOUT) -> OrderResult:
        """WS 下单。失败抛 ``OKXAPIError``（与 REST trade.place_order 对齐）。"""
        body = req.model_dump(by_alias=True, exclude_none=True, mode="json")
        resp = await self._send_op("order", [body], timeout=timeout)
        data = resp.get("data") or []
        if not data:
            raise OKXAPIError(
                code=resp.get("code", "empty"),
                message=resp.get("msg", "ws place_order returned empty data"),
                endpoint="ws:order",
            )
        result = OrderResult.model_validate(data[0])
        if not result.ok:
            raise OKXAPIError(
                code=result.s_code, message=result.s_msg,
                data=data[0], endpoint="ws:order",
            )
        return result

    async def cancel_order(
        self,
        inst_id: str,
        *,
        ord_id: str | None = None,
        cl_ord_id: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> OrderResult:
        if not ord_id and not cl_ord_id:
            raise ValueError("either ord_id or cl_ord_id must be provided")
        arg: dict[str, str] = {"instId": inst_id}
        if ord_id:
            arg["ordId"] = ord_id
        if cl_ord_id:
            arg["clOrdId"] = cl_ord_id
        resp = await self._send_op("cancel-order", [arg], timeout=timeout)
        data = resp.get("data") or []
        if not data:
            raise OKXAPIError(
                code=resp.get("code", "empty"),
                message=resp.get("msg", "ws cancel-order empty"),
                endpoint="ws:cancel-order",
            )
        result = OrderResult.model_validate(data[0])
        if not result.ok:
            raise OKXAPIError(
                code=result.s_code, message=result.s_msg,
                data=data[0], endpoint="ws:cancel-order",
            )
        return result

    async def amend_order(
        self,
        inst_id: str,
        *,
        ord_id: str | None = None,
        cl_ord_id: str | None = None,
        new_sz: str | None = None,
        new_px: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> OrderResult:
        if not ord_id and not cl_ord_id:
            raise ValueError("either ord_id or cl_ord_id must be provided")
        if new_sz is None and new_px is None:
            raise ValueError("at least one of new_sz / new_px must be provided")
        arg: dict[str, str] = {"instId": inst_id}
        if ord_id:
            arg["ordId"] = ord_id
        if cl_ord_id:
            arg["clOrdId"] = cl_ord_id
        if new_sz is not None:
            arg["newSz"] = new_sz
        if new_px is not None:
            arg["newPx"] = new_px
        resp = await self._send_op("amend-order", [arg], timeout=timeout)
        data = resp.get("data") or []
        if not data:
            raise OKXAPIError(
                code=resp.get("code", "empty"),
                message=resp.get("msg", "ws amend-order empty"),
                endpoint="ws:amend-order",
            )
        result = OrderResult.model_validate(data[0])
        if not result.ok:
            raise OKXAPIError(
                code=result.s_code, message=result.s_msg,
                data=data[0], endpoint="ws:amend-order",
            )
        return result

    # ------------------------------------------------------------------
    # 响应路由（OKXWSClient._on_frame 调用）
    # ------------------------------------------------------------------

    def try_complete(self, frame: dict[str, Any]) -> bool:
        """若 frame 是我们发出的请求的响应，完成对应 Future 并返回 True。"""
        req_id = frame.get("id")
        if not req_id or req_id not in self._pending:
            return False
        fut = self._pending.pop(req_id)
        if not fut.done():
            fut.set_result(frame)
        return True

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _send_op(
        self,
        op: str,
        args: list[dict[str, Any]],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """发出一帧请求并等响应。OKX 顶层 ``code != "0"`` 抛 ``OKXWSError``。"""
        # 确保 private 连接已建立（WS trade 走 private endpoint）
        conn = await self._client._get_or_start("private")

        req_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = fut

        frame = {"id": req_id, "op": op, "args": args}
        try:
            await conn.send(frame)
            try:
                resp = await asyncio.wait_for(fut, timeout=timeout)
            except TimeoutError as e:
                raise OKXWSError(f"ws {op} timeout after {timeout}s") from e
        finally:
            self._pending.pop(req_id, None)

        # 顶层 code 校验（数据级失败留给上层 OrderResult 判断）
        code = str(resp.get("code", "0"))
        if code != "0":
            raise OKXWSError(f"ws {op} failed: code={code} msg={resp.get('msg')}")
        return resp


__all__ = ["WSTradeClient"]
