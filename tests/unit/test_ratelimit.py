"""TokenBucket / RateLimiter 测试。

用 ``asyncio.get_event_loop().time()`` 自然推进——这些测试本身耗时不会超过 1s。
不引入 freezegun（freezegun 对 asyncio loop time 支持不稳定）。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from okx_trade.rest.ratelimit import RateLimiter, TokenBucket, build_default_limiter


class TestTokenBucket:
    async def test_initial_full(self) -> None:
        b = TokenBucket(capacity=5, period=1.0)
        assert b.tokens == 5

    async def test_acquire_does_not_block_when_available(self) -> None:
        b = TokenBucket(capacity=3, period=10.0)
        start = time.monotonic()
        await b.acquire(1)
        await b.acquire(1)
        await b.acquire(1)
        elapsed = time.monotonic() - start
        assert elapsed < 0.05  # 不应该有任何等待

    async def test_acquire_blocks_when_empty(self) -> None:
        b = TokenBucket(capacity=2, period=0.2)  # 每秒补 10 枚
        await b.acquire(1)
        await b.acquire(1)
        # 桶空了，再取 1 枚要等约 0.1s（10 枚/s）
        start = time.monotonic()
        await b.acquire(1)
        elapsed = time.monotonic() - start
        assert 0.05 < elapsed < 0.3, f"expected ~0.1s wait, got {elapsed:.3f}s"

    async def test_request_more_than_capacity_raises(self) -> None:
        b = TokenBucket(capacity=2, period=1.0)
        with pytest.raises(ValueError, match="> capacity"):
            await b.acquire(3)

    async def test_refill_over_time(self) -> None:
        b = TokenBucket(capacity=10, period=0.1)  # 100 枚/s
        # 取空
        for _ in range(10):
            await b.acquire(1)
        # 等 0.05s，应补到约 5 枚
        await asyncio.sleep(0.05)
        # 再取 4 枚不应阻塞
        start = time.monotonic()
        for _ in range(4):
            await b.acquire(1)
        elapsed = time.monotonic() - start
        assert elapsed < 0.05


class TestRateLimiter:
    async def test_unregistered_group_is_no_op(self) -> None:
        rl = RateLimiter()
        # 未注册的 group 直接放行，不抛异常
        start = time.monotonic()
        await rl.acquire("nonexistent")
        await rl.cool_down("nonexistent", 1.0)
        elapsed = time.monotonic() - start
        assert elapsed < 0.05

    async def test_register_and_acquire(self) -> None:
        rl = RateLimiter()
        rl.register("trade.order", capacity=2, period=1.0)
        assert rl.get("trade.order") is not None
        await rl.acquire("trade.order")
        await rl.acquire("trade.order")
        # 第三次应触发等待——但我们只测 "立即返回" 不阻塞过久
        # 跳过深度测试，前面已有 TokenBucket 自身的测试

    def test_default_limiter_has_common_groups(self) -> None:
        rl = build_default_limiter()
        for g in ("trade.order", "market.candles", "account.balance"):
            assert rl.get(g) is not None, f"missing default group {g}"
