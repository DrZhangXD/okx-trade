"""Tests for SpotCashAccount + FuturesCrossAccount in basis_arb_sim."""
from __future__ import annotations

import pytest

from okx_trade.backtest.basis_arb_sim import FuturesCrossAccount, SpotCashAccount


def test_spot_cash_account_buy_then_mark_and_sell():
    acc = SpotCashAccount(cash_usdt=10_000.0)
    # Buy 0.1 BTC at $60k → 6000 USDT cost + 5 bps fee
    acc.buy_spot(price=60_000, qty=0.1, fee_bps=5.0)
    expected_after_buy = 10_000 - 6_000 - 6_000 * 5 / 10_000
    assert acc.cash_usdt == pytest.approx(expected_after_buy)
    assert acc.spot_qty == pytest.approx(0.1)

    # Mark at $61k → equity = cash + 0.1 × 61000 = expected_after_buy + 6100
    assert acc.equity(mark_price=61_000) == pytest.approx(expected_after_buy + 6_100)

    # Sell all at $61k
    acc.sell_spot(price=61_000, qty=0.1, fee_bps=5.0)
    expected_cash = expected_after_buy + 6_100 - 6_100 * 5 / 10_000
    assert acc.cash_usdt == pytest.approx(expected_cash)
    assert acc.spot_qty == 0.0


def test_spot_cash_account_overlarge_sell_no_op():
    acc = SpotCashAccount(cash_usdt=10_000.0)
    acc.buy_spot(price=60_000, qty=0.1, fee_bps=0)
    cash_before = acc.cash_usdt
    acc.sell_spot(price=60_000, qty=999.0, fee_bps=0)  # > spot_qty → no-op
    assert acc.cash_usdt == cash_before
    assert acc.spot_qty == pytest.approx(0.1)


def test_futures_account_short_unrealized_and_close():
    acc = FuturesCrossAccount(cash_usdt=1_000.0, mmr=0.005)
    # Short 0.1 BTC at $60k; fee 5 bps
    acc.short_futures(price=60_000, qty=0.1, fee_bps=5.0)
    assert acc.futures_qty == pytest.approx(-0.1)
    assert acc.entry_price == pytest.approx(60_000)

    # Mark at $59k → unrealized PnL = (60000 - 59000) × 0.1 = +100
    assert acc.unrealized_pnl(mark_price=59_000) == pytest.approx(100.0)

    # Close at $59k → realized = +100 - fee_bps × notional
    acc.close_futures(price=59_000, fee_bps=5.0)
    assert acc.futures_qty == 0.0


def test_futures_account_liquidates_when_equity_below_mmr():
    acc = FuturesCrossAccount(cash_usdt=1_000.0, mmr=0.005)
    acc.short_futures(price=60_000, qty=0.1, fee_bps=0)
    # At entry: not liquidated
    assert not acc.is_liquidated(mark_price=60_000)

    # Price rises to $70k → unrealized = (60k - 70k) × 0.1 = -1000 → equity = 0
    # notional = 7000 → eq/notional = 0/7000 = 0 < 0.005 → liquidated
    assert acc.is_liquidated(mark_price=70_000)

    acc.force_liquidate(mark_price=70_000)
    assert acc.futures_qty == 0.0
    assert acc.liquidated is True
    assert acc.cash_usdt >= 0.0  # OKX clamps to >= 0


def test_futures_account_apply_funding_short_receives_positive():
    acc = FuturesCrossAccount(cash_usdt=1_000.0)
    acc.short_futures(price=60_000, qty=0.1, fee_bps=0)
    cash_before = acc.cash_usdt
    # Positive funding rate ⇒ long pays short ⇒ short cash goes up
    acc.apply_funding(rate_8h=0.0001, mark_price=60_000)
    # notional = 6000 USDT, cashflow = +6000 × 0.0001 = +0.6
    assert acc.cash_usdt == pytest.approx(cash_before + 0.6)
    assert acc.funding_cashflow_total == pytest.approx(0.6)


def test_futures_account_apply_funding_no_op_when_flat():
    acc = FuturesCrossAccount(cash_usdt=1_000.0)
    cash_before = acc.cash_usdt
    acc.apply_funding(rate_8h=0.0001, mark_price=60_000)  # no position
    assert acc.cash_usdt == cash_before


def test_futures_account_short_then_short_weighted_avg_entry():
    acc = FuturesCrossAccount(cash_usdt=10_000.0)
    acc.short_futures(price=60_000, qty=0.1, fee_bps=0)
    acc.short_futures(price=62_000, qty=0.1, fee_bps=0)
    # Weighted avg = (0.1*60000 + 0.1*62000) / 0.2 = 61000
    assert acc.entry_price == pytest.approx(61_000)
    assert acc.futures_qty == pytest.approx(-0.2)


def test_futures_account_rejects_short_while_long():
    acc = FuturesCrossAccount(cash_usdt=10_000.0)
    acc.long_futures(price=60_000, qty=0.1, fee_bps=0)
    with pytest.raises(RuntimeError, match="while long"):
        acc.short_futures(price=60_000, qty=0.1, fee_bps=0)
