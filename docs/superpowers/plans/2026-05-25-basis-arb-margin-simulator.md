# Plan 6: basis_arb Cross-Account Margin Simulator

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a dedicated `basis_arb` simulator that models OKX's real margin behavior — spot legs on a cash account (no liquidation, no leverage) and futures legs on a separate cross-margin account that can be independently liquidated — so backtest equity curves reflect the actual tail risk of the strategy. The Plan 1 NT-based backtest stays for quick prototyping but is flagged as approximate.

**Architecture:** Standalone bar-driven simulator in `src/okx_trade/backtest/basis_arb_sim.py`, modeled on `scripts/backtest_funding_carry.py`'s pattern (zero NT, pure state machine). Inputs: spot bars + futures bars + (optional) funding panel. Two account dataclasses (`SpotCashAccount`, `FuturesCrossAccount`) with their own balance / unrealized-PnL / liquidation logic. Strategy decisions reuse the existing `basis_arb` decision pure-functions (or a thin shim if they don't exist as pure-functions yet — extract them in Task 1).

**Tech Stack:** Python 3.12+ only; numpy for vectorized PnL accumulation. No NT, no xgboost.

**Dependencies:** Plan 1 (funding panel) — funding history feeds the futures account funding cashflow.

**Roadmap reference:** [2026-05-25-backtest-completeness-roadmap.md](2026-05-25-backtest-completeness-roadmap.md) — Plan 6.

---

## File Structure

