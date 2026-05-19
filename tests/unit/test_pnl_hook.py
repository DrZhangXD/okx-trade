"""M5 strategies/pnl_hook 单测：纯函数 + tracker 桥接 + M6+.X fee 扣减。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from okx_trade.pnl import PnLTracker
from okx_trade.strategies.pnl_hook import (
    DEFAULT_TAKER_FEE_BPS,
    realized_pnl_and_r,
    record_strategy_equity_daily,
    record_strategy_trade,
    utc_day,
)


# ---------------------------------------------------------------------------
# realized_pnl_and_r — 不扣 fee（向后兼容路径 fee_bps=0）
# ---------------------------------------------------------------------------
def test_realized_pnl_long_winner_no_fee() -> None:
    pnl, r = realized_pnl_and_r(
        direction="long",
        entry_price=100.0, exit_price=110.0,
        contracts=10.0, ct_val=1.0,
        risk_usdt=50.0,
        fee_bps=0,
    )
    assert pnl == Decimal("100.0")  # (110-100) * 10 * 1
    assert r == 2.0  # 100 / 50


def test_realized_pnl_long_loser_no_fee() -> None:
    pnl, r = realized_pnl_and_r(
        direction="long",
        entry_price=100.0, exit_price=95.0,
        contracts=10.0, ct_val=1.0,
        risk_usdt=50.0,
        fee_bps=0,
    )
    assert pnl == Decimal("-50.0")
    assert r == -1.0


def test_realized_pnl_short_winner_no_fee() -> None:
    pnl, r = realized_pnl_and_r(
        direction="short",
        entry_price=100.0, exit_price=90.0,
        contracts=5.0, ct_val=2.0,
        risk_usdt=50.0,
        fee_bps=0,
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
        fee_bps=0,
    )
    assert pnl == Decimal("10.0")
    assert r == 0.0


# ---------------------------------------------------------------------------
# realized_pnl_and_r — 扣 fee
# ---------------------------------------------------------------------------
def test_realized_pnl_with_default_fee() -> None:
    # entry=100, exit=110, contracts=10, ct_val=1, default fee=5bps, n_legs=2
    # gross = 100；mean_notional = 10*1*105 = 1050；fee = 1050 * 0.0005 * 2 = 1.05
    # net = 100 - 1.05 = 98.95
    pnl, r = realized_pnl_and_r(
        direction="long",
        entry_price=100.0, exit_price=110.0,
        contracts=10.0, ct_val=1.0,
        risk_usdt=50.0,
    )
    assert pnl == pytest.approx(Decimal("98.95"))
    assert r == pytest.approx(1.979, abs=1e-3)


def test_realized_pnl_with_4_legs_delta_neutral() -> None:
    # delta-neutral 4 legs：fee 是 2-legs 的 2 倍
    pnl_2, _ = realized_pnl_and_r(
        direction="short", entry_price=100, exit_price=100,
        contracts=10, ct_val=1, risk_usdt=100,
        fee_bps=5.0, n_legs=2,
    )
    pnl_4, _ = realized_pnl_and_r(
        direction="short", entry_price=100, exit_price=100,
        contracts=10, ct_val=1, risk_usdt=100,
        fee_bps=5.0, n_legs=4,
    )
    # gross PnL 都是 0；只看 fee：n_legs=4 的 fee 是 2 倍
    fee_2 = -float(pnl_2)
    fee_4 = -float(pnl_4)
    assert fee_4 == pytest.approx(fee_2 * 2)
    assert fee_2 == pytest.approx(1.0)  # 10 * 1 * 100 * 0.0005 * 2 = 1.0


def test_realized_pnl_fee_consumes_small_alpha() -> None:
    # 模拟 ob_imbalance 那种小幅度 alpha：entry=76000, exit=76091.5 (12bp TP)
    # contracts=19, ct_val=0.01 → notional ≈ $14,440
    # gross PnL = (76091.5 - 76000) * 19 * 0.01 = 17.385 USDT
    # fee = 14,440 * 5bp * 2 = $14.44
    # net PnL = 17.385 - 14.44 = 2.945 USDT (vs 账面 +17 的 5x 落差)
    pnl, _ = realized_pnl_and_r(
        direction="long",
        entry_price=76000.0, exit_price=76091.5,
        contracts=19.0, ct_val=0.01,
        risk_usdt=50.0,
        fee_bps=5.0, n_legs=2,
    )
    assert float(pnl) == pytest.approx(2.945, abs=0.1)


def test_default_taker_fee_constant() -> None:
    # 防止误改默认值（修改前要 review）
    assert DEFAULT_TAKER_FEE_BPS == 5.0


# ---------------------------------------------------------------------------
# record_strategy_trade — 写入 fee-deducted PnL
# ---------------------------------------------------------------------------
def test_record_strategy_trade_writes_fee_deducted_pnl() -> None:
    tracker = PnLTracker(":memory:")
    record_strategy_trade(
        tracker,
        strategy_id="s1", instrument_id="BTC.OKX",
        closed_ts_ms=1_700_000_000_000,
        direction="long", entry_price=100.0, exit_price=110.0,
        contracts=10.0, ct_val=1.0, risk_usdt=50.0,
        fee_bps=5.0, n_legs=2,
    )
    trades = tracker.get_trades("s1")
    assert len(trades) == 1
    # gross 100 - fee 1.05 = 98.95
    assert trades[0].pnl_usdt == pytest.approx(Decimal("98.95"))
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
