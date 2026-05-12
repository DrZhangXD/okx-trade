"""``range_breakout`` 纯函数单测：与 scalp 同名实现对齐。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from okx_trade.strategies.base import BarSnapshot, position_contracts
from okx_trade.strategies.range_breakout import (
    Range,
    RangeBreakoutLogic,
    RangeBreakoutStrategy,
    detect_range_breakout_signal,
    last_completed_range,
)


def _bar(ts: int, o: float, h: float, lo: float, c: float, *,
         vol: float = 1.0, confirm: bool = True) -> BarSnapshot:
    return BarSnapshot(ts=ts, open=o, high=h, low=lo, close=c,
                       volume=vol, confirm=confirm)


# ---------------------------------------------------------------------------
# Range basics
# ---------------------------------------------------------------------------


class TestRangeBasics:
    def test_mid_and_width(self) -> None:
        r = Range(high=110.0, low=100.0, range_open_ts=1)
        assert r.mid == 105.0
        assert r.width == 10.0

    def test_contains_close_strict(self) -> None:
        r = Range(high=110, low=100, range_open_ts=1)
        assert r.contains_close(105) is True
        # 边界不算（严格小于 / 大于）
        assert r.contains_close(110) is False
        assert r.contains_close(100) is False


class TestLastCompletedRange:
    def test_picks_last_confirmed(self) -> None:
        bars = [
            _bar(1, 100, 105, 95, 102, confirm=True),
            _bar(2, 102, 108, 99, 107, confirm=True),
            _bar(3, 107, 110, 105, 109, confirm=False),  # 未收线，应忽略
        ]
        r = last_completed_range(bars)
        assert r is not None
        assert r.high == 108
        assert r.low == 99
        assert r.range_open_ts == 2

    def test_none_when_no_confirmed(self) -> None:
        bars = [_bar(1, 100, 105, 95, 102, confirm=False)]
        assert last_completed_range(bars) is None


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------


class TestDetectSignal:
    """构造合成 K 线序列触发各种信号路径。"""

    def setup_method(self) -> None:
        self.params = RangeBreakoutLogic(
            tp_rr_ratio=2.0,
            max_stop_distance_pct=0.05,
            swing_lookback=5,
        )
        # 区间 [100, 110]，width=10, mid=105, upper_third=107.5, lower_third=102.5
        self.wrange = Range(high=110, low=100, range_open_ts=0)

    def test_no_signal_when_too_few_bars(self) -> None:
        bars = [_bar(1, 105, 106, 104, 105.5)]
        assert detect_range_breakout_signal(bars, self.wrange, self.params) is None

    def test_breakout_up_then_back_in_triggers_short(self) -> None:
        # 序列：先一根上破收 112（state→breakout_up，extreme=113），
        # 再一根回区间收 102（< lower_third，但是是做空，过滤是 short<lower_third 才挡）
        bars = [
            _bar(1, 100, 102, 99, 101),         # idle 内部
            _bar(2, 101, 113, 100.5, 112),      # 上破，extreme=113
            _bar(3, 112, 112.5, 101, 102),      # 回归区间内（last bar），触发 SHORT
        ]
        # entry=102 < lower_third=102.5，过滤会挡掉 → 期望 None
        sig = detect_range_breakout_signal(bars, self.wrange, self.params)
        assert sig is None

    def test_breakout_up_signal_passes_filter(self) -> None:
        # entry 落在中区（105），通过过滤
        bars = [
            _bar(1, 100, 102, 99, 101),
            _bar(2, 101, 113, 100.5, 112),     # 上破 extreme=113
            _bar(3, 112, 112.5, 105, 106),     # 回归收 106（>105 但 <107.5），过滤通过
        ]
        sig = detect_range_breakout_signal(bars, self.wrange, self.params)
        assert sig is not None
        assert sig.direction == "short"
        assert sig.entry_price == 106
        assert sig.breakout_extreme == 113
        # stop_distance = |106 - 113| = 7；7/106 ≈ 6.6% > 5%（max_stop_distance_pct）
        # → 触发 swing 替换；swing_lookback=5 但 bars_before=[bar1, bar2] 只有 2 根
        # swing_high = max(102, 113) = 113 → 仍是 113，没改善
        # new_dist = 7，仍 > 5%；7/106=6.6% < 5%*1.5=7.5% → 通过
        assert sig.stop_price == 113
        # tp = entry - dist*RR = 106 - 7*2 = 92
        assert sig.take_profit_price == 92.0

    def test_breakout_down_signal_long(self) -> None:
        # entry=104, stop=88 → dist=16, 16/104=15% > max_stop_distance_pct*1.5=7.5%
        # swing 也救不了（只有 2 根 bar before，min low=88） → return None
        bars = [
            _bar(1, 100, 102, 99, 101),
            _bar(2, 100, 100.5, 88, 89),      # 下破 extreme=88
            _bar(3, 89, 105, 88.5, 104),      # 回归
        ]
        sig = detect_range_breakout_signal(bars, self.wrange, self.params)
        assert sig is None  # 止损过宽，放弃

    def test_breakout_down_signal_long_within_threshold(self) -> None:
        # 这次 stop 距离合规（< 5%）：extreme=98，entry=103
        # dist=5, 5/103≈4.85% < 5% → 通过
        bars = [
            _bar(1, 100, 102, 99, 101),
            _bar(2, 100, 100.5, 98, 99),      # 下破 extreme=98（仅小幅下破）
            _bar(3, 99, 104, 98.5, 103),      # 回归收 103
        ]
        sig = detect_range_breakout_signal(bars, self.wrange, self.params)
        assert sig is not None
        assert sig.direction == "long"
        assert sig.entry_price == 103
        assert sig.stop_price == 98
        # tp = entry + dist*RR = 103 + 5*2 = 113
        assert sig.take_profit_price == 113

    def test_long_filtered_at_top_of_range(self) -> None:
        # 即便有完整的 down breakout → return，但 entry 落在 upper third 也不发信号
        bars = [
            _bar(1, 100, 100.5, 99, 99.5),
            _bar(2, 99, 99.5, 95, 96),         # 下破 extreme=95
            _bar(3, 96, 109, 95.5, 108.5),     # 回归收 108.5 > upper_third=107.5
        ]
        sig = detect_range_breakout_signal(bars, self.wrange, self.params)
        assert sig is None

    def test_signal_only_on_last_bar(self) -> None:
        # 中间出现回归动作不应该触发；只有最新一根回归才发信号
        bars = [
            _bar(1, 100, 113, 99, 112),       # 上破
            _bar(2, 112, 112.5, 105, 106),    # 回归（中间，不应触发）
            _bar(3, 106, 110, 100, 105),      # 区间内 → 状态机 idle，最新一根没 breakout-then-back
        ]
        sig = detect_range_breakout_signal(bars, self.wrange, self.params)
        assert sig is None

    def test_only_wick_break_does_not_count(self) -> None:
        # K 线只有影线刺破区间但收盘仍在区间内 → 不算 breakout
        bars = [
            _bar(1, 105, 115, 104, 106),  # 影线高 115 但 close=106 仍在区间内
            _bar(2, 106, 109, 105, 107),
        ]
        sig = detect_range_breakout_signal(bars, self.wrange, self.params)
        assert sig is None


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


class TestPositionContracts:
    def test_basic_calc(self) -> None:
        # equity=10000, risk=1% = 100 USDT
        # entry=67000, stop=66800 → stop_dist=200
        # coins = 100/200 = 0.5 BTC；ct_val=0.01 → 50 张；lot_sz=1 → floor 50
        c = position_contracts(
            account_equity_usdt=10000,
            risk_pct=0.01,
            entry_price=67000,
            stop_price=66800,
            ct_val=0.01,
            min_sz=1,
            lot_sz=1,
        )
        assert c == 50.0

    def test_floor_to_lot(self) -> None:
        # raw_contracts = 50.7 → floor to lot_sz=1 → 50
        c = position_contracts(
            account_equity_usdt=10070,
            risk_pct=0.01,
            entry_price=67000,
            stop_price=66800,
            ct_val=0.01,
            min_sz=1,
            lot_sz=1,
        )
        assert c == 50.0

    def test_below_min_returns_zero(self) -> None:
        # 风险金额=10000*0.0001=1 USDT, stop_dist=10 → coins=0.1, ct_val=0.01 → 10 张
        # min_sz=20 → 10 < 20 → 0
        c = position_contracts(
            account_equity_usdt=10000,
            risk_pct=0.0001,
            entry_price=67000,
            stop_price=66990,
            ct_val=0.01,
            min_sz=20,
            lot_sz=1,
        )
        assert c == 0.0

    def test_zero_when_invalid(self) -> None:
        assert position_contracts(0, 0.01, 100, 99, 1, 1, 1) == 0.0
        assert position_contracts(1000, 0.01, 100, 100, 1, 1, 1) == 0.0  # 0 stop dist
        assert position_contracts(1000, 0.01, 100, 99, 0, 1, 1) == 0.0


class TestOnOrderRejected:
    """OrderRejected 清幻仓状态：防止 51008 级联（开仓被拒后下一根 bar 又
    拿幻仓 reduce-only 平仓，又被拒）。"""

    def _mock_strat(self, *, active_signal: object | None, contracts: float) -> object:
        strat = MagicMock()
        strat._active_signal = active_signal
        strat._position_contracts = contracts
        strat.log = MagicMock()
        return strat

    def test_clears_state_on_rejection(self) -> None:
        sig = SimpleNamespace(direction="short")
        strat = self._mock_strat(active_signal=sig, contracts=0.76)
        evt = SimpleNamespace(reason="sCode=51008: Insufficient USDT margin")
        RangeBreakoutStrategy.on_order_rejected(strat, evt)
        assert strat._active_signal is None
        assert strat._position_contracts == 0.0
        strat.log.warning.assert_called_once()

    def test_idempotent_when_already_flat(self) -> None:
        strat = self._mock_strat(active_signal=None, contracts=0.0)
        RangeBreakoutStrategy.on_order_rejected(
            strat, SimpleNamespace(reason="x"),
        )
        strat.log.warning.assert_not_called()

    def test_handles_missing_reason_attr(self) -> None:
        sig = SimpleNamespace(direction="long")
        strat = self._mock_strat(active_signal=sig, contracts=0.3)
        RangeBreakoutStrategy.on_order_rejected(strat, SimpleNamespace())
        assert strat._active_signal is None
        assert strat._position_contracts == 0.0
