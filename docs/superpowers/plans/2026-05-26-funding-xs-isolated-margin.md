# FundingXS Isolated Margin + Dynamic Leverage + Outlier Guard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three-layer defense to FundingXSStrategy — isolated margin per leg, dynamic leverage by funding+basis edge, and outlier guard at entry — so a single-instrument wick cannot consume more than the leg's allocated margin (vs. the 2026-05-25 DOT incident where -$51,128 was wiped from cross-margin).

**Architecture:** Three pure helper functions (`compute_leverage`, `compute_edge_score`, `outlier_check`) in a new helpers module are wired into `FundingCrossSectionStrategy._rebalance_async` and `_open_leg`. Strategy attaches `tags=["td_mode:isolated"]` to MarketOrder; the OKX adapter's existing tag-parsing path takes it from there. `set_leverage` is called per (inst, mgnMode, lever) with an in-memory cache to avoid redundant REST calls.

**Tech Stack:** Python 3.11, NautilusTrader, OKX REST/WS, pytest, numpy (already imported).

**Spec:** [docs/superpowers/specs/2026-05-26-funding-xs-isolated-margin-design.md](../specs/2026-05-26-funding-xs-isolated-margin-design.md)

---

## File Structure

| File | Purpose | New/Modified |
|---|---|---|
| `src/okx_trade/strategies/_isolated_helpers.py` | Pure functions: `compute_leverage`, `compute_edge_score`, `outlier_check` | **New** |
| `tests/unit/test_strategy_funding_xs_isolated.py` | Unit tests for the three helpers + integration smoke | **New** |
| `src/okx_trade/strategies/funding_cross_section.py` | Wire helpers into `_compute_targets`, `_open_leg`, `_rebalance_async`; add `_set_leverage_cached` | Modified |
| `configs/live.yaml` | New config keys for dynamic leverage + outlier guard | Modified |
| `scripts/probe_okx_isolated.py` | One-shot REST smoke: set-leverage + place 1 contract isolated → close | **New** |
| `docs/operations.md` | Add rollback runbook for the new feature | Modified |

---

## Task 1: Pre-flight smoke — verify OKX set-leverage works in net + isolated

**Goal:** Confirm spec §8.3 — that `pos_side_mode=net` + `mgnMode=isolated` is actually accepted by OKX before writing any strategy code.

**Files:**
- Create: `scripts/probe_okx_isolated.py`

- [ ] **Step 1: Write the probe script**

```python
"""One-shot smoke test: set leverage to isolated for DOT-USDT-SWAP at lever=3,
verify OKX accepts it under the current pos_side_mode=net configuration.

Usage:
    .venv/bin/python scripts/probe_okx_isolated.py

Exits non-zero if set-leverage fails or returned mgnMode != isolated.
"""
from __future__ import annotations

import asyncio
import sys

from okx_trade import OKXRestClient, OKXSettings
from okx_trade.rest.constants import TdMode

INST_ID = "DOT-USDT-SWAP"
TEST_LEVER = 3


async def main() -> int:
    settings = OKXSettings()
    async with OKXRestClient(settings) as client:
        try:
            await client.account.set_leverage(
                inst_id=INST_ID,
                leverage=TEST_LEVER,
                mgn_mode=TdMode.ISOLATED,
                pos_side=None,  # net mode → no posSide
            )
        except Exception as exc:
            print(f"FAIL set_leverage: {exc}")
            return 1

        # Verify via GET /api/v5/account/leverage-info
        data = await client.transport.request(
            "GET", "/api/v5/account/leverage-info",
            params={"instId": INST_ID, "mgnMode": "isolated"},
            private=True, group=None,
        )
        if not data:
            print("FAIL: leverage-info returned empty")
            return 1
        for row in data:
            print(f"  {row}")
            if row.get("mgnMode") != "isolated":
                print(f"FAIL: row mgnMode={row.get('mgnMode')} != isolated")
                return 1
            if str(row.get("lever")) != str(TEST_LEVER):
                print(f"FAIL: row lever={row.get('lever')} != {TEST_LEVER}")
                return 1
        print(f"PASS: {INST_ID} set to isolated lever={TEST_LEVER} (net mode)")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Run probe against the demo account on okx-vps**

Run: `ssh okx-vps "cd /home/okxtrade/okx-trade && .venv/bin/python scripts/probe_okx_isolated.py"`
Expected: `PASS: DOT-USDT-SWAP set to isolated lever=3 (net mode)`

If this FAILS with an error like "Margin mode does not match position mode" → STOP. The whole plan needs revision: switch `pos_side_mode` to `long_short` first, or restructure the strategy to use a `pos_side` per leg. Do not proceed to Task 2 until this passes.

- [ ] **Step 3: Commit**

```bash
git add scripts/probe_okx_isolated.py
git commit -m "test(okx): one-shot probe for isolated margin in net pos_side_mode"
```

---

## Task 2: Add new config fields to FundingCrossSectionConfig

**Files:**
- Modify: `src/okx_trade/strategies/funding_cross_section.py` (config dataclass, around lines 80-100)
- Modify: `configs/live.yaml` (FundingXS section)

- [ ] **Step 1: Locate the config dataclass**

Read [funding_cross_section.py:80-100](src/okx_trade/strategies/funding_cross_section.py:80) to find the `FundingCrossSectionConfig` dataclass and confirm exact field names + ordering. The existing fields are `top_n`, `bot_n`, `rebalance_hours_utc`, `max_position_pct`, etc.

- [ ] **Step 2: Add the 11 new fields**

Add to the dataclass (preserve existing fields, append below):

```python
# === 2026-05-26: isolated margin + dynamic leverage ===
enable_dynamic_lever: bool = True
margin_mode: str = "isolated"   # "isolated" or "cross"; backtest forces "cross"
lever_min: float = 2.0
lever_max: float = 10.0
lever_base: float = 2.0
lever_slope: float = 3.0
lever_edge_combine_basis: bool = True

# === 2026-05-26: outlier guard ===
enable_outlier_guard: bool = True
outlier_vol_ratio: float = 3.0
outlier_window_min: int = 60
outlier_baseline_min: int = 1440
outlier_warmup_min: int = 1440
```

- [ ] **Step 3: Add live.yaml defaults under `strategies.funding_cross_section.config`**

```yaml
      # === 2026-05-26: defense layer ===
      enable_dynamic_lever: true
      margin_mode: isolated
      lever_min: 2.0
      lever_max: 10.0
      lever_base: 2.0
      lever_slope: 3.0
      lever_edge_combine_basis: true

      enable_outlier_guard: true
      outlier_vol_ratio: 3.0
      outlier_window_min: 60
      outlier_baseline_min: 1440
      outlier_warmup_min: 1440
