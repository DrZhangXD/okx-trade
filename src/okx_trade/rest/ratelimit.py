"""按 endpoint group 维护的令牌桶限频器（async）。

OKX REST 的 rate limit 不是全局一档，而是按接口分组（如 ``place_order`` 是
60 次/2s/UID，``candles`` 是 40 次/2s/IP）。因此设计：

- 每个 group 一个 ``TokenBucket``；
- ``RateLimiter.acquire(group)`` 在桶里取一枚令牌，没有就 ``await`` 等待补充；
- 命中 HTTP 429 时，``cool_down(group, seconds)`` 强制清空令牌并等待。

实现选择：
* 用 ``asyncio.Lock`` 串行化每个桶的状态更新，避免并发取令牌时的竞争；
* 用 ``loop.time()`` 做单调时钟，避免系统时间跳变影响补充计算；
* 不引入第三方限频库（aiolimiter 等）——本场景需求简单，自己写更可控。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """单个令牌桶。``capacity`` 个令牌、``period`` 秒填满。

    例如 OKX ``candles`` 接口是 40 次 / 2s → ``capacity=40, period=2``。
    """
    capacity: int
    period: float
    tokens: float = field(init=False)
    last_refill: float = field(init=False, default=0.0)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)

    @property
    def refill_rate(self) -> float:
        """每秒补充的令牌数。"""
        return self.capacity / self.period

    async def acquire(self, n: int = 1) -> None:
        """阻塞等待直到可以扣减 ``n`` 枚令牌。"""
        if n > self.capacity:
            raise ValueError(f"requested {n} tokens > capacity {self.capacity}")
        loop = asyncio.get_event_loop()
        async with self._lock:
            while True:
                now = loop.time()
                # 首次调用：初始化 last_refill
                if self.last_refill == 0:
                    self.last_refill = now
                # 按时间补充
                elapsed = now - self.last_refill
                if elapsed > 0:
                    self.tokens = min(
                        float(self.capacity),
                        self.tokens + elapsed * self.refill_rate,
                    )
                    self.last_refill = now

                if self.tokens >= n:
                    self.tokens -= n
                    return
                # 不够：算需要等多久
                deficit = n - self.tokens
                wait = deficit / self.refill_rate
                # 释放锁等待，醒来再 loop 一次
                await asyncio.sleep(wait)

    async def cool_down(self, seconds: float) -> None:
        """清空令牌并阻塞 ``seconds`` 秒——命中 429 后调用。"""
        async with self._lock:
            self.tokens = 0
            self.last_refill = asyncio.get_event_loop().time() + seconds
        await asyncio.sleep(seconds)


class RateLimiter:
    """按 group 维护多个 TokenBucket 的容器。"""

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}

    def register(self, group: str, capacity: int, period: float) -> None:
        """注册一个限频 group。重复注册同 group 会覆盖（便于测试调整参数）。"""
        self._buckets[group] = TokenBucket(capacity=capacity, period=period)

    def get(self, group: str) -> TokenBucket | None:
        return self._buckets.get(group)

    async def acquire(self, group: str, n: int = 1) -> None:
        """对指定 group 申请令牌。group 不存在时直接放行（视作无限制）。"""
        bucket = self._buckets.get(group)
        if bucket is None:
            return
        await bucket.acquire(n)

    async def cool_down(self, group: str, seconds: float) -> None:
        bucket = self._buckets.get(group)
        if bucket is None:
            return
        await bucket.cool_down(seconds)


# OKX v5 常见接口的限频默认值（capacity, period_sec）。
# 数据来源：https://www.okx.com/docs-v5/zh/#overview-rate-limits
# 第一阶段先覆盖会用到的，后续按需扩展。
DEFAULT_OKX_LIMITS: dict[str, tuple[int, float]] = {
    # 公共行情
    "market.candles": (40, 2.0),         # 40 次 / 2s / IP
    "market.history_candles": (20, 2.0),
    "market.tickers": (20, 2.0),
    "market.books": (40, 2.0),
    "market.trades": (100, 2.0),
    "public.instruments": (20, 2.0),
    "public.open_interest": (20, 2.0),
    "public.open_interest_history": (5, 2.0),   # rubik: 5 req/2s
    # 账户（需鉴权，UID 维度）
    "account.balance": (10, 2.0),
    "account.positions": (10, 2.0),
    "account.set_leverage": (20, 2.0),
    # 交易
    "trade.order": (60, 2.0),            # 下单 60 次 / 2s / UID
    "trade.cancel_order": (60, 2.0),
    "trade.amend_order": (60, 2.0),
    "trade.orders_pending": (60, 2.0),
    "trade.orders_history": (40, 2.0),
    "trade.order_query": (60, 2.0),
    "trade.fills": (60, 2.0),
    "trade.close_position": (20, 2.0),
    "trade.algo_orders": (20, 2.0),
    "trade.cancel_algo_order": (20, 2.0),
}


def build_default_limiter() -> RateLimiter:
    """按 ``DEFAULT_OKX_LIMITS`` 注册一个开箱即用的 RateLimiter。"""
    rl = RateLimiter()
    for group, (cap, period) in DEFAULT_OKX_LIMITS.items():
        rl.register(group, cap, period)
    return rl


__all__ = [
    "DEFAULT_OKX_LIMITS",
    "RateLimiter",
    "TokenBucket",
    "build_default_limiter",
]
