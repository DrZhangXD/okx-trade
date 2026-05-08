"""WS 订阅注册：幂等订阅 + 引用计数 + 重连恢复。

- 同一 ``(channel, **filters)`` 重复订阅，只引用计数 +1，不重复发包；
- ``unsubscribe`` 减到 0 才真正发 ``op=unsubscribe`` 帧；
- 重连后 ``snapshot()`` 返回所有活跃订阅，连接层用它一次性重新发订阅帧。

OKX 订阅帧格式：
    {"op": "subscribe", "args": [{"channel": "books5", "instId": "BTC-USDT-SWAP"}, ...]}

``filters`` 是 channel 之外的所有维度——instId / instType / instFamily 等。不同 channel
的 filters 不同，本模块不做 schema 校验，直接当 dict 透传。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SubscriptionKey:
    """订阅唯一键：channel + filters 的不可变快照。"""

    channel: str
    # 用 tuple of (k, v) 表示 dict 的 frozen 形态，便于做 hashable
    filter_items: tuple[tuple[str, str], ...]

    @classmethod
    def make(cls, channel: str, **filters: str) -> SubscriptionKey:
        return cls(channel=channel, filter_items=tuple(sorted(filters.items())))

    @property
    def filters(self) -> dict[str, str]:
        return dict(self.filter_items)

    def to_arg(self) -> dict[str, str]:
        """转成 OKX 订阅帧 args[i] 的格式。"""
        return {"channel": self.channel, **self.filters}


@dataclass
class SubscriptionRegistry:
    """订阅引用计数表。所有方法非异步——状态变更瞬时完成，由调用方在 lock 内调用。"""

    _refs: dict[SubscriptionKey, int] = field(default_factory=dict)

    def add(self, key: SubscriptionKey) -> bool:
        """引用计数 +1。返回是否是首次订阅（True 时连接层才需发 subscribe 帧）。"""
        new_count = self._refs.get(key, 0) + 1
        self._refs[key] = new_count
        return new_count == 1

    def remove(self, key: SubscriptionKey) -> bool:
        """引用计数 -1。返回是否计数归零（True 时连接层才需发 unsubscribe 帧）。"""
        cur = self._refs.get(key, 0)
        if cur <= 1:
            self._refs.pop(key, None)
            return cur == 1
        self._refs[key] = cur - 1
        return False

    def has(self, key: SubscriptionKey) -> bool:
        return key in self._refs

    def count(self, key: SubscriptionKey) -> int:
        return self._refs.get(key, 0)

    def snapshot(self) -> list[SubscriptionKey]:
        """返回所有活跃订阅（重连后用这个一次重发）。"""
        return list(self._refs.keys())

    def clear(self) -> None:
        self._refs.clear()


# ---------------------------------------------------------------------------
# 帧构造助手
# ---------------------------------------------------------------------------

def make_subscribe_frame(keys: Iterable[SubscriptionKey]) -> dict[str, Any]:
    return {"op": "subscribe", "args": [k.to_arg() for k in keys]}


def make_unsubscribe_frame(keys: Iterable[SubscriptionKey]) -> dict[str, Any]:
    return {"op": "unsubscribe", "args": [k.to_arg() for k in keys]}


__all__ = [
    "SubscriptionKey",
    "SubscriptionRegistry",
    "make_subscribe_frame",
    "make_unsubscribe_frame",
]
