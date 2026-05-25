"""basis_arb cross-account margin simulator.

Models OKX's reality:
- spot leg sits in a cash sub-account (no leverage, no liquidation risk)
- futures leg sits in a cross-margin sub-account that can be independently
  liquidated when equity / |notional| < MMR

Scope: account-isolation tail risk that NT's single-account default hides.
NOT a full exchange simulator — no order book, no partial fills, single-tier
MMR (no IMR ladder), flat bps fees only.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SpotCashAccount:
    """Cash spot account: USDT + base asset, no leverage, no liquidation risk.

    The model is deliberately simple — every trade adjusts cash + spot_qty by
    the cash-flow it produces (price × qty ± fee). `equity` is mark-to-market.
    """

    cash_usdt: float
    spot_qty: float = 0.0
    realized_pnl: float = 0.0

    def buy_spot(self, *, price: float, qty: float, fee_bps: float) -> None:
        if qty <= 0:
            return
        cost = price * qty
        fee = cost * fee_bps / 10_000.0
        self.cash_usdt -= cost + fee
        self.spot_qty += qty
        self.realized_pnl -= fee

    def sell_spot(self, *, price: float, qty: float, fee_bps: float) -> None:
        if qty <= 0 or qty > self.spot_qty + 1e-12:
            return
        proceeds = price * qty
        fee = proceeds * fee_bps / 10_000.0
        self.cash_usdt += proceeds - fee
        self.spot_qty -= qty
        self.realized_pnl -= fee

    def equity(self, *, mark_price: float) -> float:
        return self.cash_usdt + self.spot_qty * mark_price

    def unrealized_pnl(self, *, mark_price: float, avg_cost: float) -> float:
        """Mark-to-market PnL on open spot relative to ``avg_cost``.

        Kept as an explicit input (caller tracks entry cost) rather than an
        internal weighted-average so the account stays state-light.
        """
        if self.spot_qty == 0:
            return 0.0
        return (mark_price - avg_cost) * self.spot_qty


@dataclass
class FuturesCrossAccount:
    """Cross-margin futures sub-account: liquidates when equity / |notional| < MMR.

    Convention: ``futures_qty > 0`` = long; ``< 0`` = short. ``entry_price``
    is the weighted average across all add-on trades for the open position.
    Funding cashflow is applied externally via ``apply_funding``.
    """

    cash_usdt: float
    mmr: float = 0.005  # maintenance margin ratio (0.5% ≈ OKX tier-1 BTC perp)
    futures_qty: float = 0.0  # signed
    entry_price: float = 0.0
    realized_pnl: float = 0.0
    funding_cashflow_total: float = 0.0
    liquidated: bool = False

    def _avg_in(self, *, price: float, qty: float) -> None:
        """Weighted-average the new fill into the existing entry_price."""
        existing = abs(self.futures_qty)
        if existing == 0 or self.entry_price == 0:
            self.entry_price = price
        else:
            self.entry_price = (existing * self.entry_price + qty * price) / (existing + qty)

    def short_futures(self, *, price: float, qty: float, fee_bps: float) -> None:
        if qty <= 0 or self.liquidated:
            return
        if self.futures_qty > 0:
            raise RuntimeError(
                "FuturesCrossAccount.short_futures called while long; "
                "close the long first to keep the simulator state machine simple"
            )
        notional = price * qty
        fee = notional * fee_bps / 10_000.0
        self.cash_usdt -= fee
        self.realized_pnl -= fee
        self._avg_in(price=price, qty=qty)
        self.futures_qty -= qty

    def long_futures(self, *, price: float, qty: float, fee_bps: float) -> None:
        if qty <= 0 or self.liquidated:
            return
        if self.futures_qty < 0:
            raise RuntimeError(
                "FuturesCrossAccount.long_futures called while short; "
                "close the short first to keep the simulator state machine simple"
            )
        notional = price * qty
        fee = notional * fee_bps / 10_000.0
        self.cash_usdt -= fee
        self.realized_pnl -= fee
        self._avg_in(price=price, qty=qty)
        self.futures_qty += qty

    def close_futures(self, *, price: float, fee_bps: float) -> None:
        if self.futures_qty == 0:
            return
        qty = abs(self.futures_qty)
        notional = price * qty
        fee = notional * fee_bps / 10_000.0
        # PnL: short = (entry - mark) * qty; long = (mark - entry) * qty
        pnl = (self.entry_price - price) * qty if self.futures_qty < 0 \
            else (price - self.entry_price) * qty
        net = pnl - fee
        self.cash_usdt += net
        self.realized_pnl += net
        self.futures_qty = 0.0
        self.entry_price = 0.0

    def unrealized_pnl(self, *, mark_price: float) -> float:
        if self.futures_qty == 0:
            return 0.0
        qty = abs(self.futures_qty)
        return (self.entry_price - mark_price) * qty if self.futures_qty < 0 \
            else (mark_price - self.entry_price) * qty

    def equity(self, *, mark_price: float) -> float:
        return self.cash_usdt + self.unrealized_pnl(mark_price=mark_price)

    def is_liquidated(self, *, mark_price: float) -> bool:
        if self.liquidated:
            return True
        if self.futures_qty == 0:
            return False
        eq = self.equity(mark_price=mark_price)
        notional = abs(self.futures_qty) * mark_price
        if notional <= 0:
            return False
        return eq / notional < self.mmr

    def force_liquidate(self, *, mark_price: float) -> None:
        """Close at mark, wipe remaining equity (worst-case approximation).

        OKX clamps post-liquidation balance to >= 0 — we model that here.
        """
        if self.futures_qty == 0 or self.liquidated:
            self.liquidated = True
            return
        qty = abs(self.futures_qty)
        pnl = (self.entry_price - mark_price) * qty if self.futures_qty < 0 \
            else (mark_price - self.entry_price) * qty
        self.cash_usdt += pnl
        self.realized_pnl += pnl
        self.cash_usdt = max(0.0, self.cash_usdt)
        self.futures_qty = 0.0
        self.entry_price = 0.0
        self.liquidated = True

    def apply_funding(self, *, rate_8h: float, mark_price: float) -> None:
        """Settle one funding cycle. Convention: positive rate ⇒ long pays short."""
        if self.futures_qty == 0 or self.liquidated:
            return
        notional = abs(self.futures_qty) * mark_price
        # Short receives positive funding; long pays
        sign = 1 if self.futures_qty < 0 else -1
        cashflow = sign * notional * rate_8h
        self.cash_usdt += cashflow
        self.funding_cashflow_total += cashflow
        self.realized_pnl += cashflow
