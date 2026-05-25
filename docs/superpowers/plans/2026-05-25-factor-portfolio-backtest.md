# Plan 5: factor_portfolio Integration into Main Backtest CLI

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `python scripts/backtest.py --strategy factor_portfolio --config configs/factor_portfolio.yaml` a one-command path that (a) verifies factor approvals, (b) warms the panel cache offline if missing, (c) runs the full backtest via the existing `research.runtime.cmd_backtest_portfolio()`.

**Architecture:** The research lab already implements 95% of this — `okx_trade.research.cli:_cmd_backtest_portfolio_online` is functional and `factor_portfolio.py:_load_warmup_panel` reads parquet panels. The remaining work is **integration glue**: a `_run_factor_portfolio()` in `scripts/backtest.py` that pre-flights yaml + cache + calls the runtime; a CLI shortcut `scripts/factor_panel_warm.py` for the panel-cache step; documentation. No new strategy logic.

**Tech Stack:** Existing `okx_trade.research.*`, no new deps.

**Dependencies:** Plan 1 (funding panel) is consumed by some factors (e.g. `funding_skew`); this plan should run **after** Plan 1 so factor backtests see consistent funding data.

**Roadmap reference:** [2026-05-25-backtest-completeness-roadmap.md](2026-05-25-backtest-completeness-roadmap.md) — Plan 5.

---

## File Structure

| Path | Responsibility | Status |
|---|---|---|
| `scripts/factor_panel_warm.py` | CLI: fetch_panel → write parquet (replaces ad-hoc one-off scripts) | create |
| `scripts/backtest.py` | Register `factor_portfolio` + `_run_factor_portfolio` | modify |
| `src/okx_trade/research/runtime.py` | Add `ensure_panel_cached()` helper extracted from existing code paths | modify |
| `configs/factor_portfolio.yaml` | (no schema change; ensure example has ≥ 1 approved factor) | modify |
| `tests/unit/research/test_runtime_ensure_panel.py` | Unit test for `ensure_panel_cached` cache-hit/miss behavior | create |
| `tests/integration/test_backtest_factor_portfolio.py` | E2E: tiny yaml + cached panel → run → finite stats | create |
| `docs/strategy_roadmap.md` | Mark factor_portfolio as backtestable via main CLI | modify |
| `docs/operations.md` | Add "factor_portfolio one-pager" workflow section | modify |

---

## Conventions

Standard conventions per [2026-05-25-backtest-completeness-roadmap.md](2026-05-25-backtest-completeness-roadmap.md).

The research module uses its own parquet cache (`${cache_dir}/panel_<sha1>.parquet`), separate from the `${catalog}/funding/...` layout introduced in Plan 1. This is intentional — the panel cache is content-addressed (inst_ids × bar × time-range hashed) so callers don't manage filenames. We **keep** this convention; don't unify it with Plan 1's per-instrument layout.

---

## Task 1: `ensure_panel_cached()` runtime helper

**Files:**
- Modify: `src/okx_trade/research/runtime.py`
- Create: `tests/unit/research/test_runtime_ensure_panel.py`

- [ ] **Step 1: Locate current cache-or-fetch code in runtime.py**

```bash
grep -n "fetch_panel\|_cache_key\|_load_cache" src/okx_trade/research/runtime.py
```

- [ ] **Step 2: Failing test for the new helper**

```python
"""Test ensure_panel_cached: cache-hit short-circuits REST."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_ensure_panel_cached_skips_fetch_when_cache_present(tmp_path, monkeypatch):
    from okx_trade.research.runtime import ensure_panel_cached
    # Simulate an existing cache file at the deterministic path
    inst_ids = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    bar = "1H"
    start_ms, end_ms = 1_700_000_000_000, 1_700_500_000_000
    include = ("close", "volume", "funding")

    from okx_trade.research.data import _cache_key  # internal helper
    cache_path = tmp_path / f"panel_{_cache_key(inst_ids, bar, start_ms, end_ms, include)}.parquet"
    cache_path.write_bytes(b"\x00" * 16)  # dummy file; ensure_panel_cached only checks existence

    fake_rest = AsyncMock()
    res = await ensure_panel_cached(
        rest_client=fake_rest, inst_ids=inst_ids, bar=bar,
        start_ms=start_ms, end_ms=end_ms, include=include,
        cache_dir=tmp_path,
    )
    assert res == cache_path
    # No REST calls should have happened
    fake_rest.market.get_candles_extended.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_panel_cached_fetches_when_missing(tmp_path, monkeypatch):
    from okx_trade.research.runtime import ensure_panel_cached

    async def fake_fetch_panel(*_, cache_dir, **kw):
        # Simulate fetch_panel writing the parquet
        from okx_trade.research.data import _cache_key
        key = _cache_key(kw["inst_ids"], kw["bar"], kw["start_ms"], kw["end_ms"], kw["include"])
        (cache_dir / f"panel_{key}.parquet").write_bytes(b"\x00")
        return None  # caller doesn't use return value, only the side-effect parquet

    import okx_trade.research.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "fetch_panel", fake_fetch_panel)

    fake_rest = AsyncMock()
    res = await ensure_panel_cached(
        rest_client=fake_rest, inst_ids=["X"], bar="1H",
        start_ms=1, end_ms=2, include=("close",), cache_dir=tmp_path,
    )
    assert res.exists()
```

