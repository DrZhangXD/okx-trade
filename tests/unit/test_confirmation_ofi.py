"""OFI 门控单测。"""
from __future__ import annotations

from collections import deque

import pytest

from okx_trade.strategies.confirmation.ofi import (
    OFIFilter,
    TradeFlow,
    ofi_filter_decision,
    ofi_score,
)


def _t(ts_ms: int, side: str, sz: float) -> TradeFlow:
    return TradeFlow(ts_ms=ts_ms, side=side, sz=sz)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ofi_score
# ---------------------------------------------------------------------------


class TestOFIScore:
    def test_empty_returns_zero(self) -> None:
        assert ofi_score(deque(), window_ms=1000, now_ms=10_000) == 0.0

    def test_all_buy_returns_one(self) -> None:
        trades = [_t(9_500, "buy", 1.0), _t(9_800, "buy", 2.0)]
        assert ofi_score(trades, window_ms=1000, now_ms=10_000) == 1.0

    def test_all_sell_returns_minus_one(self) -> None:
        trades = [_t(9_500, "sell", 1.0), _t(9_800, "sell", 2.0)]
        assert ofi_score(trades, window_ms=1000, now_ms=10_000) == -1.0

    def test_balanced_returns_zero(self) -> None:
        trades = [_t(9_500, "buy", 1.0), _t(9_700, "sell", 1.0)]
        assert ofi_score(trades, window_ms=1000, now_ms=10_000) == 0.0

    def test_skews_buy(self) -> None:
        # buy=3, sell=1 → (3-1)/(3+1) = 0.5
        trades = [
            _t(9_500, "buy", 2.0),
            _t(9_700, "sell", 1.0),
            _t(9_800, "buy", 1.0),
        ]
        assert ofi_score(trades, window_ms=1000, now_ms=10_000) == 0.5

    def test_filters_out_window(self) -> None:
        # 老数据（ts < cutoff = now - window）应忽略
        trades = [
            _t(1_000, "sell", 100.0),  # 老数据，不算
            _t(9_500, "buy", 1.0),
            _t(9_800, "buy", 1.0),
        ]
        # 窗口 [9_000, 10_000]
        assert ofi_score(trades, window_ms=1000, now_ms=10_000) == 1.0

    def test_window_boundary_inclusive_lower(self) -> None:
        # ts == cutoff 应包含（>= cutoff）
        trades = [_t(9_000, "buy", 1.0)]
        assert ofi_score(trades, window_ms=1000, now_ms=10_000) == 1.0

    def test_score_in_range(self) -> None:
        # 任意输入结果都应在 [-1, 1]
        trades = [_t(i * 100, "buy" if i % 3 else "sell", float(i + 1))
                  for i in range(20)]
        s = ofi_score(trades, window_ms=10_000, now_ms=2_000)
        assert -1.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# ofi_filter_decision
# ---------------------------------------------------------------------------


class TestOFIFilterDecision:
    @pytest.mark.parametrize("ofi,direction,expected", [
        # long 同向（OFI 正且 >= same_dir_threshold=0.1）
        (0.5, "long", "pass"),
        (0.1, "long", "pass"),
        # long 弱反向 / 中性（aligned ∈ [-0.3, 0.1)）
        (0.0, "long", "scale_50"),
        (-0.2, "long", "scale_50"),
        (0.05, "long", "scale_50"),
        # long 强反向（aligned < -0.3）
        (-0.5, "long", "skip"),
        (-0.4, "long", "skip"),
        # short 同向（OFI 负且 -ofi >= 0.1）
        (-0.5, "short", "pass"),
        (-0.1, "short", "pass"),
        # short 弱反向（aligned ∈ [-0.3, 0.1)）
        (0.0, "short", "scale_50"),
        (0.2, "short", "scale_50"),
        # short 强反向
        (0.5, "short", "skip"),
    ])
    def test_decision_matrix(self, ofi: float, direction: str,
                             expected: str) -> None:
        got = ofi_filter_decision(ofi, direction)  # type: ignore[arg-type]
        assert got == expected

    def test_custom_thresholds(self) -> None:
        # 把同向阈值放宽到 0，反向阈值收紧到 -0.5
        assert ofi_filter_decision(
            0.0, "long", same_dir_threshold=0.0, counter_dir_threshold=-0.5,
        ) == "pass"
        assert ofi_filter_decision(
            -0.4, "long", same_dir_threshold=0.0, counter_dir_threshold=-0.5,
        ) == "scale_50"
        assert ofi_filter_decision(
            -0.6, "long", same_dir_threshold=0.0, counter_dir_threshold=-0.5,
        ) == "skip"


# ---------------------------------------------------------------------------
# OFIFilter (stateful)
# ---------------------------------------------------------------------------


class TestOFIFilter:
    def test_evaluate_end_to_end(self) -> None:
        f = OFIFilter(window_ms=1000)
        f.update(_t(9_500, "buy", 1.0))
        f.update(_t(9_700, "buy", 2.0))
        f.update(_t(9_800, "sell", 0.5))
        # buy=3, sell=0.5 → (3-0.5)/3.5 ≈ 0.714
        assert f.evaluate("long", now_ms=10_000) == "pass"
        assert f.evaluate("short", now_ms=10_000) == "skip"

    def test_empty_buffer_neutral(self) -> None:
        f = OFIFilter(window_ms=1000)
        assert f.score(now_ms=10_000) == 0.0
        # 中性 → long 走 scale_50（aligned=0 ∈ [-0.3, 0.1)）
        assert f.evaluate("long", now_ms=10_000) == "scale_50"

    def test_maxlen_drop_old(self) -> None:
        # maxlen=2，append 第 3 条会挤掉第 1 条
        f = OFIFilter(window_ms=10_000, maxlen=2)
        f.update(_t(1_000, "buy", 1.0))
        f.update(_t(1_100, "buy", 1.0))
        f.update(_t(1_200, "sell", 1.0))
        assert len(f) == 2
        # 还在 buffer 的是 [buy@1100, sell@1200]，OFI = 0
        assert f.score(now_ms=2_000) == 0.0

    def test_invalid_window_rejected(self) -> None:
        with pytest.raises(ValueError):
            OFIFilter(window_ms=0)
        with pytest.raises(ValueError):
            OFIFilter(window_ms=1000, maxlen=0)