| Path | Responsibility | Status |
|---|---|---|
| `src/okx_trade/strategies/basis_arb.py` | Extract decision logic as pure functions (`basis_arb_decision`, `BasisArbAction`) if not already | modify |
| `src/okx_trade/backtest/basis_arb_sim.py` | New: `SpotCashAccount`, `FuturesCrossAccount`, `run_basis_arb_sim` | create |
| `scripts/backtest_basis_arb.py` | CLI wrapper: load bars + funding, invoke simulator, print summary + equity csv/plot | create |
| `scripts/backtest.py` | Re-route `--strategy basis_arb` to the dedicated simulator (replacing Plan 1's NT wrapper) when `--use-margin-sim` flag is set | modify |
| `tests/unit/backtest/test_basis_arb_sim.py` | Unit tests for account math + liquidation trigger | create |
| `tests/unit/strategies/test_basis_arb_decision.py` | Pure-function decision tests (may already exist; verify) | modify |
| `tests/integration/test_backtest_basis_arb_margin.py` | E2E scenario: synthetic bars with engineered drawdown → assert futures liquidated, spot survived | create |
| `docs/strategy_roadmap.md` | Mark basis_arb as "backtestable (margin-isolated sim available)" | modify |

---

## Conventions

Standard conventions per [2026-05-25-backtest-completeness-roadmap.md](2026-05-25-backtest-completeness-roadmap.md).

**Simulator scope (out of scope):** This is **not** a full exchange simulator. Out-of-scope: order book matching, partial fills, slippage modeling (use a flat bps cost), margin call ladder (OKX has tiered IMR/MMR — we approximate with a single MMR threshold). Document these limits in the simulator's docstring; the goal is to capture the **account-isolation risk** that NT's single-account default hides.

**MMR default:** 0.5% (close to OKX's tier-1 BTC perp MMR). User can override via CLI.

---

## Task 1: Extract `basis_arb_decision` as pure function (if needed)

**Files:**
- Modify: `src/okx_trade/strategies/basis_arb.py`
- Modify: `tests/unit/test_strategy_basis_arb.py`

- [ ] **Step 1: Audit existing structure**

```bash
grep -n "^def \|class BasisArb" src/okx_trade/strategies/basis_arb.py
```

If `basis_arb_decision()` already exists as a top-level pure function (mirroring `funding_carry_decision`), skip to Task 2.

- [ ] **Step 2: If embedded only in the strategy, extract**

Pull out the entry/exit logic into a module-level function:

```python
@dataclass(frozen=True, slots=True)
class BasisArbParams:
    entry_basis_apr: float = 0.05   # spread APR above which to enter
    exit_basis_apr: float = 0.01    # below this, close
    max_position_pct: float = 0.30


class BasisArbAction(str, Enum):
    HOLD = "hold"
    ENTER = "enter"
    EXIT = "exit"


def basis_arb_decision(
    spot_price: float, futures_price: float, days_to_expiry: float,
    *, has_position: bool, params: BasisArbParams,
) -> BasisArbAction:
    """Decide ENTER/HOLD/EXIT based on basis APR vs thresholds."""
    if spot_price <= 0 or days_to_expiry <= 0:
        return BasisArbAction.HOLD
    basis = (futures_price - spot_price) / spot_price
    apr = basis * (365 / days_to_expiry)
    if has_position:
        return BasisArbAction.EXIT if apr < params.exit_basis_apr else BasisArbAction.HOLD
    return BasisArbAction.ENTER if apr >= params.entry_basis_apr else BasisArbAction.HOLD
```

- [ ] **Step 3: Test the pure function**

Add to `tests/unit/test_strategy_basis_arb.py`:

```python
def test_basis_arb_decision_enters_when_apr_high():
    from okx_trade.strategies.basis_arb import basis_arb_decision, BasisArbAction, BasisArbParams
    # basis = (61000 - 60000) / 60000 = 1.67%; days_to_expiry=30 → APR = 1.67% × 365/30 = 20.3%
    action = basis_arb_decision(
        spot_price=60000, futures_price=61000, days_to_expiry=30,
        has_position=False, params=BasisArbParams(entry_basis_apr=0.05),
    )
    assert action == BasisArbAction.ENTER


def test_basis_arb_decision_holds_when_in_position_above_exit():
    from okx_trade.strategies.basis_arb import basis_arb_decision, BasisArbAction, BasisArbParams
    action = basis_arb_decision(
        spot_price=60000, futures_price=60500, days_to_expiry=30,
        has_position=True, params=BasisArbParams(exit_basis_apr=0.01),
    )
    # APR = 0.83% × 365/30 = 10% > exit; should HOLD
    assert action == BasisArbAction.HOLD
```

- [ ] **Step 4: Commit**

```bash
git add src/okx_trade/strategies/basis_arb.py tests/unit/test_strategy_basis_arb.py
git commit -m "refactor(basis_arb): extract basis_arb_decision as pure function"
```

---

## Task 2: `SpotCashAccount` + `FuturesCrossAccount` dataclasses

**Files:**
- Create: `src/okx_trade/backtest/basis_arb_sim.py`
- Create: `tests/unit/backtest/test_basis_arb_sim.py`

- [ ] **Step 1: Failing test for account math**

```python
"""Tests for basis_arb cross-account simulator."""
from __future__ import annotations

import pytest

from okx_trade.backtest.basis_arb_sim import (
    SpotCashAccount, FuturesCrossAccount,
)


def test_spot_cash_account_buy_then_mark_and_sell():
    acc = SpotCashAccount(cash_usdt=10_000.0)
    # Buy 0.1 BTC at $60k → spend 6000 USDT
    acc.buy_spot(price=60_000, qty=0.1, fee_bps=5.0)
    assert acc.cash_usdt == pytest.approx(10_000 - 6_000 - 6_000 * 0.0005)
    assert acc.spot_qty == 0.1

    # Mark at $61k → unrealized +$100
    assert acc.unrealized_pnl(mark_price=61_000) == pytest.approx(100.0)

    # Sell all at $61k → realize +$100, less fees
    acc.sell_spot(price=61_000, qty=0.1, fee_bps=5.0)
    expected_cash = (10_000 - 6_000 - 6_000 * 0.0005) + 6_100 - 6_100 * 0.0005
    assert acc.cash_usdt == pytest.approx(expected_cash)
    assert acc.spot_qty == 0.0


def test_futures_account_liquidates_when_equity_below_mmr():
    acc = FuturesCrossAccount(cash_usdt=1_000.0, mmr=0.005)
    # Short 0.1 BTC at $60k → notional 6000 USDT, IMR 5% → initial margin 300
    acc.short_futures(price=60_000, qty=0.1, fee_bps=5.0)
    assert not acc.is_liquidated(mark_price=60_000)

    # Price spikes to $70k (against the short) → unrealized loss = (60k - 70k) * 0.1 = -1000
    # Equity = 1000 - 1000 = 0 → margin ratio negative → liquidated
    assert acc.is_liquidated(mark_price=70_000)

    # Force liquidation: realize the loss, position closed
    acc.force_liquidate(mark_price=70_000)
    assert acc.futures_qty == 0.0
    assert acc.cash_usdt == pytest.approx(0.0, abs=10.0)  # ~equity wiped
```

- [ ] **Step 2: Implement**

```python
"""basis_arb cross-account margin simulator.

Models OKX's reality: spot leg sits in a cash sub-account (no leverage, no
liquidation risk); futures leg sits in a cross-margin sub-account that can
be independently liquidated when equity / notional < MMR.

Scope: account-isolation tail risk. NOT a full exchange simulator — no order
book, no partial fills, single-tier MMR (no IMR ladder).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SpotCashAccount:
    """Cash spot account: holds USDT + base asset, no leverage, no liquidation."""
    cash_usdt: float
    spot_qty: float = 0.0
    realized_pnl: float = 0.0

    def buy_spot(self, *, price: float, qty: float, fee_bps: float) -> None:
        cost = price * qty
        fee = cost * fee_bps / 10_000
        self.cash_usdt -= cost + fee
        self.spot_qty += qty

    def sell_spot(self, *, price: float, qty: float, fee_bps: float) -> None:
        proceeds = price * qty
        fee = proceeds * fee_bps / 10_000
        self.cash_usdt += proceeds - fee
        self.spot_qty -= qty

    def unrealized_pnl(self, *, mark_price: float) -> float:
        if self.spot_qty == 0:
            return 0.0
        # Vs notional at average cost? Simpler: vs current cash + mark
        return self.spot_qty * mark_price + self.cash_usdt - self._initial_equity()

    def _initial_equity(self) -> float:
        # Tracked separately — simpler API: return total equity at start
        return getattr(self, "_init_eq", self.cash_usdt + self.spot_qty * 0)

    def equity(self, *, mark_price: float) -> float:
        return self.cash_usdt + self.spot_qty * mark_price


@dataclass
class FuturesCrossAccount:
    """Cross-margin futures sub-account: gets liquidated when equity / |notional| < MMR.

    Convention: ``futures_qty > 0`` = long, ``< 0`` = short. ``entry_price`` is
    the weighted average entry. Funding is applied externally via ``apply_funding``.
    """
    cash_usdt: float
    mmr: float = 0.005  # maintenance margin ratio (0.5% default)
    futures_qty: float = 0.0  # signed
    entry_price: float = 0.0
    realized_pnl: float = 0.0
    funding_cashflow_total: float = 0.0
    _liquidated: bool = False

    def short_futures(self, *, price: float, qty: float, fee_bps: float) -> None:
        # Add to short — qty is positive size to short
        notional = price * qty
        fee = notional * fee_bps / 10_000
        self.cash_usdt -= fee
        new_size = abs(self.futures_qty) + qty
        # Weighted avg entry
        self.entry_price = (abs(self.futures_qty) * self.entry_price + qty * price) / new_size
        self.futures_qty -= qty  # short → negative

    def close_futures(self, *, price: float, fee_bps: float) -> None:
        if self.futures_qty == 0:
            return
        qty = abs(self.futures_qty)
        notional = price * qty
        fee = notional * fee_bps / 10_000
        # PnL = (entry - mark) * qty for short, (mark - entry) * qty for long
        pnl = (self.entry_price - price) * qty if self.futures_qty < 0 else (price - self.entry_price) * qty
        self.cash_usdt += pnl - fee
        self.realized_pnl += pnl - fee
        self.futures_qty = 0.0
        self.entry_price = 0.0

    def unrealized_pnl(self, *, mark_price: float) -> float:
        if self.futures_qty == 0:
            return 0.0
        qty = abs(self.futures_qty)
        return (self.entry_price - mark_price) * qty if self.futures_qty < 0 else (mark_price - self.entry_price) * qty

    def equity(self, *, mark_price: float) -> float:
        return self.cash_usdt + self.unrealized_pnl(mark_price=mark_price)

    def is_liquidated(self, *, mark_price: float) -> bool:
        if self._liquidated or self.futures_qty == 0:
            return self._liquidated
        eq = self.equity(mark_price=mark_price)
        notional = abs(self.futures_qty) * mark_price
        if notional == 0:
            return False
        return eq / notional < self.mmr

    def force_liquidate(self, *, mark_price: float) -> None:
        """Close at mark, wipe remaining equity (worst-case approximation)."""
        if self.futures_qty == 0:
            return
        # Realize loss at mark
        qty = abs(self.futures_qty)
        pnl = (self.entry_price - mark_price) * qty if self.futures_qty < 0 else (mark_price - self.entry_price) * qty
        self.cash_usdt += pnl
        # OKX takes the maintenance margin remainder on full liq
        self.cash_usdt = max(0.0, self.cash_usdt)
        self.futures_qty = 0.0
        self.entry_price = 0.0
        self._liquidated = True

    def apply_funding(self, *, rate_8h: float, mark_price: float) -> None:
        """Settle one funding cycle. Convention: positive rate ⇒ long pays short."""
        if self.futures_qty == 0:
            return
        notional = abs(self.futures_qty) * mark_price
        # Short receives positive funding; long pays
        sign = 1 if self.futures_qty < 0 else -1
        cashflow = sign * notional * rate_8h
        self.cash_usdt += cashflow
        self.funding_cashflow_total += cashflow
```

- [ ] **Step 3: Run, commit**

```bash
git add src/okx_trade/backtest/basis_arb_sim.py tests/unit/backtest/test_basis_arb_sim.py
git commit -m "feat(basis_arb_sim): add SpotCashAccount + FuturesCrossAccount models"
```

---

## Task 3: `run_basis_arb_sim` orchestrator

**Files:**
- Modify: `src/okx_trade/backtest/basis_arb_sim.py`
- Modify: `tests/unit/backtest/test_basis_arb_sim.py`

- [ ] **Step 1: Failing test — engineer a drawdown that liquidates futures but not spot**

```python
def test_simulator_liquidates_futures_when_basis_blows_out():
    """Scenario: enter carry; futures spikes upward (basis explodes), futures account
    gets liquidated; spot still appreciates and survives."""
    from okx_trade.backtest.basis_arb_sim import run_basis_arb_sim
    from okx_trade.strategies.basis_arb import BasisArbParams

    # Synthetic bars: 30 bars
    spot_bars = [(i, 60_000 + (i if i < 15 else 200 * (i - 15))) for i in range(30)]
    # Futures: basis blows out — 1% premium at start, 30% premium by bar 20
    futures_bars = [(i, spot_bars[i][1] * (1 + 0.01 + 0.015 * max(0, i - 10))) for i in range(30)]
    # No funding for simplicity
    funding_panel = {"BTC-USDT-SWAP": []}

    result = run_basis_arb_sim(
        spot_bars=spot_bars, futures_bars=futures_bars,
        funding_panel=funding_panel, days_to_expiry_per_bar=[30.0 - i * 0.5 for i in range(30)],
        params=BasisArbParams(entry_basis_apr=0.05, exit_basis_apr=0.01, max_position_pct=0.50),
        starting_cash_usdt=10_000, futures_margin_pct=0.5,  # 50% to futures sub
        mmr=0.005, fee_bps=5.0,
    )
    assert result.futures_liquidated is True
    assert result.spot_final_equity > 0  # spot survived
    # Net equity reflects futures wipe
    assert result.net_final_equity < result.starting_equity
```

- [ ] **Step 2: Implement orchestrator**

```python
from dataclasses import dataclass, field

from ..strategies.basis_arb import BasisArbAction, BasisArbParams, basis_arb_decision


@dataclass(frozen=True, slots=True)
class BasisArbSimResult:
    starting_equity: float
    net_final_equity: float
    spot_final_equity: float
    futures_final_equity: float
    futures_liquidated: bool
    n_entries: int
    n_exits: int
    total_funding: float
    equity_curve: list[tuple[int, float]]  # (ts_or_idx, net_equity)


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
) -> BasisArbSimResult:
    """Run the basis_arb decision loop over interleaved bars with margin isolation."""
    assert len(spot_bars) == len(futures_bars) == len(days_to_expiry_per_bar), \
        "bar arrays must be aligned"

    spot_acc = SpotCashAccount(cash_usdt=starting_cash_usdt * (1 - futures_margin_pct))
    fut_acc = FuturesCrossAccount(cash_usdt=starting_cash_usdt * futures_margin_pct, mmr=mmr)
    has_position = False
    n_entries = n_exits = 0
    equity_curve: list[tuple[int, float]] = []

    for i, ((ts, spot_px), (_, fut_px), dte) in enumerate(
        zip(spot_bars, futures_bars, days_to_expiry_per_bar, strict=True)
    ):
        # 1. Liquidation check
        if has_position and fut_acc.is_liquidated(mark_price=fut_px):
            fut_acc.force_liquidate(mark_price=fut_px)
            # Spot leg is now naked — exit it too (no point holding naked spot on a carry strategy)
            if spot_acc.spot_qty > 0:
                spot_acc.sell_spot(price=spot_px, qty=spot_acc.spot_qty, fee_bps=fee_bps)
            has_position = False
            n_exits += 1
            equity_curve.append((ts, spot_acc.equity(mark_price=spot_px) + fut_acc.equity(mark_price=fut_px)))
            continue

        # 2. Funding settlement (every 8 bars assuming 1H bars; tune for your bar size)
        if i % 8 == 0 and has_position:
            rates = funding_panel.get("BTC-USDT-SWAP", [])
            if rates:
                # Find most recent rate at-or-before ts
                rate = next((r for t, r in reversed(rates) if t <= ts), 0.0)
                fut_acc.apply_funding(rate_8h=rate, mark_price=fut_px)

        # 3. Decision
        action = basis_arb_decision(
            spot_price=spot_px, futures_price=fut_px, days_to_expiry=dte,
            has_position=has_position, params=params,
        )
        if action == BasisArbAction.ENTER:
            spot_budget = spot_acc.cash_usdt * 0.95  # keep small reserve
            qty = spot_budget / spot_px
            spot_acc.buy_spot(price=spot_px, qty=qty, fee_bps=fee_bps)
            fut_acc.short_futures(price=fut_px, qty=qty, fee_bps=fee_bps)
            has_position = True
            n_entries += 1
        elif action == BasisArbAction.EXIT:
            if spot_acc.spot_qty > 0:
                spot_acc.sell_spot(price=spot_px, qty=spot_acc.spot_qty, fee_bps=fee_bps)
            fut_acc.close_futures(price=fut_px, fee_bps=fee_bps)
            has_position = False
            n_exits += 1

        equity_curve.append((ts, spot_acc.equity(mark_price=spot_px) + fut_acc.equity(mark_price=fut_px)))

    spot_eq = spot_acc.equity(mark_price=spot_bars[-1][1])
    fut_eq = fut_acc.equity(mark_price=futures_bars[-1][1])
    return BasisArbSimResult(
        starting_equity=starting_cash_usdt,
        net_final_equity=spot_eq + fut_eq,
        spot_final_equity=spot_eq,
        futures_final_equity=fut_eq,
        futures_liquidated=fut_acc._liquidated,
        n_entries=n_entries, n_exits=n_exits,
        total_funding=fut_acc.funding_cashflow_total,
        equity_curve=equity_curve,
    )
```

- [ ] **Step 3: Run test, commit**

```bash
git add src/okx_trade/backtest/basis_arb_sim.py tests/unit/backtest/test_basis_arb_sim.py
git commit -m "feat(basis_arb_sim): add run_basis_arb_sim orchestrator with margin isolation"
```

---

## Task 4: `scripts/backtest_basis_arb.py` CLI

**Files:**
- Create: `scripts/backtest_basis_arb.py`

- [ ] **Step 1: Implement** (pattern mirrors `scripts/backtest_funding_carry.py`)

```python
"""basis_arb cross-account margin simulator CLI.

Uses the dedicated sim in src/okx_trade/backtest/basis_arb_sim.py rather than NT,
because NT's default SimulatedExchange uses a single MARGIN account for both legs,
which OVERSTATES survivability under sharp basis blowouts.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from okx_trade import OKXRestClient, OKXSettings  # noqa: E402
from okx_trade.backtest.basis_arb_sim import run_basis_arb_sim  # noqa: E402
from okx_trade.backtest.data_loader import prepare_backtest_catalog, prepare_funding_panel  # noqa: E402
from okx_trade.strategies.basis_arb import BasisArbParams  # noqa: E402


def _days_to_expiry(inst_id: str, ts_ms: int) -> float:
    """Parse the YYMMDD suffix from an OKX dated future and return days until expiry."""
    # "BTC-USDT-250627" → 2025-06-27
    suffix = inst_id.split("-")[-1]
    expiry = datetime.strptime("20" + suffix, "%Y%m%d").replace(tzinfo=timezone.utc)
    bar_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return max(0.5, (expiry - bar_dt).total_seconds() / 86_400)


async def _main(args: argparse.Namespace) -> None:
    catalog = Path(args.catalog)
    async with OKXRestClient(OKXSettings()) as client:
        _, spot_nt_bars = await prepare_backtest_catalog(
            client, args.spot_inst, bar_period=args.bar, total=args.total_bars,
            catalog_path=catalog, reuse=args.reuse_data,
        )
        _, fut_nt_bars = await prepare_backtest_catalog(
            client, args.futures_inst, bar_period=args.bar, total=args.total_bars,
            catalog_path=catalog, reuse=args.reuse_data,
        )
        funding = {}
        if args.perp_inst:
            panel = await prepare_funding_panel(
                client, args.perp_inst, total=args.funding_total,
                catalog_path=catalog, reuse_cache=args.reuse_data,
            )
            funding[args.perp_inst] = list(zip(panel.ts_ms, panel.rates, strict=True))

    spot_bars = [(int(b.ts_event // 1_000_000), float(b.close)) for b in spot_nt_bars]
    fut_bars = [(int(b.ts_event // 1_000_000), float(b.close)) for b in fut_nt_bars]
    # Align (truncate to common length)
    n = min(len(spot_bars), len(fut_bars))
    spot_bars, fut_bars = spot_bars[-n:], fut_bars[-n:]
    dte = [_days_to_expiry(args.futures_inst, ts) for ts, _ in fut_bars]

    result = run_basis_arb_sim(
        spot_bars=spot_bars, futures_bars=fut_bars, funding_panel=funding,
        days_to_expiry_per_bar=dte,
        params=BasisArbParams(
            entry_basis_apr=args.entry_apr, exit_basis_apr=args.exit_apr,
            max_position_pct=args.max_position_pct,
        ),
        starting_cash_usdt=args.equity, futures_margin_pct=args.futures_margin_pct,
        mmr=args.mmr, fee_bps=args.fee_bps,
    )
    print(f"net final equity: ${result.net_final_equity:,.2f}  "
          f"(spot ${result.spot_final_equity:,.2f}, futures ${result.futures_final_equity:,.2f})")
    print(f"futures liquidated: {result.futures_liquidated}")
    print(f"entries={result.n_entries}  exits={result.n_exits}  "
          f"total_funding=${result.total_funding:,.2f}")
    if args.equity_csv:
        import csv
        with open(args.equity_csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["ts_ms", "net_equity"])
            writer.writerows(result.equity_curve)
        print(f"wrote {args.equity_csv}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--spot-inst", required=True)
    p.add_argument("--futures-inst", required=True, help="dated future like BTC-USDT-250627")
    p.add_argument("--perp-inst", help="optional perp for funding context")
    p.add_argument("--bar", default="1H")
    p.add_argument("--total-bars", type=int, default=1500)
    p.add_argument("--funding-total", type=int, default=750)
    p.add_argument("--equity", type=float, default=10_000)
    p.add_argument("--futures-margin-pct", type=float, default=0.5)
    p.add_argument("--mmr", type=float, default=0.005)
    p.add_argument("--fee-bps", type=float, default=5.0)
    p.add_argument("--entry-apr", type=float, default=0.05)
    p.add_argument("--exit-apr", type=float, default=0.01)
    p.add_argument("--max-position-pct", type=float, default=0.50)
    p.add_argument("--catalog", default="./data")
    p.add_argument("--reuse-data", action="store_true")
    p.add_argument("--equity-csv")
    asyncio.run(_main(p.parse_args()))
```

- [ ] **Step 2: Smoke test**

```bash
python scripts/backtest_basis_arb.py --spot-inst BTC-USDT \
    --futures-inst BTC-USDT-250627 --perp-inst BTC-USDT-SWAP \
    --total-bars 500 --reuse-data --equity-csv var/basis_arb_equity.csv
```

- [ ] **Step 3: Commit**

```bash
git add scripts/backtest_basis_arb.py
git commit -m "feat(scripts): add basis_arb margin-isolated backtest CLI"
```

---

## Task 5: Wire `--use-margin-sim` into `scripts/backtest.py`

**Files:**
- Modify: `scripts/backtest.py`

- [ ] **Step 1: Add flag + dispatch**

In `_run_basis_arb` (from Plan 1), branch:

```python
async def _run_basis_arb(args: argparse.Namespace) -> object:
    if args.use_margin_sim:
        # Delegate to the dedicated simulator
        from okx_trade.backtest.basis_arb_sim import run_basis_arb_sim
        # ... build inputs same as scripts/backtest_basis_arb.py and return result
        ...
        return result  # Different return type — print summary in main()
    # else: fall through to NT-based path from Plan 1 (with the existing caveat warning)
    ...
```

```python
    parser.add_argument("--use-margin-sim", action="store_true",
                        help="basis_arb only: use cross-account margin simulator (recommended)")
```

- [ ] **Step 2: Smoke**

```bash
python scripts/backtest.py --strategy basis_arb --use-margin-sim \
    --spot-instrument-id BTC-USDT --futures-instrument-id BTC-USDT-250627 \
    --total-bars 500 --reuse-data
```

- [ ] **Step 3: Commit**

```bash
git add scripts/backtest.py
git commit -m "feat(backtest): add --use-margin-sim flag for basis_arb"
```

---

## Task 6: Integration test (engineered drawdown)

**Files:**
- Create: `tests/integration/test_backtest_basis_arb_margin.py`

- [ ] **Step 1: Test asserts the headline outcome — futures liquidated, spot survived**

(Same logic as Task 3 unit test, but at integration level with realistic bar count.)

- [ ] **Step 2: Commit**

---

## Task 7: Update docs

- [ ] `docs/strategy_roadmap.md`: basis_arb → "backtestable (use --use-margin-sim for accurate tail risk)".
- [ ] Add a "When to use margin-sim vs NT path" callout in `docs/operations.md`.
- [ ] Commit.

---

## Self-Review Checklist

- [ ] Spot account math (buy/mark/sell/equity) matches by-hand spot trade arithmetic.
- [ ] Futures account liquidates when synthetic adverse move drives equity/notional < MMR.
- [ ] Funding cashflow applied with correct sign (short receives positive funding).
- [ ] Simulator returns a finite equity curve for all bars.
- [ ] CLI completes for real BTC-USDT / BTC-USDT-<near-expiry> data.

---

## Execution Handoff

**1. Subagent-Driven (recommended)** — `superpowers:subagent-driven-development`. Tasks 1–3 (pure logic) excellent for parallel subagents; Tasks 4–6 sequential.

**2. Inline Execution** — `superpowers:executing-plans`.