- [ ] **Step 3: Implement helper**

```python
# Append to src/okx_trade/research/runtime.py

from pathlib import Path

from .data import _cache_key, fetch_panel


async def ensure_panel_cached(
    *,
    rest_client,
    inst_ids: list[str],
    bar: str,
    start_ms: int,
    end_ms: int,
    include: tuple[str, ...] = ("close", "volume", "funding", "open_interest"),
    cache_dir: Path,
) -> Path:
    """Return path to cached panel parquet; fetch + write if missing.

    Returns:
        Absolute path to the parquet file (always exists on success).
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(list(inst_ids), bar, start_ms, end_ms, tuple(include))
    cache_path = cache_dir / f"panel_{key}.parquet"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path
    await fetch_panel(
        rest_client=rest_client, inst_ids=list(inst_ids),
        start_ms=start_ms, end_ms=end_ms, bar=bar,
        include=list(include), cache_dir=cache_dir,
    )
    if not cache_path.exists():
        raise RuntimeError(f"fetch_panel did not produce expected cache at {cache_path}")
    return cache_path
```

- [ ] **Step 4: Run, commit**

```bash
git add src/okx_trade/research/runtime.py tests/unit/research/test_runtime_ensure_panel.py
git commit -m "feat(research): add ensure_panel_cached helper for cache-first fetch"
```

---

## Task 2: `scripts/factor_panel_warm.py` CLI

**Files:**
- Create: `scripts/factor_panel_warm.py`

- [ ] **Step 1: Implement**

```python
"""Warm the factor research panel cache for a given config.

Usage:
    python scripts/factor_panel_warm.py --yaml configs/factor_portfolio.yaml \\
        --total-bars 2000 --bar 1H
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import yaml as _yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from okx_trade import OKXRestClient, OKXSettings  # noqa: E402
from okx_trade.research.runtime import ensure_panel_cached  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--yaml", required=True, help="factor_portfolio.yaml path")
    p.add_argument("--bar", default="1H")
    p.add_argument("--total-bars", type=int, default=2000)
    p.add_argument("--cache-dir", default="./data/research_panel")
    p.add_argument("--include", default="close,volume,funding,open_interest")
    return p.parse_args()


async def _main(args: argparse.Namespace) -> None:
    cfg = _yaml.safe_load(Path(args.yaml).read_text()) or {}
    inst_ids = cfg.get("instrument_ids") or []
    if not inst_ids:
        raise SystemExit(f"[error] {args.yaml} has no instrument_ids")
    bar_hours = {"1m": 1/60, "5m": 5/60, "1H": 1, "4H": 4, "1D": 24}[args.bar]
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.total_bars * bar_hours * 3_600_000)
    include = tuple(args.include.split(","))
    cache_dir = Path(args.cache_dir)

    async with OKXRestClient(OKXSettings()) as client:
        path = await ensure_panel_cached(
            rest_client=client, inst_ids=inst_ids, bar=args.bar,
            start_ms=start_ms, end_ms=end_ms, include=include,
            cache_dir=cache_dir,
        )
    print(f"[warm] panel cached at {path}  ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
```

- [ ] **Step 2: Manual smoke**

```bash
python scripts/factor_panel_warm.py --yaml configs/factor_portfolio.yaml --total-bars 500
```

- [ ] **Step 3: Commit**

```bash
git add scripts/factor_panel_warm.py
git commit -m "feat(scripts): add factor_panel_warm CLI for one-shot panel caching"
```

---

## Task 3: Register `factor_portfolio` in `scripts/backtest.py`

**Files:**
- Modify: `scripts/backtest.py`

