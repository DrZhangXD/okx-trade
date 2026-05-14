"""XS Momentum 纯函数单测。"""
from __future__ import annotations

import pytest

from okx_trade.strategies.xs_momentum import (
    apply_inst_count_cap,
    cross_section_rank,
    momentum_score,
    plan_delta_orders,
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


# ---------------------------------------------------------------------------
# apply_inst_count_cap —— demo margin 节流,把 (longs, shorts) 裁到 cap 总数
# ---------------------------------------------------------------------------


class TestApplyInstCountCap:
    def test_proportional_5_5_to_4(self) -> None:
        """top_n=bot_n=5, cap=4 → 2 long + 2 short(各保留前 2 最佳)。"""
        longs = ["L1", "L2", "L3", "L4", "L5"]
        shorts = ["S1", "S2", "S3", "S4", "S5"]
        out_l, out_s = apply_inst_count_cap(
            longs, shorts, cap=4, top_n=5, bot_n=5,
        )
        assert out_l == ["L1", "L2"]
        assert out_s == ["S1", "S2"]

    def test_uneven_3_5_to_4(self) -> None:
        """top_n=3, bot_n=5, cap=4 → max_long=round(4*3/8)=2(银行舍入), short=2。"""
        longs = ["L1", "L2", "L3"]
        shorts = ["S1", "S2", "S3", "S4", "S5"]
        out_l, out_s = apply_inst_count_cap(
            longs, shorts, cap=4, top_n=3, bot_n=5,
        )
        # 注:Python round() 是银行舍入,round(1.5)=2 → max_long=2, max_short=2
        assert len(out_l) + len(out_s) == 4
        assert out_l[0] == "L1"  # 最佳保留
        assert out_s[0] == "S1"

    def test_cap_exceeds_total_returns_unchanged(self) -> None:
        longs = ["L1", "L2"]
        shorts = ["S1", "S2"]
        out_l, out_s = apply_inst_count_cap(
            longs, shorts, cap=10, top_n=5, bot_n=5,
        )
        assert out_l == longs
        assert out_s == shorts

    def test_zero_bot_n_gives_all_to_longs(self) -> None:
        """bot_n=0(只多不空)时,cap 全给 longs。"""
        longs = ["L1", "L2", "L3", "L4", "L5"]
        out_l, out_s = apply_inst_count_cap(
            longs, [], cap=3, top_n=5, bot_n=0,
        )
        assert out_l == ["L1", "L2", "L3"]
        assert out_s == []

    def test_zero_top_n_gives_all_to_shorts(self) -> None:
        """top_n=0(只空不多)时,cap 全给 shorts。"""
        shorts = ["S1", "S2", "S3", "S4", "S5"]
        out_l, out_s = apply_inst_count_cap(
            [], shorts, cap=3, top_n=0, bot_n=5,
        )
        assert out_l == []
        assert out_s == ["S1", "S2", "S3"]

    def test_cap_zero_returns_empty(self) -> None:
        out_l, out_s = apply_inst_count_cap(
            ["L1"], ["S1"], cap=0, top_n=5, bot_n=5,
        )
        assert out_l == [] and out_s == []

    def test_leftover_redistribute_when_one_side_short(self) -> None:
        """top_n=5/bot_n=5/cap=8 → 想要 4+4,但 shorts 只 1 个 → leftover=3 → longs=7。"""
        longs = ["L1", "L2", "L3", "L4", "L5", "L6", "L7"]
        shorts = ["S1"]
        out_l, out_s = apply_inst_count_cap(
            longs, shorts, cap=8, top_n=5, bot_n=5,
        )
        # cap=8 >= 7+1=8 → 不动 → 直接走 cap-exceeds 分支
        assert out_l == longs and out_s == shorts

    def test_leftover_redistribute_strict_cap(self) -> None:
        """cap < total,某一边名额不够时余额还给另一边。"""
        # top_n=5, bot_n=5, cap=6 → 期望 3+3;但 longs 只 2 个 → 余 1 给 shorts → 2+4
        longs = ["L1", "L2"]
        shorts = ["S1", "S2", "S3", "S4", "S5"]
        out_l, out_s = apply_inst_count_cap(
            longs, shorts, cap=6, top_n=5, bot_n=5,
        )
        assert len(out_l) + len(out_s) == 6
        assert out_l == ["L1", "L2"]
        assert out_s == ["S1", "S2", "S3", "S4"]

    def test_preserves_order_keeps_best_picks(self) -> None:
        """裁切是 list[:k],保留前 k 个;longs / shorts 顺序假设最佳在前。"""
        longs = ["BEST_L", "MID_L", "WORST_L"]
        shorts = ["BEST_S", "MID_S", "WORST_S"]
        out_l, out_s = apply_inst_count_cap(
            longs, shorts, cap=2, top_n=3, bot_n=3,
        )
        assert out_l == ["BEST_L"]
        assert out_s == ["BEST_S"]


# ---------------------------------------------------------------------------
# plan_delta_orders —— 根据 current + delta 拆开/平/翻仓单 + 正确 reduce_only
# 回归 2026-05 真实故障:_submit_delta 不传 reduce_only → OKX 把"平仓"当新开
# → margin 累积全锁 → 全 51008。
# ---------------------------------------------------------------------------


class TestPlanDeltaOrders:
    def test_open_from_flat_long(self) -> None:
        """current=0, delta=+5 → 单一开多单,reduce_only=False。"""
        out = plan_delta_orders(current=0.0, delta=5.0)
        assert out == [(5.0, False)]

    def test_open_from_flat_short(self) -> None:
        """current=0, delta=-3 → 单一开空单,reduce_only=False。"""
        out = plan_delta_orders(current=0.0, delta=-3.0)
        assert out == [(-3.0, False)]

    def test_add_to_long(self) -> None:
        """current=+5, delta=+3 → 同向加仓,reduce_only=False。"""
        out = plan_delta_orders(current=5.0, delta=3.0)
        assert out == [(3.0, False)]

    def test_add_to_short(self) -> None:
        """current=-5, delta=-3 → 同向加仓,reduce_only=False。"""
        out = plan_delta_orders(current=-5.0, delta=-3.0)
        assert out == [(-3.0, False)]

    def test_partial_reduce_long(self) -> None:
        """current=+5, delta=-3 → 缩仓,reduce_only=True。"""
        out = plan_delta_orders(current=5.0, delta=-3.0)
        assert out == [(-3.0, True)]

    def test_partial_reduce_short(self) -> None:
        """current=-5, delta=+3 → 缩仓,reduce_only=True。"""
        out = plan_delta_orders(current=-5.0, delta=3.0)
        assert out == [(3.0, True)]

    def test_close_to_flat_long(self) -> None:
        """current=+5, delta=-5 → 全平到 0,reduce_only=True。"""
        out = plan_delta_orders(current=5.0, delta=-5.0)
        assert out == [(-5.0, True)]

    def test_close_to_flat_short(self) -> None:
        """current=-5, delta=+5 → 全平到 0,reduce_only=True。"""
        out = plan_delta_orders(current=-5.0, delta=5.0)
        assert out == [(5.0, True)]

    def test_flip_long_to_short(self) -> None:
        """current=+5, delta=-8 → 翻仓:先平 +5(reduce_only) 再开空 -3。"""
        out = plan_delta_orders(current=5.0, delta=-8.0)
        assert out == [(-5.0, True), (-3.0, False)]

    def test_flip_short_to_long(self) -> None:
        """current=-5, delta=+9 → 翻仓:先平 -5(reduce_only) 再开多 +4。"""
        out = plan_delta_orders(current=-5.0, delta=9.0)
        assert out == [(5.0, True), (4.0, False)]

    def test_zero_delta_is_noop(self) -> None:
        assert plan_delta_orders(current=5.0, delta=0.0) == []
        assert plan_delta_orders(current=0.0, delta=0.0) == []

    def test_qty_signs_preserved_for_orderside_resolution(self) -> None:
        """所有返回 qty 的符号必须与 ORDER 方向一致(>0 BUY / <0 SELL),
        上层 ``_submit_delta`` 据此选 ``OrderSide``。"""
        # 翻仓中:close 的符号必须 BUY/SELL 平 current,open 的符号继续 delta 方向
        out = plan_delta_orders(current=5.0, delta=-8.0)
        close_q, _ = out[0]
        open_q, _ = out[1]
        # current 是 LONG +5,平仓必须 SELL → qty < 0
        assert close_q < 0
        # delta -8 = SELL,翻完后 open 继续 SELL → qty < 0
        assert open_q < 0
        # 镜像
        out2 = plan_delta_orders(current=-5.0, delta=9.0)
        assert out2[0][0] > 0 and out2[1][0] > 0  # 都是 BUY 方向
