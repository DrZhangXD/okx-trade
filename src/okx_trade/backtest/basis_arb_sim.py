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

from bisect import bisect_right
from dataclasses import dataclass

from ..strategies._signals import (
    BasisAction,
    BasisArbParams,
    annualized_basis,
    basis_decision,
)


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
        if self.liquidated:
            return
        if self.futures_qty == 0:
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


@dataclass(frozen=True, slots=True)
class BasisArbSimResult:
    """Summary + per-bar equity curve for a basis_arb margin-isolated backtest."""

    starting_equity: float
    net_final_equity: float
    spot_final_equity: float
    futures_final_equity: float
    futures_liquidated: bool
    n_entries: int
    n_exits: int
    total_funding_cashflow: float
    equity_curve: list[tuple[int, float]]


def _latest_funding_rate(funding: list[tuple[int, float]], ts_ms: int) -> float:
    """Return the most recent funding rate settled at-or-before ts_ms; 0 if none."""
    if not funding:
        return 0.0
    ts_list = [t for t, _ in funding]
    idx = bisect_right(ts_list, ts_ms) - 1
    return 0.0 if idx < 0 else funding[idx][1]


def run_basis_arb_sim(
    *,
    spot_bars: list[tuple[int, float]],
    futures_bars: list[tuple[int, float]],
    funding_panel: dict[str, list[tuple[int, float]]],
    days_to_expiry_per_bar: list[float],
    params: BasisArbParams,
    starting_cash_usdt: float,
    futures_margin_pct: float = 0.5,
    mmr: float = 0.005,
    fee_bps: float = 5.0,
    funding_inst_id: str | None = None,
    bars_per_funding_cycle: int = 8,
    position_pct_per_entry: float = 0.95,
) -> BasisArbSimResult:
    """Bar-by-bar replay of the basis_arb strategy with cross-account margin.

    The bars are assumed pre-aligned (same length, same ts ordering, spot & futures
    bar at index i share a logical timestamp). Funding is applied to the futures
    account every ``bars_per_funding_cycle`` bars (OKX is 8h per cycle ≈ 8 × 1H).

    Args:
        spot_bars / futures_bars: list of ``(ts_ms, close_price)``, sorted ascending.
        funding_panel: ``{inst_id: [(ts_ms, rate_8h), ...]}``; if empty, funding = 0.
        days_to_expiry_per_bar: aligned with bars, days until the futures expires.
        params: ``BasisArbParams`` (entry/exit APR thresholds + force_close_days_before_expiry).
        starting_cash_usdt: total starting capital split across spot & futures sub-accounts.
        futures_margin_pct: fraction of starting cash allocated to futures sub-account.
        mmr: maintenance margin ratio for the futures account.
        fee_bps: flat round-trip fee per leg.
        funding_inst_id: which key in funding_panel to read funding rates from.
            Defaults to the first non-empty key.
        bars_per_funding_cycle: cadence at which apply_funding is called.
        position_pct_per_entry: fraction of spot_account cash to commit on each entry.
    """
    if len(spot_bars) != len(futures_bars) or len(spot_bars) != len(days_to_expiry_per_bar):
        raise ValueError(
            f"spot_bars ({len(spot_bars)}), futures_bars ({len(futures_bars)}), and "
            f"days_to_expiry_per_bar ({len(days_to_expiry_per_bar)}) must be the same length"
        )
    if not spot_bars:
        raise ValueError("at least one bar required")
    if not (0.0 < futures_margin_pct < 1.0):
        raise ValueError(f"futures_margin_pct must be in (0, 1); got {futures_margin_pct}")

    spot_acc = SpotCashAccount(cash_usdt=starting_cash_usdt * (1 - futures_margin_pct))
    fut_acc = FuturesCrossAccount(cash_usdt=starting_cash_usdt * futures_margin_pct, mmr=mmr)

    # Pick a default funding source if caller didn't specify
    chosen_funding_id = funding_inst_id
    if chosen_funding_id is None:
        chosen_funding_id = next(
            (k for k, v in funding_panel.items() if v), None
        )
    funding_series = funding_panel.get(chosen_funding_id or "", [])

    has_position = False
    n_entries = 0
    n_exits = 0
    equity_curve: list[tuple[int, float]] = []

    for i, ((ts, spot_px), (_, fut_px), dte) in enumerate(
        zip(spot_bars, futures_bars, days_to_expiry_per_bar, strict=True)
    ):
        # 1) Liquidation check (only if we have a futures position)
        if has_position and fut_acc.is_liquidated(mark_price=fut_px):
            fut_acc.force_liquidate(mark_price=fut_px)
            # Spot leg is now naked; basis_arb is delta-neutral by design, so close it too
            if spot_acc.spot_qty > 0:
                spot_acc.sell_spot(price=spot_px, qty=spot_acc.spot_qty, fee_bps=fee_bps)
            has_position = False
            n_exits += 1
            net_eq = spot_acc.equity(mark_price=spot_px) + fut_acc.equity(mark_price=fut_px)
            equity_curve.append((ts, net_eq))
            continue

        # 2) Funding cashflow on the futures leg
        if has_position and i > 0 and i % bars_per_funding_cycle == 0:
            rate = _latest_funding_rate(funding_series, ts)
            fut_acc.apply_funding(rate_8h=rate, mark_price=fut_px)

        # 3) Decision via existing pure-function pipeline
        ann_basis = annualized_basis(fut_px, spot_px, dte)
        action = basis_decision(ann_basis, dte, has_position, params)

        if action == BasisAction.ENTER:
            spot_budget = spot_acc.cash_usdt * position_pct_per_entry
            qty = spot_budget / spot_px if spot_px > 0 else 0.0
            if qty > 0:
                spot_acc.buy_spot(price=spot_px, qty=qty, fee_bps=fee_bps)
                fut_acc.short_futures(price=fut_px, qty=qty, fee_bps=fee_bps)
                has_position = True
                n_entries += 1
        elif action == BasisAction.EXIT:
            if spot_acc.spot_qty > 0:
                spot_acc.sell_spot(price=spot_px, qty=spot_acc.spot_qty, fee_bps=fee_bps)
            fut_acc.close_futures(price=fut_px, fee_bps=fee_bps)
            has_position = False
            n_exits += 1

        net_eq = spot_acc.equity(mark_price=spot_px) + fut_acc.equity(mark_price=fut_px)
        equity_curve.append((ts, net_eq))

    final_spot = spot_acc.equity(mark_price=spot_bars[-1][1])
    final_fut = fut_acc.equity(mark_price=futures_bars[-1][1])
    return BasisArbSimResult(
        starting_equity=starting_cash_usdt,
        net_final_equity=final_spot + final_fut,
        spot_final_equity=final_spot,
        futures_final_equity=final_fut,
        futures_liquidated=fut_acc.liquidated,
        n_entries=n_entries,
        n_exits=n_exits,
        total_funding_cashflow=fut_acc.funding_cashflow_total,
        equity_curve=equity_curve,
    )


__all__ = [
    "BasisArbSimResult",
    "FuturesCrossAccount",
    "SpotCashAccount",
    "run_basis_arb_sim",
]