- [ ] **Step 1: Implement `_run_factor_portfolio`**

```python
async def _run_factor_portfolio(args: argparse.Namespace) -> dict:
    """Backtest factor_portfolio using the research runtime's existing path.

    Returns a summary dict (not BacktestSummary — research runtime has its own).
    """
    from okx_trade.research.runtime import cmd_backtest_portfolio, ensure_panel_cached
    import yaml as _yaml
    import time

    yaml_path = Path(args.factor_yaml)
    if not yaml_path.exists():
        raise FileNotFoundError(f"yaml not found: {yaml_path}")
    yaml_cfg = _yaml.safe_load(yaml_path.read_text()) or {}
    if not yaml_cfg.get("factor_weights"):
        raise ValueError(
            f"{yaml_path} has empty factor_weights; approve at least one factor first: "
            "`python -m okx_trade.research approve <factor> <weight>`"
        )

    # Pre-warm panel cache
    cache_dir = Path(args.research_cache_dir)
    bar_hours = {"1m": 1/60, "5m": 5/60, "1H": 1, "4H": 4, "1D": 24}[args.signal_bar]
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.total_bars * bar_hours * 3_600_000)
    inst_ids = yaml_cfg.get("instrument_ids") or []
    include = ("close", "volume", "funding", "open_interest")

    async with OKXRestClient(OKXSettings()) as client:
        await ensure_panel_cached(
            rest_client=client, inst_ids=inst_ids, bar=args.signal_bar,
            start_ms=start_ms, end_ms=end_ms, include=include, cache_dir=cache_dir,
        )
        summary = await cmd_backtest_portfolio(
            rest_client=client, yaml_cfg=yaml_cfg,
            bar=args.signal_bar, total_bars=args.total_bars,
            catalog_path=Path(args.catalog),
            taker_fee_bps=args.taker_fee_bps, maker_fee_bps=args.maker_fee_bps,
            warmup_days=args.warmup_days, warmup_panel_dir=cache_dir,
        )
    return summary
```

- [ ] **Step 2: Add CLI args**

```python
    parser.add_argument("--factor-yaml", default="configs/factor_portfolio.yaml")
    parser.add_argument("--research-cache-dir", default="./data/research_panel")
    parser.add_argument("--warmup-days", type=int, default=30)
    parser.add_argument("--taker-fee-bps", type=float, default=5.0)
    parser.add_argument("--maker-fee-bps", type=float, default=2.0)
```

- [ ] **Step 3: Add dispatch + smoke test**

```bash
python scripts/backtest.py --strategy factor_portfolio --signal-bar 1H --total-bars 500
```

- [ ] **Step 4: Commit**

```bash
git add scripts/backtest.py
git commit -m "feat(backtest): wire factor_portfolio via research runtime + auto cache warm"
```

---

## Task 4: Integration test

**Files:**
- Create: `tests/integration/test_backtest_factor_portfolio.py`
- Create: `tests/integration/fixtures/factor_portfolio_minimal.yaml` (1 approved factor, 3 instruments)
- (Reuse) `tests/integration/fixtures/funding_btc_swap.parquet` from Plan 1 for the funding factor

- [ ] **Step 1: Test**

```python
"""End-to-end factor_portfolio backtest with a minimal config (no network if cache exists)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_factor_portfolio_backtest_runs_to_finite_summary(tmp_path):
    pytest.importorskip("nautilus_trader")
    # Construct minimal yaml; pre-seed cache with a generated panel; run.
    # Assert summary contains net_pnl, sharpe, max_drawdown and all are finite.
```

- [ ] **Step 2: Commit**

---

## Task 5: Update docs

- [ ] `docs/strategy_roadmap.md`: factor_portfolio → "backtestable via main CLI (auto-warms panel cache)".
- [ ] `docs/operations.md`: add 30-line workflow:
  - approve factors via research CLI
  - warm panel (or rely on auto-warm)
  - run `python scripts/backtest.py --strategy factor_portfolio`
- [ ] Commit.

---

## Self-Review Checklist

- [ ] `ensure_panel_cached` short-circuits when cache present.
- [ ] `scripts/backtest.py --strategy factor_portfolio` works without manually running `factor_panel_warm.py` first.
- [ ] Clear error when no factors approved.

---

## Execution Handoff

**1. Subagent-Driven (recommended)** — `superpowers:subagent-driven-development`.
**2. Inline Execution** — `superpowers:executing-plans`.

This plan is the smallest of the six. Inline execution is also reasonable if the engineer is familiar with the research module.
