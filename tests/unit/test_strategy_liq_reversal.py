"""Liq Reversal 纯函数单测。"""
from __future__ import annotations

from collections import deque

import pytest

from okx_trade.strategies.liq_reversal import (
    LiqEvent,
    liq_reversal_decision,
    liq_zscore,
    parse_okx_liq_frame,
)


def _ev(ts_ms: int, side: str, sz_usd: float, inst: str = "BTC-USDT-SWAP") -> LiqEvent:
    return LiqEvent(ts_ms=ts_ms, inst_id=inst, side=side, sz_usd=sz_usd)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# liq_zscore
# ---------------------------------------------------------------------------


class TestLiqZScore:
    def test_empty_returns_zero(self) -> None:
        z, dom = liq_zscore(deque(), window_sec=60, history_sec=3600, now_ms=10_000_000)
        assert z == 0.0
        assert dom is None

    def test_basic_spike(self) -> None:
        # 历史每分钟约 100 USD 强平；当前 1 分钟 10000 USD → z 应非常高
        events: list[LiqEvent] = []
        # 历史：3540 秒（59 分钟），每 60 秒一条 100 USD
        for sec in range(60, 3600, 60):
            events.append(_ev(ts_ms=(3600 - sec) * 1000, side="sell", sz_usd=100.0))
        # 当前 60 秒窗口：1 条大额 10000 USD（at ts=3590s）
        events.append(_ev(ts_ms=3590 * 1000, side="sell", sz_usd=10000.0))
        events.sort(key=lambda e: e.ts_ms)
        z, dom = liq_zscore(events, window_sec=60, history_sec=3600, now_ms=3600 * 1000)
        assert z > 5.0
        assert dom == "sell"

    def test_dominant_buy_when_more_buys(self) -> None:
        events = [
            _ev(ts_ms=9_950_000, side="buy", sz_usd=200.0),
            _ev(ts_ms=9_960_000, side="sell", sz_usd=50.0),
            _ev(ts_ms=9_980_000, side="buy", sz_usd=100.0),
        ]
        # 加点历史避免 std=0 / 不足
        for sec in range(60, 3600, 60):
            events.append(_ev(ts_ms=10_000_000 - sec * 1000, side="sell", sz_usd=10.0))
        events.sort(key=lambda e: e.ts_ms)
        _, dom = liq_zscore(events, window_sec=60, history_sec=3600, now_ms=10_000_000)
        assert dom == "buy"

    def test_no_history_returns_zero_z(self) -> None:
        # 当前窗口有事件但没历史 → z=0（不能推断异常）
        events = [_ev(ts_ms=9_999_000, side="buy", sz_usd=10000.0)]
        z, dom = liq_zscore(events, window_sec=60, history_sec=3600, now_ms=10_000_000)
        assert z == 0.0
        assert dom == "buy"  # dominant 仍可识别

    def test_constant_history_returns_zero_z(self) -> None:
        # 每个 history bucket 都精确放一条 100 USD → std=0 → z=0
        # n_buckets = (3600-60)/60 = 59；bucket k 覆盖 ts ∈ [3540 - (k+1)*60, 3540 - k*60) s
        # 在每个 bucket 中点放一条
        events = []
        for k in range(59):
            ts_s = 3540 - k * 60 - 30  # 居中
            events.append(_ev(ts_ms=ts_s * 1000, side="sell", sz_usd=100.0))
        # 当前窗口：1 条
        events.append(_ev(ts_ms=3580 * 1000, side="sell", sz_usd=100.0))
        events.sort(key=lambda e: e.ts_ms)
        z, _ = liq_zscore(events, window_sec=60, history_sec=3600, now_ms=3600 * 1000)
        assert z == 0.0

    def test_invalid_params_returns_zero(self) -> None:
        # history_sec <= 2*window_sec 不足以建立分布
        events = [_ev(1, "buy", 100.0)]
        z, dom = liq_zscore(events, window_sec=60, history_sec=60, now_ms=1000)
        assert z == 0.0
        assert dom is None


# ---------------------------------------------------------------------------
# liq_reversal_decision
# ---------------------------------------------------------------------------


class TestLiqReversalDecision:
    @pytest.mark.parametrize("z,dominant,expected", [
        (5.0, "sell", "long"),     # 多头被强平 → 做多反弹
        (5.0, "buy", "short"),     # 空头被强平 → 做空回归
        (4.0, "sell", "long"),
        (3.01, "buy", "short"),
        (2.99, "sell", None),      # z 不足
        (10.0, None, None),        # dominant 缺失
        (0.0, "buy", None),
    ])
    def test_decision_matrix(self, z: float, dominant: str | None,
                             expected: str | None) -> None:
        got = liq_reversal_decision(
            z, dominant,  # type: ignore[arg-type]
            threshold=3.0,
        )
        assert got == expected

    def test_custom_threshold(self) -> None:
        assert liq_reversal_decision(2.5, "sell", threshold=2.0) == "long"
        assert liq_reversal_decision(2.5, "sell", threshold=3.0) is None


# ---------------------------------------------------------------------------
# parse_okx_liq_frame
# ---------------------------------------------------------------------------


class TestParseOKXLiqFrame:
    def test_basic_parse(self) -> None:
        frame = {
            "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "details": [
                        {"side": "sell", "bkPx": "60000", "sz": "10",
                         "bkLoss": "0", "ts": "1697000000000"},
                        {"side": "buy", "bkPx": "60100", "sz": "5",
                         "bkLoss": "0", "ts": "1697000001000"},
                    ],
                },
            ],
        }
        evs = parse_okx_liq_frame(frame, perp_ct_val={"BTC-USDT-SWAP": 0.01})
        assert len(evs) == 2
        # 60000 * 10 * 0.01 = 6000 USD
        assert evs[0].sz_usd == pytest.approx(6000.0)
        assert evs[0].side == "sell"
        assert evs[1].sz_usd == pytest.approx(60100 * 5 * 0.01)
        assert evs[1].side == "buy"
        assert evs[0].ts_ms < evs[1].ts_ms  # ascending

    def test_wrong_channel_returns_empty(self) -> None:
        frame = {"arg": {"channel": "trades"}, "data": [{}]}
        assert parse_okx_liq_frame(frame) == []

    def test_skips_invalid_details(self) -> None:
        frame = {
            "arg": {"channel": "liquidation-orders"},
            "data": [{
                "instId": "BTC-USDT-SWAP",
                "details": [
                    {"side": "sell", "bkPx": "0", "sz": "10", "ts": "1"},     # bkPx=0
                    {"side": "buy", "bkPx": "60000", "sz": "0", "ts": "1"},   # sz=0
                    {"side": "weird", "bkPx": "1", "sz": "1", "ts": "1"},     # bad side
                    {"bkPx": "1"},                                            # missing fields
                    {"side": "buy", "bkPx": "60000", "sz": "1", "ts": "100"}, # 合法
                ],
            }],
        }
        evs = parse_okx_liq_frame(frame, perp_ct_val={"BTC-USDT-SWAP": 1.0})
        assert len(evs) == 1
        assert evs[0].sz_usd == 60000.0

    def test_default_ct_val_one_when_missing(self) -> None:
        # 没传 perp_ct_val → fallback ct_val=1
        frame = {
            "arg": {"channel": "liquidation-orders"},
            "data": [{
                "instId": "FOO-USDT-SWAP",
                "details": [{"side": "buy", "bkPx": "100", "sz": "2", "ts": "1"}],
            }],
        }
        evs = parse_okx_liq_frame(frame)
        assert len(evs) == 1
        assert evs[0].sz_usd == 200.0