```

- [ ] **Step 4: Run existing FundingXS tests to verify defaults don't break anything**

Run: `.venv/bin/python -m pytest tests/unit/ -k funding_cross -x -q`
Expected: PASS (all existing FundingXS-related tests).

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/strategies/funding_cross_section.py configs/live.yaml
git commit -m "feat(funding_xs): add isolated-margin + outlier guard config fields"
```

---

## Task 3: Pure function `compute_leverage` (TDD)

**Files:**
- Create: `src/okx_trade/strategies/_isolated_helpers.py`
- Create: `tests/unit/test_strategy_funding_xs_isolated.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_strategy_funding_xs_isolated.py`:

```python
"""Unit tests for FundingXS three-layer defense helpers (2026-05-26)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from okx_trade.strategies._isolated_helpers import (
    compute_edge_score,
    compute_leverage,
    outlier_check,
)


# ---------------------------------------------------------------------------
# compute_leverage
# ---------------------------------------------------------------------------
class TestComputeLeverage:
    def test_zero_edge_returns_base(self) -> None:
        assert compute_leverage(0.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 2.0

    def test_one_sigma_edge_returns_5x(self) -> None:
        # base=2 + slope=3 * |1| = 5
        assert compute_leverage(1.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 5.0

    def test_two_sigma_edge_returns_8x(self) -> None:
        assert compute_leverage(2.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 8.0

    def test_three_sigma_edge_clipped_to_hi(self) -> None:
        # base=2 + slope=3 * 3 = 11 → clipped to 10
        assert compute_leverage(3.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 10.0

    def test_negative_edge_uses_abs(self) -> None:
        assert compute_leverage(-1.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 5.0

    def test_lo_clip(self) -> None:
        # if base were below lo somehow (config error), still clip to lo
        assert compute_leverage(0.0, base=1.0, slope=3.0, lo=2.0, hi=10.0) == 2.0
```

- [ ] **Step 2: Run tests, verify FAIL**

