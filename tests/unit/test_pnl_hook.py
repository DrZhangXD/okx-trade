"""M5 strategies/pnl_hook 单测：纯函数 + tracker 桥接。"""
from __future__ import annotations

from decimal import Decimal

from okx_trade.pnl import PnLTracker
from okx_trade.strategies.pnl_hook import (
    realized_pnl_and_r,
    record_strategy_equity_daily,
    record_strategy_trade,
    utc_day,
)


# ---------------------------------------------------------------------------
# realized_pnl_and_r
# ---------------------------------------------------------------------------
def test_realized_pnl_long_winner() -> None:
    pnl, r = realized_pnl_and_r(
        direction="long",
        entry_price=100.0, exit_price=110.0,
        contracts=10.0, ct_val=1.0,
        risk_usdt=50.0,
    )
    assert pnl == Decimal("100.0")  # (110-100) * 10 * 1
    assert r == 2.0  # 100 / 50


def test_realized_pnl_long_loser() -> None:
    pnl, r = realized_pnl_and_r(
        direction="long",
        entry_price=100.0, exit_price=95.0,
        contracts=10.0, ct_val=1.0,
        risk_usdt=50.0,
    )
    assert pnl == Decimal("-50.0")
    assert r == -1.0


def test_realized_pnl_short_winner() -> None:
    pnl, r = realized_pnl_and_r(
        direction="short",
        entry_price=100.0, exit_price=90.0,
        contracts=5.0, ct_val=2.0,
        risk_usdt=50.0,
    )
    # sign=-1, (90-100) * 5 * 2 = -100, * sign(-1) = 100
    assert pnl == Decimal("100.0")
    assert r == 2.0


def test_realized_pnl_zero_risk_yields_zero_r() -> None:
    pnl, r = realized_pnl_and_r(
        direction="long",
        entry_price=100.0, exit_price=110.0,
        contracts=1.0, ct_val=1.0,
        risk_usdt=0.0,
    )
    assert pnl == Decimal("10.0")
    assert r == 0.0


# ---------------------------------------------------------------------------
# record_strategy_trade
# ---------------------------------------------------------------------------
def test_record_strategy_trade_writes_record() -> None:
    tracker = PnLTracker(":memory:")
    record_strategy_trade(
        tracker,
        strategy_id="s1", instrument_id="BTC.OKX",
        closed_ts_ms=1_700_000_000_000,
        direction="long", entry_price=100.0, exit_price=110.0,
        contracts=10.0, ct_val=1.0, risk_usdt=50.0,
    )
    trades = tracker.get_trades("s1")
    assert len(trades) == 1
    assert trades[0].pnl_usdt == Decimal("100.0")
    assert trades[0].r_multiple == 2.0
    tracker.close()


def test_record_strategy_trade_silent_when_tracker_none() -> None:
    # 不应抛异常
    record_strategy_trade(
        None,
        strategy_id="s1", instrument_id="x",
        closed_ts_ms=0, direction="long",
        entry_price=1.0, exit_price=2.0,
        contracts=1.0, ct_val=1.0, risk_usdt=1.0,
    )


# ---------------------------------------------------------------------------
# record_strategy_equity_daily
# ---------------------------------------------------------------------------
def test_record_equity_daily_writes_first_call() -> None:
    tracker = PnLTracker(":memory:")
    new_day = record_strategy_equity_daily(
        tracker, strategy_id="s1", ts_ms=1_700_000_000_000,
        equity_usdt=10000.0, last_day=None,
    )
    assert new_day is not None
    assert tracker.get_equities("s1")
    tracker.close()


def test_record_equity_daily_skips_same_day() -> None:
    tracker = PnLTracker(":memory:")
    ts = 1_700_000_000_000
    new_day = record_strategy_equity_daily(
        tracker, strategy_id="s1", ts_ms=ts,
        equity_usdt=10000.0, last_day=None,
    )
    # 同一天再喂：不应再写
    same_day = record_strategy_equity_daily(
        tracker, strategy_id="s1", ts_ms=ts + 100,
        equity_usdt=10100.0, last_day=new_day,
    )
    assert same_day == new_day
    assert len(tracker.get_equities("s1")) == 1
    tracker.close()


def test_record_equity_daily_writes_on_new_day() -> None:
    tracker = PnLTracker(":memory:")
    ts1 = 1_700_000_000_000
    ts2 = ts1 + 86_400_000  # +1 day
    d1 = record_strategy_equity_daily(
        tracker, strategy_id="s1", ts_ms=ts1, equity_usdt=10000.0, last_day=None,
    )
    d2 = record_strategy_equity_daily(
        tracker, strategy_id="s1", ts_ms=ts2, equity_usdt=10100.0, last_day=d1,
    )
    assert d2 != d1
    assert len(tracker.get_equities("s1")) == 2
    tracker.close()


def test_record_equity_daily_skips_zero_equity() -> None:
    tracker = PnLTracker(":memory:")
    new_day = record_strategy_equity_daily(
        tracker, strategy_id="s1", ts_ms=1_700_000_000_000,
        equity_usdt=0.0, last_day=None,
    )
    assert new_day is None
    assert tracker.get_equities("s1") == []
    tracker.close()


def test_record_equity_daily_silent_when_tracker_none() -> None:
    out = record_strategy_equity_daily(
        None, strategy_id="s1", ts_ms=0, equity_usdt=1.0, last_day="2023-01-01",
    )
    assert out == "2023-01-01"  # 原样返回


def test_utc_day_format() -> None:
    # 1700000000000 ms ≈ 2023-11-14 UTC
    assert utc_day(1_700_000_000_000) == "2023-11-14"
