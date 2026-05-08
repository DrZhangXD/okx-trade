"""M5 feed_risk_handles 单测：验证 KellyCheck / CorrelationCheck 真被回灌。"""
from __future__ import annotations

from decimal import Decimal

from okx_trade.pnl import (
    EquitySnapshot,
    PnLTracker,
    TradeRecord,
    feed_risk_handles,
)
from okx_trade.risk import RiskConfig, build_risk_manager


def _populate_trades(tracker: PnLTracker, sid: str, n_win: int, n_loss: int) -> None:
    base = 1_700_000_000_000
    for i in range(n_win):
        tracker.record_trade(TradeRecord(
            strategy_id=sid, instrument_id="x", closed_ts_ms=base + i,
            pnl_usdt=Decimal("10"), r_multiple=1.0,
        ))
    for i in range(n_loss):
        tracker.record_trade(TradeRecord(
            strategy_id=sid, instrument_id="x", closed_ts_ms=base + 1000 + i,
            pnl_usdt=Decimal("-5"), r_multiple=-1.0,
        ))


def _populate_equities(tracker: PnLTracker, sid: str, days: int) -> None:
    # 每日 +1% 增长
    base = 1_700_000_000_000
    eq = Decimal("10000")
    for i in range(days):
        tracker.record_equity(EquitySnapshot(
            strategy_id=sid, ts_ms=base + i * 86_400_000,
            equity_usdt=eq,
        ))
        eq = eq * Decimal("1.01")


def test_feed_kelly_updates_when_enough_trades() -> None:
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    assert handles.kelly is not None
    original_win_rate = handles.kelly.win_rate
    original_avg_r = handles.kelly.avg_r

    tracker = PnLTracker(":memory:")
    _populate_trades(tracker, "s1", n_win=15, n_loss=10)  # win_rate=0.6, avg_r=2.0

    feed_risk_handles(tracker, handles, "s1", min_trades_for_kelly=20)

    assert handles.kelly.win_rate != original_win_rate
    assert handles.kelly.avg_r != original_avg_r
    assert abs(handles.kelly.win_rate - 0.6) < 1e-9
    assert abs(handles.kelly.avg_r - 2.0) < 1e-9
    tracker.close()


def test_feed_kelly_skipped_when_not_enough_trades() -> None:
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    assert handles.kelly is not None
    original_win_rate = handles.kelly.win_rate

    tracker = PnLTracker(":memory:")
    _populate_trades(tracker, "s1", n_win=3, n_loss=2)  # 只有 5 个 < 20

    feed_risk_handles(tracker, handles, "s1", min_trades_for_kelly=20)
    assert handles.kelly.win_rate == original_win_rate
    tracker.close()


def test_feed_correlation_fills_returns_deque() -> None:
    cfg = RiskConfig(enable_correlation=True, correlation_window=30)
    _, handles = build_risk_manager(cfg)
    assert handles.correlation is not None

    tracker = PnLTracker(":memory:")
    _populate_equities(tracker, "s1", days=10)

    feed_risk_handles(tracker, handles, "s1")
    rets = list(handles.correlation._returns["s1"])
    assert len(rets) == 9  # 10 days → 9 returns
    for r in rets:
        assert abs(r - 0.01) < 1e-9
    tracker.close()


def test_feed_correlation_truncates_to_window() -> None:
    cfg = RiskConfig(enable_correlation=True, correlation_window=5)
    _, handles = build_risk_manager(cfg)
    assert handles.correlation is not None

    tracker = PnLTracker(":memory:")
    _populate_equities(tracker, "s1", days=20)

    feed_risk_handles(tracker, handles, "s1")
    rets = list(handles.correlation._returns["s1"])
    assert len(rets) == 5  # 截断到 window
    tracker.close()


def test_feed_correlation_idempotent_under_repeated_calls() -> None:
    """连续 feed 不应导致 deque 重复累加。"""
    cfg = RiskConfig(enable_correlation=True, correlation_window=30)
    _, handles = build_risk_manager(cfg)
    assert handles.correlation is not None

    tracker = PnLTracker(":memory:")
    _populate_equities(tracker, "s1", days=10)

    feed_risk_handles(tracker, handles, "s1")
    feed_risk_handles(tracker, handles, "s1")
    feed_risk_handles(tracker, handles, "s1")
    rets = list(handles.correlation._returns["s1"])
    assert len(rets) == 9  # 仍然只有 9 个，不是 27
    tracker.close()


def test_feed_no_handles_no_crash() -> None:
    """RiskConfig 全关时 handles 都是 None，feed 应当安静无事。"""
    cfg = RiskConfig()
    _, handles = build_risk_manager(cfg)
    assert handles.kelly is None
    assert handles.correlation is None

    tracker = PnLTracker(":memory:")
    _populate_trades(tracker, "s1", n_win=15, n_loss=10)
    _populate_equities(tracker, "s1", days=10)

    feed_risk_handles(tracker, handles, "s1")  # 不应 raise
    tracker.close()


def test_feed_with_no_data_does_not_call_set_stats() -> None:
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    assert handles.kelly is not None
    original = handles.kelly.win_rate

    tracker = PnLTracker(":memory:")
    feed_risk_handles(tracker, handles, "s1")
    assert handles.kelly.win_rate == original
    tracker.close()