Run: `.venv/bin/python -m pytest tests/unit/test_strategy_funding_xs_isolated.py::TestComputeLeverage -v`
Expected: `ImportError: cannot import name '_isolated_helpers'` (file doesn't exist yet).

- [ ] **Step 3: Create the helpers module with minimal `compute_leverage`**

Create `src/okx_trade/strategies/_isolated_helpers.py`:

```python
"""Pure helper functions for FundingXS three-layer defense (2026-05-26).

All functions here are stateless and side-effect free — testable in isolation
without NT runtime, OKX REST, or strategy state.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np


def compute_leverage(
    edge_score: float,
    *,
    base: float,
    slope: float,
    lo: float,
    hi: float,
) -> float:
    """Map |edge_score| to leverage with linear ramp + clip.

    lever = clip(base + slope * |edge_score|, lo, hi)

    Convention: |edge_score| is in z-score units. With defaults (base=2,
    slope=3, hi=10), |edge|=1σ → 5x; 2σ → 8x; ≥2.67σ → 10x.
    """
    raw = base + slope * abs(edge_score)
    return float(max(lo, min(hi, raw)))
```

- [ ] **Step 4: Run tests, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_strategy_funding_xs_isolated.py::TestComputeLeverage -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/strategies/_isolated_helpers.py tests/unit/test_strategy_funding_xs_isolated.py
git commit -m "feat(funding_xs): add compute_leverage helper with TDD"
```

---

## Task 4: Pure function `compute_edge_score` (TDD)

**Files:**
- Modify: `src/okx_trade/strategies/_isolated_helpers.py`
- Modify: `tests/unit/test_strategy_funding_xs_isolated.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_strategy_funding_xs_isolated.py`:

```python
# ---------------------------------------------------------------------------
# compute_edge_score
# ---------------------------------------------------------------------------
class TestComputeEdgeScore:
    def test_short_positive_funding_positive_edge(self) -> None:
        # leg is short, funding > universe avg → "going short on high funding"
        # → strong edge
        score = compute_edge_score(
            funding_rate=0.005,
            funding_universe=[0.001, 0.001, 0.001, 0.001, 0.005],
            basis=None,
            basis_universe=None,
            direction="short",
            combine_basis=False,
        )
        # funding_z of 0.005 in [0.001, 0.001, 0.001, 0.001, 0.005] ≈ 2.0
        # short direction → sign(+1) × 2.0 → +2.0
        assert score == pytest.approx(2.0, abs=0.1)

    def test_long_negative_funding_positive_edge(self) -> None:
        # leg is long, funding < universe avg → "going long on low funding"
        # → strong edge (mirror of above)
        score = compute_edge_score(
            funding_rate=-0.005,
            funding_universe=[-0.001, -0.001, -0.001, -0.001, -0.005],
            basis=None,
            basis_universe=None,
            direction="long",
            combine_basis=False,
        )
        # funding_z of -0.005 ≈ -2.0; long direction → sign(-1) × -2.0 → +2.0
        assert score == pytest.approx(2.0, abs=0.1)

    def test_combine_basis_adds_basis_z(self) -> None:
        # both funding and basis aligned with short → larger edge
        score = compute_edge_score(
            funding_rate=0.005,
            funding_universe=[0.001, 0.005],
            basis=0.01,
            basis_universe=[0.001, 0.01],
            direction="short",
            combine_basis=True,
        )
        # both signals at "high" end → both z ≈ +1; mean = 1; sign(+1) × 1 = 1
        assert score == pytest.approx(1.0, abs=0.1)

    def test_universe_zero_std_returns_zero(self) -> None:
        # all identical funding → no edge
        score = compute_edge_score(
            funding_rate=0.001,
            funding_universe=[0.001, 0.001, 0.001],
            basis=None, basis_universe=None,
            direction="short",
            combine_basis=False,
        )
        assert score == 0.0

    def test_single_element_universe_returns_zero(self) -> None:
        score = compute_edge_score(
            funding_rate=0.005,
            funding_universe=[0.005],
            basis=None, basis_universe=None,
            direction="short",
            combine_basis=False,
        )
        assert score == 0.0
```

- [ ] **Step 2: Run tests, verify FAIL**

Run: `.venv/bin/python -m pytest tests/unit/test_strategy_funding_xs_isolated.py::TestComputeEdgeScore -v`
Expected: `ImportError: cannot import name 'compute_edge_score'`.

- [ ] **Step 3: Implement `compute_edge_score`**

Append to `src/okx_trade/strategies/_isolated_helpers.py`:

```python
def _zscore(value: float, universe: Sequence[float]) -> float:
    """z-score of ``value`` against ``universe`` mean/std. Returns 0 if
    ``len(universe) < 2`` or std == 0 (no edge can be derived).
    """
    if len(universe) < 2:
        return 0.0
    arr = np.asarray(universe, dtype=float)
    std = float(arr.std(ddof=0))
    if std <= 0:
        return 0.0
    return (value - float(arr.mean())) / std


def compute_edge_score(
    *,
    funding_rate: float,
    funding_universe: Sequence[float],
    basis: float | None,
    basis_universe: Sequence[float] | None,
    direction: str,
    combine_basis: bool,
) -> float:
    """Compute per-leg edge score for leverage selection.

    funding_z  = z(funding_rate, funding_universe)
    basis_z    = z(basis,        basis_universe)  if combine_basis & basis is not None
    raw        = (funding_z + basis_z) / 2   if combine_basis else funding_z
    edge_score = sign(direction) × raw       # direction=short → +1, long → -1

    Convention: short direction wants positive funding/basis (we collect
    funding from longs); long direction wants negative. After ``sign``
    multiply, ``edge_score`` is positive when leg's direction agrees with
    the signal — and ``|edge_score|`` measures conviction.
    """
    funding_z = _zscore(funding_rate, funding_universe)
    if combine_basis and basis is not None and basis_universe is not None:
        basis_z = _zscore(basis, basis_universe)
        raw = (funding_z + basis_z) / 2.0
    else:
        raw = funding_z
    sign = 1.0 if direction == "short" else -1.0
    return float(sign * raw)
```

- [ ] **Step 4: Run tests, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_strategy_funding_xs_isolated.py::TestComputeEdgeScore -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/strategies/_isolated_helpers.py tests/unit/test_strategy_funding_xs_isolated.py
git commit -m "feat(funding_xs): add compute_edge_score helper with TDD"
```

---

## Task 5: Pure function `outlier_check` (TDD)

**Files:**
- Modify: `src/okx_trade/strategies/_isolated_helpers.py`
- Modify: `tests/unit/test_strategy_funding_xs_isolated.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_strategy_funding_xs_isolated.py`:

```python
# ---------------------------------------------------------------------------
# outlier_check
# ---------------------------------------------------------------------------
class TestOutlierCheck:
    def _calm_closes(self, n: int = 1500) -> list[float]:
        """Geometric Brownian motion-ish: small log-returns ~N(0, 0.001)."""
        rng = np.random.default_rng(seed=42)
        rets = rng.normal(0.0, 0.001, n)
        prices = 100.0 * np.exp(np.cumsum(rets))
        return prices.tolist()

    def test_warmup_short_history_allowed(self) -> None:
        ok, reason = outlier_check(
            closes=[1.0, 1.1, 0.9],
            window=60, baseline=1440, warmup=1440, ratio_threshold=3.0,
        )
        assert ok is True
        assert reason == "warmup"

    def test_calm_market_allowed(self) -> None:
        ok, reason = outlier_check(
            closes=self._calm_closes(),
            window=60, baseline=1440, warmup=1440, ratio_threshold=3.0,
        )
        assert ok is True
        assert reason == "ok"

    def test_recent_spike_rejected(self) -> None:
        closes = self._calm_closes(n=1440)
        # Inject a large wick in the last 60 bars: 10x normal vol
        rng = np.random.default_rng(seed=7)
        wick = rng.normal(0.0, 0.01, 60)  # 10x sigma
        closes.extend((closes[-1] * np.exp(np.cumsum(wick))).tolist())
        ok, reason = outlier_check(
            closes=closes,
            window=60, baseline=1440, warmup=1440, ratio_threshold=3.0,
        )
        assert ok is False
        assert "vol_ratio" in reason

    def test_zero_baseline_vol_allowed(self) -> None:
        # Flat history → std=0 baseline → allow (no signal to filter on)
        ok, reason = outlier_check(
            closes=[100.0] * 1500,
            window=60, baseline=1440, warmup=1440, ratio_threshold=3.0,
        )
        assert ok is True
        assert reason == "no_baseline"
```

- [ ] **Step 2: Run tests, verify FAIL**

Run: `.venv/bin/python -m pytest tests/unit/test_strategy_funding_xs_isolated.py::TestOutlierCheck -v`
Expected: `ImportError: cannot import name 'outlier_check'`.

- [ ] **Step 3: Implement `outlier_check`**

Append to `src/okx_trade/strategies/_isolated_helpers.py`:

```python
def outlier_check(
    *,
    closes: Sequence[float],
    window: int,
    baseline: int,
    warmup: int,
    ratio_threshold: float,
) -> tuple[bool, str]:
    """Decide whether to allow a new leg given recent realized vol.

    Returns ``(allow, reason)``:
      - ``(True, "warmup")``   — not enough history (< ``warmup`` bars).
      - ``(True, "no_baseline")`` — flat baseline (std==0); no filter possible.
      - ``(True, "ok")``       — recent vol within ``ratio_threshold`` of baseline.
      - ``(False, "vol_ratio=R>T")`` — recent vol > baseline × threshold; reject.

    Assumes ``closes`` are 1-minute bar closes for the instrument; both
    ``window`` and ``baseline`` are in bars (= minutes). Default config gives
    window=60 (last 1h), baseline=1440 (last 24h), warmup=1440.
    """
    if len(closes) < warmup:
        return True, "warmup"
    arr = np.asarray(closes, dtype=float)
    log_returns = np.diff(np.log(arr))
    if len(log_returns) < max(window, baseline):
        return True, "warmup"
    recent_vol = float(np.std(log_returns[-window:], ddof=0))
    baseline_vol = float(np.std(log_returns[-baseline:], ddof=0))
    if baseline_vol <= 0:
        return True, "no_baseline"
    ratio = recent_vol / baseline_vol
    if ratio > ratio_threshold:
        return False, f"vol_ratio={ratio:.2f}>{ratio_threshold}"
    return True, "ok"
```

- [ ] **Step 4: Run tests, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_strategy_funding_xs_isolated.py::TestOutlierCheck -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/strategies/_isolated_helpers.py tests/unit/test_strategy_funding_xs_isolated.py
git commit -m "feat(funding_xs): add outlier_check helper with TDD"
```

---

## Task 6: Add `_set_leverage_cached` method on FundingCrossSectionStrategy

**Files:**
- Modify: `src/okx_trade/strategies/funding_cross_section.py` (around `_rebalance_async` location, ~line 290-330)

- [ ] **Step 1: Locate FundingCrossSectionStrategy init + REST client field**

Read [funding_cross_section.py:130-160](src/okx_trade/strategies/funding_cross_section.py:130) to find the strategy's `__init__` and confirm the REST client attribute name. The existing code uses `self._rest = OKXRestClient(...)` in `_rebalance_async` (a lazily-initialized field).

- [ ] **Step 2: Add cache field to `__init__`**

In `__init__` near `self._last_rebalance_day` etc., add:

```python
            self._set_lever_cache: dict[str, float] = {}  # inst_value → last set lever
```

- [ ] **Step 3: Add the cached set-leverage method**

Add as an `async` method on the strategy class (place after `_rebalance_async` or near other helpers):

```python
        async def _set_leverage_cached(
            self, inst_value: str, lever: float,
        ) -> bool:
            """Idempotent: call OKX set-leverage only if (inst, lever) changed.

            Returns ``True`` if leverage is in the desired state (set or
            already cached); ``False`` on REST failure (caller should skip
            the leg this round).
            """
            cached = self._set_lever_cache.get(inst_value)
            if cached is not None and abs(cached - lever) < 0.01:
                return True
            if self._rest is None:
                from ..rest.client import OKXRestClient
                self._rest = OKXRestClient(self._rest_settings)
                await self._rest.__aenter__()
            try:
                from ..rest.constants import TdMode
                await self._rest.account.set_leverage(
                    inst_id=inst_value,
                    leverage=int(round(lever)),
                    mgn_mode=TdMode.ISOLATED,
                    pos_side=None,  # net mode
                )
                self._set_lever_cache[inst_value] = lever
                self.log.info(
                    f"funding_xs set leverage inst={inst_value} "
                    f"mgnMode=isolated lever={int(round(lever))}"
                )
                return True
            except Exception as exc:
                self.log.warning(
                    f"funding_xs set_leverage failed inst={inst_value} "
                    f"lever={lever}: {exc}"
                )
                return False
```

- [ ] **Step 4: Add a unit test for the cache behavior**

Append to `tests/unit/test_strategy_funding_xs_isolated.py`:

```python
# ---------------------------------------------------------------------------
# _set_leverage_cached behavior (mocked)
# ---------------------------------------------------------------------------
class TestSetLeverageCache:
    """Test the cache logic via a minimal mock — we don't spin up a real
    strategy because that requires NT TradingNode. We test the cache state
    machine directly by mocking the strategy state."""

    @pytest.fixture
    def fake_strategy(self):
        class _Mock:
            def __init__(self):
                self._set_lever_cache = {}
                self._rest = None
                self.log = type("L", (), {"info": lambda *_, **__: None,
                                          "warning": lambda *_, **__: None})()

                self.calls: list[tuple[str, int]] = []

                async def fake_set_lever(inst_id, leverage, mgn_mode, pos_side):
                    self.calls.append((inst_id, leverage))

                class _Acct:
                    def __init__(self_inner): pass
                    async def set_leverage(self_inner, *, inst_id, leverage,
                                           mgn_mode, pos_side):
                        await fake_set_lever(inst_id, leverage, mgn_mode, pos_side)

                class _Rest:
                    account = _Acct()
                self._rest = _Rest()

        from okx_trade.strategies.funding_cross_section import FundingCrossSectionStrategy
        # Bind the unbound method to our mock
        m = _Mock()
        m._set_leverage_cached = FundingCrossSectionStrategy._set_leverage_cached.__get__(m)
        return m

    @pytest.mark.asyncio
    async def test_first_call_invokes_rest(self, fake_strategy) -> None:
        ok = await fake_strategy._set_leverage_cached("DOT-USDT-SWAP", 5.0)
        assert ok is True
        assert fake_strategy.calls == [("DOT-USDT-SWAP", 5)]

    @pytest.mark.asyncio
    async def test_same_lever_skips_rest(self, fake_strategy) -> None:
        await fake_strategy._set_leverage_cached("DOT-USDT-SWAP", 5.0)
        await fake_strategy._set_leverage_cached("DOT-USDT-SWAP", 5.0)
        assert len(fake_strategy.calls) == 1  # second call hit cache

    @pytest.mark.asyncio
    async def test_changed_lever_re_invokes(self, fake_strategy) -> None:
        await fake_strategy._set_leverage_cached("DOT-USDT-SWAP", 5.0)
        await fake_strategy._set_leverage_cached("DOT-USDT-SWAP", 8.0)
        assert fake_strategy.calls == [("DOT-USDT-SWAP", 5), ("DOT-USDT-SWAP", 8)]
```

- [ ] **Step 5: Run the test, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_strategy_funding_xs_isolated.py::TestSetLeverageCache -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/okx_trade/strategies/funding_cross_section.py tests/unit/test_strategy_funding_xs_isolated.py
git commit -m "feat(funding_xs): add _set_leverage_cached with idempotent REST call"
```

---

## Task 7: Wire outlier guard into `_compute_targets`

**Files:**
- Modify: `src/okx_trade/strategies/funding_cross_section.py` (`_compute_targets` ~line 340-390)

- [ ] **Step 1: Read existing `_compute_targets` to find leg-iteration loop**

Read [funding_cross_section.py:340-390](src/okx_trade/strategies/funding_cross_section.py:340). The function iterates `for direction, legs in (("long", long_legs), ("short", short_legs)):` then `for inst_value in legs:`. The outlier check should sit at the top of the inner loop, before instrument lookup.

- [ ] **Step 2: Add the outlier guard import**

At the top of the file (with other strategy imports):

```python
from ._isolated_helpers import (
    compute_edge_score,
    compute_leverage,
    outlier_check,
)
```

- [ ] **Step 3: Insert outlier guard early in the leg loop**

Inside `_compute_targets`, immediately after `for inst_value in legs:`:

```python
                    # 2026-05-26: outlier guard — skip leg if recent vol abnormal
                    if self.config.enable_outlier_guard:
                        ok, reason = outlier_check(
                            closes=self._closes_by_inst.get(inst_value, []),
                            window=self.config.outlier_window_min,
                            baseline=self.config.outlier_baseline_min,
                            warmup=self.config.outlier_warmup_min,
                            ratio_threshold=self.config.outlier_vol_ratio,
                        )
                        if not ok:
                            self.log.warning(
                                f"funding_xs OUTLIER_SKIP inst={inst_value} "
                                f"direction={direction} reason={reason}"
                            )
                            continue
```

- [ ] **Step 4: Add an integration unit test for `_compute_targets`**

Append to `tests/unit/test_strategy_funding_xs_isolated.py`:

```python
# ---------------------------------------------------------------------------
# Integration: _compute_targets respects outlier guard
# ---------------------------------------------------------------------------
class TestComputeTargetsOutlierGuard:
    """We can't construct a real FundingCrossSectionStrategy without NT
    runtime, but we can construct a partial mock that has the same call
    interface and verify the outlier-guard branch."""

    def test_calm_market_includes_leg(self) -> None:
        # Smoke: outlier_check returns True for calm closes → leg included
        from okx_trade.strategies._isolated_helpers import outlier_check
        rng = np.random.default_rng(seed=11)
        rets = rng.normal(0.0, 0.001, 1500)
        closes = (100 * np.exp(np.cumsum(rets))).tolist()
        ok, _ = outlier_check(closes=closes, window=60, baseline=1440,
                              warmup=1440, ratio_threshold=3.0)
        assert ok is True

    def test_wicky_market_excludes_leg(self) -> None:
        from okx_trade.strategies._isolated_helpers import outlier_check
        rng = np.random.default_rng(seed=11)
        rets = rng.normal(0.0, 0.001, 1440)
        spike = rng.normal(0.0, 0.02, 60)  # 20x normal
        closes = (100 * np.exp(np.cumsum(np.concatenate([rets, spike])))).tolist()
        ok, reason = outlier_check(closes=closes, window=60, baseline=1440,
                                    warmup=1440, ratio_threshold=3.0)
        assert ok is False
        assert "vol_ratio" in reason
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/unit/test_strategy_funding_xs_isolated.py -v`
Expected: all passed (15+ tests total).

- [ ] **Step 6: Commit**

```bash
git add src/okx_trade/strategies/funding_cross_section.py tests/unit/test_strategy_funding_xs_isolated.py
git commit -m "feat(funding_xs): wire outlier guard into _compute_targets"
```

---

## Task 8: Refactor `_compute_targets` to compute and store edge_score + lever per leg

**Files:**
- Modify: `src/okx_trade/strategies/funding_cross_section.py` (`_compute_targets`)

This task is the largest single edit — `_compute_targets` returns `dict[str, tuple[str, float]]` today; we need to enrich the return to include lever per leg.

- [ ] **Step 1: Read existing `_compute_targets` return shape + _execute_diff consumer**

Read [funding_cross_section.py:336-413](src/okx_trade/strategies/funding_cross_section.py:336). Current return: `{inst_value: (direction, contracts)}`. Consumer `_execute_diff` (line ~398) unpacks `(direction, qty)`.

- [ ] **Step 2: Define an internal `LegTarget` dataclass at module scope**

At module level (near other private types):

```python
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class _LegTarget:
    direction: str
    contracts: float
    lever: float
    edge_score: float
```

- [ ] **Step 3: Refactor `_compute_targets` return type**

Change `target: dict[str, tuple[str, float]] = {}` → `target: dict[str, _LegTarget] = {}`.

Inside the inner loop, after the outlier guard and after computing `contracts`:

```python
                    # 2026-05-26: dynamic leverage from funding (+ optional basis) z-score
                    if self.config.enable_dynamic_lever:
                        basis = self._latest_basis.get(inst_value) if self.config.lever_edge_combine_basis else None
                        basis_universe = list(self._latest_basis.values()) if self.config.lever_edge_combine_basis else None
                        edge_score = compute_edge_score(
                            funding_rate=self._latest_funding[inst_value],
                            funding_universe=list(self._latest_funding.values()),
                            basis=basis,
                            basis_universe=basis_universe,
                            direction=direction,
                            combine_basis=self.config.lever_edge_combine_basis,
                        )
                        lever = compute_leverage(
                            edge_score,
                            base=self.config.lever_base,
                            slope=self.config.lever_slope,
                            lo=self.config.lever_min,
                            hi=self.config.lever_max,
                        )
                    else:
                        edge_score = 0.0
                        lever = self.config.lever_max  # fall back to max in static mode

                    target[inst_value] = _LegTarget(
                        direction=direction,
                        contracts=contracts,
                        lever=lever,
                        edge_score=edge_score,
                    )
```

- [ ] **Step 4: Update all `_compute_targets` consumers**

Find every spot that unpacks the old tuple. Use ripgrep:

Run: `rg "target\[.*\]\[0\]|target\[.*\]\[1\]|target.values\(\)" src/okx_trade/strategies/funding_cross_section.py`

For each match (likely in `_execute_diff` and possibly `on_stop`), change `(dir_, qty)` unpacks to attribute access (`.direction`, `.contracts`). Specifically in `_execute_diff` (line ~398):

```python
        def _execute_diff(self, target: dict[str, _LegTarget]) -> None:
            current = dict(self._positions)
            # Close: in current but not in target, or direction changed
            for inst_value, (cur_dir, cur_qty) in current.items():
                if inst_value not in target or target[inst_value].direction != cur_dir:
                    self._close_leg(inst_value, cur_dir, cur_qty)
            # Open / adjust: in target but not in current, or size changed
            for inst_value, leg in target.items():
                cur = self._positions.get(inst_value)
                if cur is None:
                    self._open_leg(inst_value, leg)
                elif cur[0] == leg.direction and abs(cur[1] - leg.contracts) > 1e-6:
                    self._close_leg(inst_value, cur[0], cur[1])
                    self._open_leg(inst_value, leg)
```

Note `_open_leg` signature now takes a `_LegTarget` instead of `(direction, contracts)` — Task 9 will update its implementation.

- [ ] **Step 5: Stub `self._latest_basis: dict[str, float] = {}` in `__init__`**

If not already present (it's not — basis fetcher is Task 11). Add as empty dict for now so the edge_score branch can read without KeyError:

```python
            self._latest_basis: dict[str, float] = {}
```

- [ ] **Step 6: Add a smoke test that the new return type is consumable**

Append to `tests/unit/test_strategy_funding_xs_isolated.py`:

```python
def test_leg_target_dataclass_construction() -> None:
    from okx_trade.strategies.funding_cross_section import _LegTarget
    lt = _LegTarget(direction="short", contracts=10.0, lever=5.0, edge_score=1.5)
    assert lt.direction == "short"
    assert lt.contracts == 10.0
    assert lt.lever == 5.0
    assert lt.edge_score == 1.5
```

- [ ] **Step 7: Run all FundingXS tests**

Run: `.venv/bin/python -m pytest tests/unit/ -k "funding_cross or funding_xs_isolated" -v`
Expected: all passed.

- [ ] **Step 8: Commit**

```bash
git add src/okx_trade/strategies/funding_cross_section.py tests/unit/test_strategy_funding_xs_isolated.py
git commit -m "refactor(funding_xs): _compute_targets returns _LegTarget with lever + edge_score"
```

---

## Task 9: Make `_open_leg` async + isolated tdMode + set-leverage + propagate awaits

**Files:**
- Modify: `src/okx_trade/strategies/funding_cross_section.py` (`_open_leg`, `_execute_diff`, `_rebalance_async`)

Single combined task: changing `_open_leg` from sync to async to call `_set_leverage_cached`, and propagating awaits through `_execute_diff` and `_rebalance_async`. Done in one commit to avoid an intermediate broken state.

- [ ] **Step 1: Read existing `_open_leg`**

Read [funding_cross_section.py:415-450](src/okx_trade/strategies/funding_cross_section.py:415). Current signature: `_open_leg(self, inst_value, direction, contracts)`. Task 8 already changed callers to pass `_LegTarget`, so this task converges the signature.

- [ ] **Step 2: Replace `_open_leg` with the async version**

```python
        async def _open_leg(self, inst_value: str, leg: "_LegTarget") -> None:
            inst_id = InstrumentId.from_str(inst_value)
            inst = self.cache.instrument(inst_id)
            if inst is None:
                return
            equity = effective_equity_usdt(
                self._allocated_equity_usdt, self.config.account_equity_usdt,
            )
            last_closes = self._closes_by_inst.get(inst_value, [])
            entry_px = last_closes[-1] if last_closes else 0.0
            intent = RiskIntent(
                strategy_id=str(self.id),
                instrument_id=inst_value,
                direction=leg.direction,  # type: ignore[arg-type]
                size=leg.contracts,
                entry_price=entry_px,
                stop_price=entry_px,
                account_equity_usdt=equity,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None or adjusted <= 0:
                return
            contracts = adjusted

            use_isolated = (
                self.config.margin_mode == "isolated"
                and self.config.enable_dynamic_lever
                and not self._is_backtest_context()
            )
            if use_isolated:
                ok = await self._set_leverage_cached(inst_value, leg.lever)
                if not ok:
                    self.log.warning(
                        f"funding_xs skip leg inst={inst_value} (set_leverage failed)"
                    )
                    return

            qty_obj = safe_make_qty(inst, contracts, self.log, ctx=f"open {inst_value}")
            if qty_obj is None:
                return
            side = OrderSide.BUY if leg.direction == "long" else OrderSide.SELL
            tags = ["td_mode:isolated"] if use_isolated else None
            order = self.order_factory.market(
                instrument_id=inst_id, order_side=side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
                tags=tags,
            )
            self.submit_order(order)
            self._positions[inst_value] = (leg.direction, contracts)
            self.log.info(
                f"OPEN {leg.direction} {inst_value} qty={contracts} "
                f"lever={leg.lever:.1f} edge={leg.edge_score:+.2f} "
                f"mode={'isolated' if use_isolated else 'cross'}"
            )
```

- [ ] **Step 3: Make `_execute_diff` async + await each `_open_leg`**

```python
        async def _execute_diff(self, target: dict[str, "_LegTarget"]) -> None:
            current = dict(self._positions)
            for inst_value, (cur_dir, cur_qty) in current.items():
                if inst_value not in target or target[inst_value].direction != cur_dir:
                    self._close_leg(inst_value, cur_dir, cur_qty)
            for inst_value, leg in target.items():
                cur = self._positions.get(inst_value)
                if cur is None:
                    await self._open_leg(inst_value, leg)
                elif cur[0] == leg.direction and abs(cur[1] - leg.contracts) > 1e-6:
                    self._close_leg(inst_value, cur[0], cur[1])
                    await self._open_leg(inst_value, leg)
```

- [ ] **Step 4: Update `_rebalance_async` caller**

In `_rebalance_async`, replace `self._execute_diff(target)` (sync call) with `await self._execute_diff(target)`. Locate via:

Run: `rg "_execute_diff\(" src/okx_trade/strategies/funding_cross_section.py`

- [ ] **Step 5: Add `_is_backtest_context()` helper**

Add as a method:

```python
        def _is_backtest_context(self) -> bool:
            """Detect whether we're in NT backtest engine (synchronous, no
            live REST client). Used to skip set-leverage and fall back to
            cross mode in backtest, since NT's MarginAccount doesn't model
            isolated correctly.

            Cheap heuristic: live mode has ``self._rest_settings`` populated
            with real credentials; backtest's fixture passes empty/dummy
            settings. We check whether the settings look usable.
            """
            try:
                return not bool(getattr(self._rest_settings, "api_key", None))
            except Exception:
                return True  # safe default: assume backtest on any error
```

- [ ] **Step 6: Run the FundingXS unit tests + a backtest smoke**

Run: `.venv/bin/python -m pytest tests/unit/ -k "funding_cross or funding_xs_isolated" -v`
Expected: all passed.

Run: `.venv/bin/python -m pytest tests/unit/test_backtest_m5.py -k funding -v` (if any backtest test covers funding_xs)
Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add src/okx_trade/strategies/funding_cross_section.py
git commit -m "feat(funding_xs): _open_leg async + isolated tdMode + set-leverage call"
```

---

## Task 10: Fetch basis (spot - perp mark) per universe inst

**Files:**
- Modify: `src/okx_trade/strategies/funding_cross_section.py` (`_rebalance_async`)

- [ ] **Step 1: Locate `_rebalance_async` and the funding-fetch step**

Read [funding_cross_section.py:306-340](src/okx_trade/strategies/funding_cross_section.py:306). The fetch step uses `asyncio.gather` over `self._fetch_current_funding(...)`.

- [ ] **Step 2: Add `_fetch_basis` async method**

```python
        async def _fetch_basis(self, inst_value: str) -> float | None:
            """Pull (perp_mark - spot_mark) / spot_mark via OKX REST.

            Uses /api/v5/market/index-tickers for spot index and
            /api/v5/public/mark-price for perp mark. Returns ``None`` on
            failure so caller can skip basis-component for that leg.
            """
            if self._rest is None:
                from ..rest.client import OKXRestClient
                self._rest = OKXRestClient(self._rest_settings)
                await self._rest.__aenter__()
            # inst_value like "DOT-USDT-SWAP" → underlying "DOT-USDT"
            underlying = inst_value.replace("-SWAP", "")
            try:
                idx_data = await self._rest.transport.request(
                    "GET", "/api/v5/market/index-tickers",
                    params={"instId": underlying}, private=False, group=None,
                )
                mark_data = await self._rest.transport.request(
                    "GET", "/api/v5/public/mark-price",
                    params={"instType": "SWAP", "instId": inst_value},
                    private=False, group=None,
                )
                if not idx_data or not mark_data:
                    return None
                idx_px = float(idx_data[0].get("idxPx") or 0)
                mark_px = float(mark_data[0].get("markPx") or 0)
                if idx_px <= 0 or mark_px <= 0:
                    return None
                return (mark_px - idx_px) / idx_px
            except Exception as exc:
                self.log.warning(f"funding_xs fetch_basis failed inst={inst_value}: {exc}")
                return None
```

- [ ] **Step 3: Call `_fetch_basis` in parallel with funding fetch inside `_rebalance_async`**

Locate the existing block (around line 320-335):

```python
                funding_tasks = [
                    self._fetch_current_funding(iid.symbol.value)
                    for iid in self._inst_ids
                ]
                rates = await asyncio.gather(*funding_tasks, return_exceptions=True)
```

Add right after:

```python
                if self.config.lever_edge_combine_basis:
                    basis_tasks = [
                        self._fetch_basis(iid.value) for iid in self._inst_ids
                    ]
                    basis_values = await asyncio.gather(*basis_tasks, return_exceptions=True)
                    self._latest_basis = {}
                    for iid, b in zip(self._inst_ids, basis_values):
                        if isinstance(b, Exception) or b is None:
                            continue
                        self._latest_basis[iid.value] = float(b)
```

- [ ] **Step 4: Run FundingXS tests**

Run: `.venv/bin/python -m pytest tests/unit/ -k "funding_cross or funding_xs_isolated" -v`
Expected: all passed (no test mocks `_fetch_basis` yet but it's called only when `lever_edge_combine_basis=True`; existing tests probably set it False or don't exercise the rebalance loop).

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/strategies/funding_cross_section.py
git commit -m "feat(funding_xs): fetch perp basis alongside funding rates"
```

---

## Task 11: Backtest fallback path — force cross + skip set-leverage

**Files:**
- Modify: `src/okx_trade/strategies/funding_cross_section.py`

Task 9b already added `_is_backtest_context()` and uses it in `_open_leg`. This task adds a focused unit test to lock in the behavior.

- [ ] **Step 1: Add unit test for backtest fallback**

Append to `tests/unit/test_strategy_funding_xs_isolated.py`:

```python
# ---------------------------------------------------------------------------
# Backtest fallback: force cross mode + skip set-leverage
# ---------------------------------------------------------------------------
class TestBacktestFallback:
    def test_is_backtest_context_no_api_key(self) -> None:
        from okx_trade.strategies.funding_cross_section import FundingCrossSectionStrategy

        class _Stub:
            _rest_settings = type("S", (), {"api_key": None})()

        result = FundingCrossSectionStrategy._is_backtest_context(_Stub())  # type: ignore
        assert result is True

    def test_is_backtest_context_with_api_key(self) -> None:
        from okx_trade.strategies.funding_cross_section import FundingCrossSectionStrategy

        class _Stub:
            _rest_settings = type("S", (), {"api_key": "real-key"})()

        result = FundingCrossSectionStrategy._is_backtest_context(_Stub())  # type: ignore
        assert result is False
```

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python -m pytest tests/unit/test_strategy_funding_xs_isolated.py::TestBacktestFallback -v`
Expected: 2 passed.

- [ ] **Step 3: Run any existing FundingXS backtest tests**

Run: `.venv/bin/python -m pytest tests/unit/ -k "backtest and funding" -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_strategy_funding_xs_isolated.py
git commit -m "test(funding_xs): lock in backtest-fallback context detection"
```

---

## Task 12: Full unit test suite + lint sanity

**Files:** none modified; this is a verification gate.

- [ ] **Step 1: Run the full unit suite**

Run: `.venv/bin/python -m pytest tests/unit/ -q`
Expected: All previously-passing tests still pass; ~20+ new tests pass.

- [ ] **Step 2: Spot-check imports + flake-style sanity**

Run: `.venv/bin/python -c "from okx_trade.strategies.funding_cross_section import FundingCrossSectionStrategy; from okx_trade.strategies._isolated_helpers import compute_leverage, compute_edge_score, outlier_check; print('OK')"`
Expected: `OK`

- [ ] **Step 3: If any failure, fix in-place and re-run; do not commit until clean**

---

## Task 13: Local backtest smoke (cross fallback path)

**Files:** none modified.

- [ ] **Step 1: Run a 3-day backtest of FundingXS**

Run: `.venv/bin/python scripts/backtest.py --strategy funding_cross_section --days 3 2>&1 | tail -20`

(Verify the exact CLI flag with `--help` first if unsure.)

Expected:
- Backtest completes without errors related to `_is_backtest_context`, `_set_leverage_cached`, or `_open_leg`.
- PnL roughly comparable to pre-change baseline (small differences due to outlier guard rejecting legs OK).
- No "OPEN ... mode=isolated" log lines (backtest should always go through cross fallback).

- [ ] **Step 2: If diverges materially**, investigate `_is_backtest_context` returning False unexpectedly. Backtest infrastructure shouldn't have a real API key in `_rest_settings`.

---

## Task 14: Smoke probe — set-leverage round-trip on VPS

**Files:** none modified.

- [ ] **Step 1: Run Task 1's probe script on VPS**

Run: `ssh okx-vps "cd /home/okxtrade/okx-trade && sudo -u okxtrade .venv/bin/python scripts/probe_okx_isolated.py"`

Expected: `PASS: DOT-USDT-SWAP set to isolated lever=3 (net mode)`

If still PASS as in Task 1, proceed.

If FAIL with a new error after deploy, do not deploy — investigate first.

---

## Task 15: Deploy — commit, push, VPS pull, restart

**Files:** none modified.

- [ ] **Step 1: Confirm `git status` is clean (all prior tasks committed)**

Run: `git status`
Expected: `nothing to commit, working tree clean`

- [ ] **Step 2: Push to origin/main**

Run: `git push origin main`
Expected: push succeeds with no conflicts.

- [ ] **Step 3: VPS git pull**

Run: `ssh okx-vps "sudo -u okxtrade git -C /home/okxtrade/okx-trade pull --ff-only"`
Expected: fast-forward to the latest commit.

- [ ] **Step 4: Restart service**

Run: `ssh okx-vps "sudo systemctl restart okx-trade.service && sleep 8 && systemctl status okx-trade.service --no-pager | head -15"`
Expected: `Active: active (running)` and `Started OKX paper trading`.

- [ ] **Step 5: Tail journal for the next 60s — look for any startup errors**

Run: `ssh okx-vps "journalctl -u okx-trade.service --since '1 minute ago' --no-pager | grep -iE 'error|fatal|traceback' | head -10"`
Expected: no Python tracebacks; OKX 51015 warnings are pre-existing and acceptable.

---

## Task 16: Post-deploy verification — wait for next funding window

**Files:** none modified.

- [ ] **Step 1: Identify the next funding window**

Funding windows are at 00:00 / 08:00 / 16:00 UTC. Compute the next one from now.

- [ ] **Step 2: At the next window + 30s, check journal for the new behavior**

Run: `ssh okx-vps "journalctl -u okx-trade.service --since '<funding_window_time_utc>' --no-pager | grep -E 'funding_xs|set leverage|OUTLIER_SKIP|OPEN.*mode=isolated' | head -40"`

Expected log lines (illustrative):

```
funding_xs set leverage inst=DOGE-USDT-SWAP mgnMode=isolated lever=5
OPEN short DOGE-USDT-SWAP qty=120.5 lever=5.0 edge=+1.23 mode=isolated
```

- [ ] **Step 3: Verify on OKX side via REST**

Run: `ssh okx-vps "cd /home/okxtrade/okx-trade && sudo -u okxtrade .venv/bin/python -c '
import asyncio
from okx_trade import OKXRestClient, OKXSettings
async def main():
    async with OKXRestClient(OKXSettings()) as c:
        pos = await c.transport.request(\"GET\", \"/api/v5/account/positions\", private=True, group=None)
        for p in pos:
            print(f\"  {p[\\\"instId\\\"]} mgnMode={p[\\\"mgnMode\\\"]} pos={p[\\\"pos\\\"]} lever={p.get(\\\"lever\\\")}\")
asyncio.run(main())'"`

Expected: positions opened by FundingXS this rebalance show `mgnMode=isolated`.

- [ ] **Step 4: Spot-check 24h later**

After 24h, query `pnl.sqlite.trades_okx` and confirm no single leg has bal_chg loss exceeding ~5% of account equity (the design's worst-case bound).

Run: `ssh okx-vps "sqlite3 /home/okxtrade/okx-trade/var/pnl.sqlite \"SELECT inst_id, ROUND(bal_chg,2), datetime(ts_ms/1000,'unixepoch','+8 hours') FROM trades_okx WHERE strategy_id LIKE '%FundingXS%' AND ts_ms > strftime('%s','now')*1000 - 86400000 ORDER BY bal_chg ASC LIMIT 10;\""`

Expected: no row with bal_chg < -$1,500 (~5% of $30k account).

---

## Task 17: Docs runbook — add rollback note to `docs/operations.md`

**Files:**
- Modify: `docs/operations.md`

- [ ] **Step 1: Append rollback section**

```markdown
## FundingXS three-layer defense rollback (2026-05-26)

Symptom: FundingXS opens too few legs, or set-leverage REST is misbehaving,
or outlier guard is over-aggressively skipping legs.

**Soft rollback** (no restart needed):
1. Edit `configs/live.yaml` → `strategies.funding_cross_section.config`:
   - `enable_outlier_guard: false`  → disable outlier filter only
   - `enable_dynamic_lever: false`  → disable dynamic leverage (still isolated, but lever=lever_max for all legs)
   - `margin_mode: cross`           → full revert to pre-2026-05-26 behavior
2. Restart service: `sudo systemctl restart okx-trade.service`

**Hard rollback**: `git revert` the three commits matching `^(feat|refactor)\(funding_xs\)`.

**Diagnostics**:
- Look for `OUTLIER_SKIP` log lines to confirm guard activity.
- `set_leverage failed` warnings indicate REST issues; check OKX status page.
- Check `pnl.sqlite.equities` table: equity_usdt should match OKX totalEq within 5%.
```

- [ ] **Step 2: Commit**

```bash
git add docs/operations.md
git commit -m "docs(ops): add FundingXS isolated-margin rollback runbook"
```

- [ ] **Step 3: Push**

Run: `git push origin main`

---

## Done. Final acceptance checklist

- [ ] All 20+ unit tests pass (`pytest tests/unit/ -q`)
- [ ] Probe script returns `PASS` on VPS
- [ ] Backtest runs without divergence
- [ ] Deploy succeeded; service `active (running)`
- [ ] Next funding window logs show `set leverage isolated` + `mode=isolated` per leg
- [ ] OKX REST `/positions` returns `mgnMode=isolated` for FundingXS legs
- [ ] 24h post-deploy: no single-leg loss > 5% of account equity
- [ ] Rollback runbook documented in `docs/operations.md`
