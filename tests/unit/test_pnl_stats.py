"""M5 PnL 纯函数 stats 单测。"""
from __future__ import annotations

from decimal import Decimal

from okx_trade.pnl import (
    EquitySnapshot,
    TradeRecord,
    aggregate_pnl_by_strategy,
    compute_daily_returns,
    compute_sharpe,
    compute_win_rate_avg_r,
)


def _t(pnl: str, *, sid: str = "s1", ts: int = 100, r: float = 1.0) -> TradeRecord:
    return TradeRecord(
        strategy_id=sid, instrument_id="x", closed_ts_ms=ts,
        pnl_usdt=Decimal(pnl), r_multiple=r,
    )


def _e(ts: int, eq: str, *, sid: str = "s1") -> EquitySnapshot:
    return EquitySnapshot(strategy_id=sid, ts_ms=ts, equity_usdt=Decimal(eq))


# ---------------------------------------------------------------------------
# compute_win_rate_avg_r
# ---------------------------------------------------------------------------
def test_win_rate_avg_r_below_min_n_returns_none() -> None:
    trades = [_t("10")] * 5
    assert compute_win_rate_avg_r(trades, min_n=20) is None


def test_win_rate_avg_r_balanced() -> None:
    # 10 wins of +20, 10 losses of -10 → win_rate=0.5, avg_r=20/10=2.0
    trades = [_t("20") for _ in range(10)] + [_t("-10") for _ in range(10)]
    out = compute_win_rate_avg_r(trades, min_n=20)
    assert out is not None
    win_rate, avg_r = out
    assert win_rate == 0.5
    assert avg_r == 2.0


def test_win_rate_all_wins_uses_capped_avg_r() -> None:
    trades = [_t("10") for _ in range(20)]
    out = compute_win_rate_avg_r(trades, min_n=20)
    assert out is not None
    win_rate, avg_r = out
    assert win_rate == 1.0
    assert avg_r == 5.0  # 全胜兜底


def test_win_rate_all_losses_returns_none() -> None:
    trades = [_t("-1") for _ in range(20)]
    assert compute_win_rate_avg_r(trades, min_n=20) is None


def test_win_rate_zero_pnl_excluded_from_both_sides() -> None:
    trades = [_t("10") for _ in range(10)] + [_t("-5") for _ in range(10)] + [_t("0")] * 5
    out = compute_win_rate_avg_r(trades, min_n=20)
    assert out is not None
    win_rate, avg_r = out
    # 25 trades total, 10 wins → win_rate = 10/25 = 0.4
    assert win_rate == 0.4
    assert avg_r == 2.0


# ---------------------------------------------------------------------------
# compute_daily_returns
# ---------------------------------------------------------------------------
def test_daily_returns_empty_below_two_days() -> None:
    assert compute_daily_returns([]) == []
    assert compute_daily_returns([_e(100, "10000")]) == []


def test_daily_returns_two_days() -> None:
    # day1 = 2023-11-14, day2 = 2023-11-15
    d1 = 1_700_000_000_000          # ~2023-11-14
    d2 = 1_700_086_400_000          # +1 day
    rets = compute_daily_returns([_e(d1, "10000"), _e(d2, "10100")])
    assert len(rets) == 1
    assert abs(rets[0] - 0.01) < 1e-9


def test_daily_returns_keeps_last_per_day() -> None:
    d1 = 1_700_000_000_000
    d2 = 1_700_086_400_000
    eqs = [
        _e(d1, "10000"),
        _e(d1 + 100, "11000"),     # 同一天后续覆盖前者
        _e(d2, "12000"),
    ]
    rets = compute_daily_returns(eqs)
    assert len(rets) == 1
    # 用 day1 最后一笔 11000 → day2 12000
    assert abs(rets[0] - (12000 / 11000 - 1)) < 1e-9


def test_daily_returns_handles_zero_equity() -> None:
    d1 = 1_700_000_000_000
    d2 = 1_700_086_400_000
    rets = compute_daily_returns([_e(d1, "0"), _e(d2, "100")])
    assert rets == [0.0]


# ---------------------------------------------------------------------------
# compute_sharpe
# ---------------------------------------------------------------------------
def test_sharpe_zero_for_short_series() -> None:
    assert compute_sharpe([]) == 0.0
    assert compute_sharpe([0.01]) == 0.0


def test_sharpe_zero_variance() -> None:
    assert compute_sharpe([0.01, 0.01, 0.01]) == 0.0


def test_sharpe_positive_for_consistent_positive_returns() -> None:
    # 10 个 0.01 + 1 个 0.02 → 平均正且方差小 → 正 Sharpe
    sharpe = compute_sharpe([0.01] * 10 + [0.02])
    assert sharpe > 0


# ---------------------------------------------------------------------------
# aggregate_pnl_by_strategy
# ---------------------------------------------------------------------------
def test_aggregate_pnl() -> None:
    trades = [_t("10", sid="s1"), _t("-5", sid="s1"), _t("3", sid="s2")]
    out = aggregate_pnl_by_strategy(trades)
    assert out == {"s1": 5.0, "s2": 3.0}


def test_aggregate_pnl_empty() -> None:
    assert aggregate_pnl_by_strategy([]) == {}
