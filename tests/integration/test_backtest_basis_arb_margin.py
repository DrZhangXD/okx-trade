"""End-to-end integration: basis_arb cross-account margin simulator pipeline.

Per Plans 1-3 lesson: exercise the realistic library + CLI surface without
hitting OKX REST. The synthetic-bar scenario verifies the headline outcome
the margin simulator was built for — futures sub-account liquidates while the
spot sub-account survives (the failure mode NT's single MARGIN account hides).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_margin_sim_isolates_futures_blowout_from_spot_survival():
    """Engineered scenario: enter cash-and-carry; futures premium explodes;
    futures sub-account liquidates; spot sub-account survives intact.

    Note: mmr=0.8 is deliberately elevated (real OKX is ~0.005) so the test
    reaches the liquidation threshold within 200 synthetic bars.  The goal is
    to verify the *isolation mechanic*, not a realistic parameter set.
    """
    from okx_trade.backtest.basis_arb_sim import run_basis_arb_sim
    from okx_trade.strategies._signals import BasisArbParams

    n = 200
    base_ts = 1_700_000_000_000
    bar_ms = 3_600_000

    # Spot drifts up modestly (positive market environment)
    spot_bars = [(base_ts + i * bar_ms, 60_000.0 + 5.0 * i) for i in range(n)]
    # Futures: starts at 0.5% premium, blows out from bar 50 onward at +0.4%/bar
    futures_bars = []
    for i, (ts, s) in enumerate(spot_bars):
        if i < 50:
            premium = 0.005  # 0.5% — entry-worthy at 30-day APR
        else:
            premium = 0.005 + 0.004 * (i - 49)  # explode
        futures_bars.append((ts, s * (1 + premium)))

    # 30-day expiry, decreasing 0.5 days per bar
    dte = [max(0.5, 30.0 - i * 0.05) for i in range(n)]

    res = run_basis_arb_sim(
        spot_bars=spot_bars, futures_bars=futures_bars,
        funding_panel={"BTC-USDT-SWAP": []},  # no funding for cleanliness
        days_to_expiry_per_bar=dte,
        params=BasisArbParams(
            entry_basis_apr=0.05, exit_basis_apr=0.01,
            force_close_days_before_expiry=2,
        ),
        starting_cash_usdt=10_000.0,
        futures_margin_pct=0.5,
        mmr=0.8,   # elevated: triggers liq at ~bar 82 within the 200-bar window
        fee_bps=5.0,
    )

    # Headline: account isolation worked
    assert res.futures_liquidated is True, "engineered blowout should liquidate futures"
    assert res.spot_final_equity > 0, "spot sub-account must survive (no cross-contamination)"
    assert res.n_entries >= 1, "strategy should have entered when basis was favorable"

    # Net equity reflects the futures wipe, but spot is still > 0
    assert res.net_final_equity < res.starting_equity
    assert res.net_final_equity > 0  # spot has value even after futures liq

    # Equity curve aligned with bars
    assert len(res.equity_curve) == n
    # No NaN / inf in the equity curve
    for _ts, eq in res.equity_curve:
        assert eq == eq  # not NaN
        assert eq > -float("inf") and eq < float("inf")


def _run_backtest_cli(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "backtest.py"), *args],
        capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT),
    )


def test_use_margin_sim_rejects_bad_futures_suffix_before_network():
    """The --use-margin-sim path should validate the YYMMDD suffix before any
    REST call, so a malformed inst id fails fast (no bandwidth wasted)."""
    res = _run_backtest_cli([
        "--strategy", "basis_arb",
        "--use-margin-sim",
        "--spot-instrument-id", "BTC-USDT",
        "--futures-instrument-id", "BTC-USDT-NOTADATE",
    ])
    assert res.returncode != 0
    combined = (res.stdout or "") + (res.stderr or "")
    assert "expected YYMMDD suffix" in combined, combined


def test_basis_arb_missing_instrument_ids_fires_validation_before_branch():
    """Validation of --spot-instrument-id / --futures-instrument-id must fire
    BEFORE the --use-margin-sim branch, so error UX is identical regardless of flag."""
    res = _run_backtest_cli([
        "--strategy", "basis_arb",
        "--use-margin-sim",
    ])
    assert res.returncode != 0
    combined = (res.stdout or "") + (res.stderr or "")
    assert "需要 --spot-instrument-id" in combined, combined
