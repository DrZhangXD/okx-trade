"""WS 消息分发：把 OKX 推送的数据按订阅 key 路由到 handler / async queue。

OKX 推送帧格式：
    {"arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"}, "data": [...]}

分发器维护两套表：
- ``_callbacks[SubscriptionKey] -> list[async_handler]``：低层 callback；
- ``_queues[SubscriptionKey] -> list[asyncio.Queue]``：高层 async iterator 用。

收到推送时同时投递给两套表（同一订阅可同时被 callback 和 iterator 消费）。
错误处理：单个 callback 抛异常不应影响其他订阅者，logger 记录后继续。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from ..utils.logging import get_logger
from .subscription import SubscriptionKey

log = get_logger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class Dispatcher:
    """订阅消息路由器。线程不安全（asyncio task 内独占）。"""

    def __init__(self, queue_maxsize: int = 1000) -> None:
        self._callbacks: dict[SubscriptionKey, list[EventHandler]] = {}
        self._queues: dict[SubscriptionKey, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._queue_maxsize = queue_maxsize

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def add_callback(self, key: SubscriptionKey, handler: EventHandler) -> None:
        self._callbacks.setdefault(key, []).append(handler)

    def remove_callback(self, key: SubscriptionKey, handler: EventHandler) -> None:
        handlers = self._callbacks.get(key)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            return
        if not handlers:
            self._callbacks.pop(key, None)

    def make_queue(self, key: SubscriptionKey) -> asyncio.Queue[dict[str, Any]]:
        """创建并注册一个新 queue（async iterator 用）。"""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._queues.setdefault(key, []).append(q)
        return q

    def drop_queue(self, key: SubscriptionKey, q: asyncio.Queue[dict[str, Any]]) -> None:
        queues = self._queues.get(key)
        if not queues:
            return
        try:
            queues.remove(q)
        except ValueError:
            pass
        if not queues:
            self._queues.pop(key, None)

    def has_consumers(self, key: SubscriptionKey) -> bool:
        return bool(self._callbacks.get(key)) or bool(self._queues.get(key))

    # ------------------------------------------------------------------
    # 投递
    # ------------------------------------------------------------------

    async def dispatch(self, frame: dict[str, Any]) -> None:
        """按帧的 ``arg`` 字段路由。无 ``arg``（如 pong / login response）忽略。"""
        arg = frame.get("arg")
        if not arg:
            return
        channel = arg.get("channel")
        if not channel:
            return
        # filters: 除 channel 外的所有键
        filters = {k: v for k, v in arg.items() if k != "channel"}
        key = SubscriptionKey.make(channel, **filters)

        # 投递到 callbacks
        for handler in list(self._callbacks.get(key, ())):
            try:
                await handler(frame)
            except Exception as exc:
                log.error("ws_callback_error", channel=channel, error=str(exc))

        # 投递到 queues（满了就丢最老的，保留最新事件）
        for q in list(self._queues.get(key, ())):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                log.warning("ws_queue_overflow", channel=channel, filters=filters)
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                log.warning("ws_queue_full", channel=channel)

    # ------------------------------------------------------------------
    # async iterator helper
    # ------------------------------------------------------------------

    async def iter_events(self, key: SubscriptionKey) -> AsyncIterator[dict[str, Any]]:
        """返回一个绑定到 key 的 async iterator。退出时自动 drop queue。

        典型用法（由 OKXWSClient.subscribe 调用）：

            async for event in dispatcher.iter_events(key):
                ...
        """
        q = self.make_queue(key)
        try:
            while True:
                frame = await q.get()
                yield frame
        finally:
            self.drop_queue(key, q)


__all__ = ["Dispatcher", "EventHandler"]
