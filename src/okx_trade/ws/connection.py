"""单条 WebSocket 物理连接的生命周期管理。

职责
----
- 连接 OKX WS endpoint（公共/私有/business 三选一）；
- 私有/business 连接：握手后发 login 帧；
- 心跳：每 ``ping_interval`` 秒发 OKX 协议的 ``"ping"`` 文本，避免 30s 超时；
- 收消息：parse JSON 后调用 ``on_message`` 回调，把帧交给上层 dispatcher；
- 自动重连：异常断开后退避（1→2→5→10→30s 上限）；
- 重连后自动重发所有活跃订阅（``SubscriptionRegistry.snapshot()``）；
- 订阅幂等：同一 key 重复订阅只发一次帧（依赖外层 ``SubscriptionRegistry``）。

WSConnection 不持有 dispatcher——保持单一职责。上层（OKXWSClient）负责
把 ``on_message`` 桥接到 dispatcher。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from ..auth import build_ws_login_args
from ..exceptions import OKXSubscriptionError, OKXWSConnectionError
from ..utils.logging import get_logger
from .subscription import (
    SubscriptionKey,
    SubscriptionRegistry,
    make_subscribe_frame,
    make_unsubscribe_frame,
)

log = get_logger(__name__)

OnMessage = Callable[[dict[str, Any]], Awaitable[None]]
LoginCreds = tuple[str, str, str]  # (api_key, secret, passphrase)

_BACKOFF_SCHEDULE = (1.0, 2.0, 5.0, 10.0, 30.0)
_PING_INTERVAL_SEC = 25.0  # OKX 30s 静默断；25s 主动 ping 留余量


class WSConnection:
    """单条 OKX WebSocket 连接。"""

    def __init__(
        self,
        url: str,
        on_message: OnMessage,
        *,
        login_creds: LoginCreds | None = None,
        proxy: str | None = None,
        registry: SubscriptionRegistry | None = None,
    ) -> None:
        self.url = url
        self._on_message = on_message
        self._login_creds = login_creds
        self._proxy = proxy
        self.registry = registry if registry is not None else SubscriptionRegistry()

        self._ws: ClientConnection | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._connected_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._send_lock = asyncio.Lock()  # 同一连接同时只能有一个 send 在飞
        self._login_done_event = asyncio.Event()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._connected_event.is_set()

    async def start(self) -> None:
        """启动后台 run 循环。立即返回；可用 ``wait_connected`` 等首次连上。"""
        if self._run_task is not None:
            return
        self._stop_event.clear()
        self._run_task = asyncio.create_task(self._run(), name=f"ws-{self.url}")

    async def stop(self) -> None:
        """优雅关闭：通知 run 循环退出，关闭底层连接，等 task 结束。"""
        self._stop_event.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._run_task is not None:
            try:
                await asyncio.wait_for(self._run_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._run_task.cancel()
            self._run_task = None

    async def wait_connected(self, timeout: float = 10.0) -> None:
        """阻塞等待首次连接成功（含 login，如果需要）。超时抛 ``OKXWSConnectionError``。"""
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
            if self._login_creds is not None:
                await asyncio.wait_for(self._login_done_event.wait(), timeout=timeout)
        except TimeoutError as e:
            raise OKXWSConnectionError(f"connect timeout: {self.url}") from e

    async def send(self, frame: dict[str, Any]) -> None:
        """发送 JSON 帧。等待已连接后再发。"""
        await self._connected_event.wait()
        assert self._ws is not None
        async with self._send_lock:
            await self._ws.send(json.dumps(frame))

    async def subscribe(self, keys: list[SubscriptionKey]) -> None:
        """订阅一组 key。仅注册引用计数后首次出现的才发包。"""
        new_keys = [k for k in keys if self.registry.add(k)]
        if not new_keys:
            return
        if self.is_connected:
            await self.send(make_subscribe_frame(new_keys))
        # 未连接时先注册，连上后由 _resubscribe_all 一次性发出

    async def unsubscribe(self, keys: list[SubscriptionKey]) -> None:
        """取消订阅。引用计数归零的才真正发 unsubscribe 帧。"""
        to_send = [k for k in keys if self.registry.remove(k)]
        if not to_send:
            return
        if self.is_connected:
            await self.send(make_unsubscribe_frame(to_send))

    # ------------------------------------------------------------------
    # 内部：主循环
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """外层 while-true：连接 → 跑到断 → 退避重连。stop_event 时退出。"""
        attempt = 0
        while not self._stop_event.is_set():
            try:
                await self._run_once()
                attempt = 0  # 成功完成一轮则重置退避
            except (
                ConnectionClosed,
                OKXWSConnectionError,
                OSError,
                asyncio.TimeoutError,
            ) as e:
                if self._stop_event.is_set():
                    break
                wait = _BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)]
                log.warning(
                    "ws_disconnected",
                    url=self.url, error=type(e).__name__,
                    attempt=attempt + 1, backoff_sec=wait,
                )
                attempt += 1
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                except TimeoutError:
                    pass
            except Exception as e:
                # 未预期的异常也走重连，但记 error
                log.error("ws_unexpected_error", url=self.url, error=str(e))
                attempt += 1
                if attempt > 5:
                    raise
                await asyncio.sleep(_BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)])

    async def _run_once(self) -> None:
        """单次连接生命周期：建连 → login → 重订阅 → 收消息 + 心跳 → 退出。"""
        self._connected_event.clear()
        self._login_done_event.clear()

        kwargs: dict[str, Any] = {}
        if self._proxy:
            kwargs["proxy"] = self._proxy

        async with connect(self.url, **kwargs) as ws:
            self._ws = ws
            self._connected_event.set()
            log.info("ws_connected", url=self.url)

            # login（仅 private/business）
            if self._login_creds is not None:
                await self._do_login(ws)

            # 重订阅历史 active subscriptions
            await self._resubscribe_all()

            # 启动心跳与 reader 两个 task
            heartbeat = asyncio.create_task(self._heartbeat(ws), name="ws-heartbeat")
            try:
                await self._read_loop(ws)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except (asyncio.CancelledError, Exception):
                    pass
                self._connected_event.clear()
                self._ws = None

    async def _do_login(self, ws: ClientConnection) -> None:
        """发 login 帧并等待响应。失败抛 ``OKXWSConnectionError``。"""
        assert self._login_creds is not None
        api_key, secret, passphrase = self._login_creds
        login_args = build_ws_login_args(api_key, secret, passphrase)
        frame = {"op": "login", "args": [login_args]}
        await ws.send(json.dumps(frame))

        # 等 login 响应（OKX 返回 ``{"event": "login", "code": "0"}``）
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        except TimeoutError as e:
            raise OKXWSConnectionError("ws login response timeout") from e
        try:
            resp = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise OKXWSConnectionError(f"ws login bad response: {raw!r}") from e
        if resp.get("event") != "login" or str(resp.get("code", "0")) != "0":
            raise OKXWSConnectionError(f"ws login failed: {resp}")
        log.info("ws_login_ok", url=self.url)
        self._login_done_event.set()

    async def _resubscribe_all(self) -> None:
        """重连后重发所有 active 订阅。"""
        snap = self.registry.snapshot()
        if not snap:
            return
        await self.send(make_subscribe_frame(snap))
        log.info("ws_resubscribed", url=self.url, count=len(snap))

    async def _heartbeat(self, ws: ClientConnection) -> None:
        """OKX 协议：直接发文本 ``"ping"``，服务端回 ``"pong"``。"""
        try:
            while True:
                await asyncio.sleep(_PING_INTERVAL_SEC)
                await ws.send("ping")
        except (asyncio.CancelledError, ConnectionClosed):
            return

    async def _read_loop(self, ws: ClientConnection) -> None:
        """收消息分发。OKX 也会推非 JSON 文本（``"pong"``），跳过即可。"""
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if raw == "pong":
                continue
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("ws_bad_frame", raw=str(raw)[:200])
                continue

            # 订阅响应（OKX: {"event":"subscribe","arg":{...}}）
            if frame.get("event") == "error":
                # 单条订阅失败不应炸整个连接；记录后继续
                log.error(
                    "ws_subscribe_error",
                    code=frame.get("code"), msg=frame.get("msg"), arg=frame.get("arg"),
                )
                continue
            if "event" in frame and "data" not in frame:
                # subscribe/unsubscribe ack 等控制帧
                continue

            try:
                await self._on_message(frame)
            except Exception as e:
                log.error("ws_on_message_error", error=str(e))


__all__ = [
    "OKXSubscriptionError",
    "OnMessage",
    "WSConnection",
]
