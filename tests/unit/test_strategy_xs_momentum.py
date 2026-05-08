"""XS Momentum 纯函数单测。"""
from __future__ import annotations

import pytest

from okx_trade.strategies.xs_momentum import (
    cross_section_rank,
    momentum_score,
    rebalance_orders,
    vol_managed_weight,
)

# ---------------------------------------------------------------------------
# momentum_score
# ---------------------------------------------------------------------------


class TestMomentumScore:
    def test_basic(self) -> None:
        # close[-8] = 100, close[-1] = 110 → 10%
        closes = [100.0] + [105.0] * 6 + [110.0]
        assert momentum_score(closes, lookback_days=7) == pytest.approx(0.10)

    def test_negative(self) -> None:
        closes = [100.0] + [95.0] * 6 + [80.0]
        assert momentum_score(closes, lookback_days=7) == pytest.approx(-0.20)

    def test_insufficient_returns_none(self) -> None:
        # 长度 == lookback_days，差 1 根
        assert momentum_score([100.0] * 7, lookback_days=7) is None

    def test_lookback_one(self) -> None:
        closes = [100.0, 102.0]
        assert momentum_score(closes, lookback_days=1) == pytest.approx(0.02)

    def test_zero_base_returns_none(self) -> None:
        closes = [0.0, 100.0, 110.0]
        assert momentum_score(closes, lookback_days=2) is None


# ---------------------------------------------------------------------------
# cross_section_rank
# ---------------------------------------------------------------------------


class TestCrossSectionRank:
    def test_basic_sort(self) -> None:
        scores = {"A": 0.1, "B": 0.5, "C": -0.2, "D": 0.3, "E": -0.5}
        longs, shorts = cross_section_rank(scores, top_n=2, bot_n=2)
        assert longs == ["B", "D"]
        assert shorts == ["E", "C"]

    def test_stable_tiebreak(self) -> None:
        # 并列时按 inst_id 升序
        scores = {"BBB": 0.5, "AAA": 0.5, "CCC": 0.5, "DDD": -0.1}
        longs, shorts = cross_section_rank(scores, top_n=2, bot_n=1)
        assert longs == ["AAA", "BBB"]
        assert shorts == ["DDD"]

    def test_empty(self) -> None:
        longs, shorts = cross_section_rank({}, top_n=5, bot_n=5)
        assert longs == []
        assert shorts == []

    def test_overlap_dropped(self) -> None:
        # 只有 3 个 inst，top 2 + bot 2 = 4 名额，应去重
        scores = {"A": 0.5, "B": 0.0, "C": -0.5}
        longs, shorts = cross_section_rank(scores, top_n=2, bot_n=2)
        assert longs == ["A", "B"]
        # shorts_raw = [B, C] → 去掉 B（已在 longs） → [C]
        assert shorts == ["C"]

    def test_top_n_larger_than_universe(self) -> None:
        scores = {"A": 0.5, "B": 0.0}
        longs, shorts = cross_section_rank(scores, top_n=10, bot_n=0)
        assert longs == ["A", "B"]
        assert shorts == []


# ---------------------------------------------------------------------------
# vol_managed_weight
# ---------------------------------------------------------------------------


class TestVolManagedWeight:
    def test_insufficient_data_returns_raw(self) -> None:
        # vol_window=20，只给 10 根 close → 不足，返回原值
        closes = [100.0 + i for i in range(10)]
        assert vol_managed_weight(0.01, closes, vol_window=20) == 0.01

    def test_low_vol_scales_up(self) -> None:
        # 平稳缓涨：vol 极低 → scale 应触顶 max_scale
        closes = [100.0 + 0.001 * i for i in range(25)]
        out = vol_managed_weight(
            0.01, closes, target_vol_annualized=0.15,
            vol_window=20, max_scale=2.0, min_scale=0.1,
        )
        assert out == pytest.approx(0.02)  # 0.01 * 2.0

    def test_high_vol_scales_down(self) -> None:
        # 大幅震荡：vol 远高于 15% → scale 应贴底 min_scale
        closes = [100.0 * (1.0 + 0.05 * (-1) ** i) for i in range(25)]
        out = vol_managed_weight(
            0.01, closes, target_vol_annualized=0.15,
            vol_window=20, max_scale=2.0, min_scale=0.1,
        )
        # min_scale=0.1 → 0.01 * 0.1 = 0.001
        assert out == pytest.approx(0.001)

    def test_preserves_sign(self) -> None:
        # 负权重（做空）应保留负号
        closes = [100.0 + 0.001 * i for i in range(25)]
        out = vol_managed_weight(-0.01, closes, vol_window=20, max_scale=2.0)
        assert out < 0
        assert out == pytest.approx(-0.02)

    def test_constant_closes_returns_raw(self) -> None:
        # vol = 0 → 函数 fallback 返回 raw_weight（避免除 0）
        closes = [100.0] * 30
        out = vol_managed_weight(0.01, closes, vol_window=20)
        assert out == 0.01


# ---------------------------------------------------------------------------
# rebalance_orders
# ---------------------------------------------------------------------------


class TestRebalanceOrders:
    def test_open_from_zero(self) -> None:
        deltas = rebalance_orders(current={}, target={"A": 5.0, "B": -3.0})
        assert deltas == {"A": 5.0, "B": -3.0}

    def test_close_to_zero(self) -> None:
        deltas = rebalance_orders(current={"A": 5.0, "B": -3.0}, target={})
        assert deltas == {"A": -5.0, "B": 3.0}

    def test_flip_long_to_short(self) -> None:
        # +5 → -3，需要卖 8 张
        deltas = rebalance_orders(current={"A": 5.0}, target={"A": -3.0})
        assert deltas == {"A": -8.0}

    def test_no_change_omitted(self) -> None:
        deltas = rebalance_orders(current={"A": 5.0, "B": 1.0}, target={"A": 5.0, "B": 2.0})
        assert deltas == {"B": 1.0}

    def test_min_delta_filter(self) -> None:
        deltas = rebalance_orders(
            current={"A": 5.0, "B": 1.0},
            target={"A": 5.05, "B": 2.0},
            min_delta=0.1,
        )
        # A 的 delta=0.05 < 0.1 → 不下；B delta=1.0 → 下
        assert deltas == {"B": 1.0}
