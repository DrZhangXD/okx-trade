"""Dispatcher 路由与 async iterator 测试。"""
from __future__ import annotations

import asyncio
from typing import Any

from okx_trade.ws.dispatcher import Dispatcher
from okx_trade.ws.subscription import SubscriptionKey


def _frame(channel: str, inst_id: str, data: list[dict[str, Any]]) -> dict[str, Any]:
    return {"arg": {"channel": channel, "instId": inst_id}, "data": data}


class TestCallback:
    async def test_dispatched_to_callback(self) -> None:
        d = Dispatcher()
        k = SubscriptionKey.make("books5", instId="BTC-USDT")
        received: list[dict[str, Any]] = []

        async def handler(frame: dict[str, Any]) -> None:
            received.append(frame)

        d.add_callback(k, handler)
        await d.dispatch(_frame("books5", "BTC-USDT", [{"a": 1}]))

        assert len(received) == 1
        assert received[0]["data"] == [{"a": 1}]

    async def test_unmatched_frame_dropped(self) -> None:
        d = Dispatcher()
        k = SubscriptionKey.make("books5", instId="BTC-USDT")
        received: list[dict[str, Any]] = []

        async def handler(frame: dict[str, Any]) -> None:
            received.append(frame)

        d.add_callback(k, handler)
        # 不同 instId 的帧不应被路由到此 handler
        await d.dispatch(_frame("books5", "ETH-USDT", [{"a": 1}]))
        assert received == []

    async def test_multiple_callbacks_same_key(self) -> None:
        d = Dispatcher()
        k = SubscriptionKey.make("tickers", instId="BTC-USDT")
        a: list[Any] = []
        b: list[Any] = []

        async def h1(f: Any) -> None: a.append(f)
        async def h2(f: Any) -> None: b.append(f)

        d.add_callback(k, h1)
        d.add_callback(k, h2)
        await d.dispatch(_frame("tickers", "BTC-USDT", [{}]))

        assert len(a) == 1 and len(b) == 1

    async def test_callback_exception_does_not_block_others(self) -> None:
        d = Dispatcher()
        k = SubscriptionKey.make("tickers", instId="BTC-USDT")
        good: list[Any] = []

        async def bad(_: Any) -> None: raise RuntimeError("boom")
        async def good_h(f: Any) -> None: good.append(f)

        d.add_callback(k, bad)
        d.add_callback(k, good_h)
        await d.dispatch(_frame("tickers", "BTC-USDT", [{}]))
        assert len(good) == 1

    async def test_remove_callback(self) -> None:
        d = Dispatcher()
        k = SubscriptionKey.make("tickers", instId="X")
        received: list[Any] = []

        async def h(f: Any) -> None: received.append(f)

        d.add_callback(k, h)
        d.remove_callback(k, h)
        await d.dispatch(_frame("tickers", "X", [{}]))
        assert received == []
        assert not d.has_consumers(k)


class TestQueueAndIterator:
    async def test_queue_receives_frame(self) -> None:
        d = Dispatcher()
        k = SubscriptionKey.make("tickers", instId="BTC-USDT")
        q = d.make_queue(k)
        await d.dispatch(_frame("tickers", "BTC-USDT", [{"x": 1}]))
        frame = await asyncio.wait_for(q.get(), timeout=0.5)
        assert frame["data"] == [{"x": 1}]

    async def test_iter_events(self) -> None:
        d = Dispatcher()
        k = SubscriptionKey.make("tickers", instId="BTC-USDT")
        seen: list[Any] = []

        async def consumer() -> None:
            async for event in d.iter_events(k):
                seen.append(event)
                if len(seen) == 2:
                    break

        task = asyncio.create_task(consumer())
        # 让 consumer 注册 queue
        await asyncio.sleep(0)
        await d.dispatch(_frame("tickers", "BTC-USDT", [{"x": 1}]))
        await d.dispatch(_frame("tickers", "BTC-USDT", [{"x": 2}]))
        await asyncio.wait_for(task, timeout=1.0)
        assert seen[0]["data"] == [{"x": 1}]
        assert seen[1]["data"] == [{"x": 2}]
        # break 后 generator 在 GC 时关闭——让 event loop 跑一次触发 finally
        for _ in range(3):
            await asyncio.sleep(0)
        # 退出时 queue 自动 drop（最终一致即可）
        assert not d.has_consumers(k)

    async def test_queue_overflow_drops_oldest(self) -> None:
        d = Dispatcher(queue_maxsize=2)
        k = SubscriptionKey.make("tickers", instId="X")
        q = d.make_queue(k)
        # 投 3 条，capacity=2，应该只剩最新 2 条
        for i in range(3):
            await d.dispatch(_frame("tickers", "X", [{"i": i}]))
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        assert len(items) == 2
        assert items[0]["data"] == [{"i": 1}]
        assert items[1]["data"] == [{"i": 2}]


class TestEdgeCases:
    async def test_no_arg_field_ignored(self) -> None:
        d = Dispatcher()
        await d.dispatch({"event": "pong"})  # 不应崩

    async def test_no_channel_field_ignored(self) -> None:
        d = Dispatcher()
        await d.dispatch({"arg": {"instId": "X"}, "data": []})
