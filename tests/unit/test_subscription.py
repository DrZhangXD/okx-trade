"""SubscriptionRegistry 与帧构造测试。"""
from __future__ import annotations

from okx_trade.ws.subscription import (
    SubscriptionKey,
    SubscriptionRegistry,
    make_subscribe_frame,
    make_unsubscribe_frame,
)


class TestSubscriptionKey:
    def test_make_normalizes_filter_order(self) -> None:
        a = SubscriptionKey.make("books5", instId="BTC-USDT-SWAP", instType="SWAP")
        b = SubscriptionKey.make("books5", instType="SWAP", instId="BTC-USDT-SWAP")
        # filter 顺序无关，hashable，相等
        assert a == b
        assert hash(a) == hash(b)

    def test_to_arg_round_trip(self) -> None:
        k = SubscriptionKey.make("tickers", instId="BTC-USDT")
        assert k.to_arg() == {"channel": "tickers", "instId": "BTC-USDT"}


class TestRegistry:
    def test_first_add_returns_true(self) -> None:
        r = SubscriptionRegistry()
        k = SubscriptionKey.make("books5", instId="BTC-USDT")
        assert r.add(k) is True

    def test_subsequent_add_returns_false(self) -> None:
        r = SubscriptionRegistry()
        k = SubscriptionKey.make("books5", instId="BTC-USDT")
        r.add(k)
        assert r.add(k) is False
        assert r.count(k) == 2

    def test_remove_when_count_one_returns_true(self) -> None:
        r = SubscriptionRegistry()
        k = SubscriptionKey.make("books5", instId="BTC-USDT")
        r.add(k)
        assert r.remove(k) is True
        assert not r.has(k)

    def test_remove_decrements_only(self) -> None:
        r = SubscriptionRegistry()
        k = SubscriptionKey.make("books5", instId="BTC-USDT")
        r.add(k)
        r.add(k)
        # 第一次移除：还有引用，不返回 True
        assert r.remove(k) is False
        assert r.count(k) == 1
        # 第二次移除：归零，返回 True
        assert r.remove(k) is True
        assert not r.has(k)

    def test_remove_nonexistent_is_safe(self) -> None:
        r = SubscriptionRegistry()
        k = SubscriptionKey.make("books5", instId="X")
        assert r.remove(k) is False  # count 0 → False

    def test_snapshot_returns_active_keys(self) -> None:
        r = SubscriptionRegistry()
        k1 = SubscriptionKey.make("books5", instId="A")
        k2 = SubscriptionKey.make("tickers", instId="B")
        r.add(k1)
        r.add(k2)
        r.add(k2)  # 多次引用不影响快照
        snap = r.snapshot()
        assert set(snap) == {k1, k2}


class TestFrames:
    def test_subscribe_frame_shape(self) -> None:
        keys = [
            SubscriptionKey.make("books5", instId="BTC-USDT"),
            SubscriptionKey.make("tickers", instId="ETH-USDT"),
        ]
        frame = make_subscribe_frame(keys)
        assert frame["op"] == "subscribe"
        assert len(frame["args"]) == 2
        assert frame["args"][0]["channel"] == "books5"

    def test_unsubscribe_frame_shape(self) -> None:
        keys = [SubscriptionKey.make("books5", instId="BTC-USDT")]
        frame = make_unsubscribe_frame(keys)
        assert frame["op"] == "unsubscribe"
