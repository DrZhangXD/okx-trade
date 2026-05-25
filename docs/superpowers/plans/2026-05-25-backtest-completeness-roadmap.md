# Backtest Completeness Roadmap — 8 Partial Strategies → Fully Backtestable

> **Master roadmap for 6 sub-plans.** Each sub-plan is its own document under `docs/superpowers/plans/` and is independently executable. Use this file to track sequencing, dependencies, and "definition of done".

**Goal:** Bring all 8 currently-partial strategies (`funding_carry`, `funding_cross_section`, `funding_skew_momentum`, `basis_arb`, `ob_imbalance`, `option_vol_selling`, `ml_fusion`, `factor_portfolio`) to **end-to-end backtestable** state, with equity curves comparable to the 4 already-working strategies (`xs_momentum` / `liq_reversal` / `stat_arb_pairs` / `range_breakout`).

**Architecture principle:** Extend (not replace) the existing `BacktestRunner` (Nautilus Trader `BacktestNode` wrapper) and `data_loader` (parquet catalog). All new data types (funding, orderbook, option summary) follow the established pattern: REST/WS downloader → parquet on disk → loaded into NT's `BacktestDataConfig` or injected via strategy hooks. Custom simulators (basis_arb margin isolation) live alongside the runner, not inside NT.

**Tech stack:** Python 3.12+, asyncio, NautilusTrader ≥1.225, pyarrow/parquet, numpy/pandas/scipy, xgboost (ml_fusion), statsmodels (stat_arb, already wired).

---

## Sub-plan inventory

| # | Plan file | Unlocks | Effort | Depends on |
|---|---|---|---|---|
| 1 | [2026-05-25-funding-rate-backtest-data.md](2026-05-25-funding-rate-backtest-data.md) | `funding_carry`, `funding_cross_section`, `funding_skew_momentum`, `basis_arb` (funding leg) | ~1 day | — (foundational) |
| 2 | [2026-05-25-orderbook-snapshot-replay.md](2026-05-25-orderbook-snapshot-replay.md) | `ob_imbalance` | ~2 days | — (independent) |
| 3 | [2026-05-25-option-summary-backtest.md](2026-05-25-option-summary-backtest.md) | `option_vol_selling` | ~1.5 days | — (independent) |
| 4 | [2026-05-25-ml-fusion-walkforward.md](2026-05-25-ml-fusion-walkforward.md) | `ml_fusion` | ~1.5 days | Plan 1 (uses funding feature) |
| 5 | [2026-05-25-factor-portfolio-backtest.md](2026-05-25-factor-portfolio-backtest.md) | `factor_portfolio` | ~1 day | Plan 1 (uses funding panel) |
| 6 | [2026-05-25-basis-arb-margin-simulator.md](2026-05-25-basis-arb-margin-simulator.md) | `basis_arb` (margin leg) | ~1.5 days | Plan 1 |

**Total:** ~8.5 engineering days. Critical-path = Plan 1 (4–6 then run in parallel).

---

## Dependency order

```
Plan 1 (funding data) ─┬─→ Plan 4 (ml_fusion)
                       ├─→ Plan 5 (factor_portfolio)
                       └─→ Plan 6 (basis_arb margin)

Plan 2 (orderbook replay) ──── independent
Plan 3 (option capture)   ──── independent
```

**Recommended sequencing:**
1. Plan 1 first (unblocks 4 strategies on its own + enables 3 downstream plans).
2. Plans 2 & 3 in parallel with Plan 1 (no overlap).
3. Plans 4, 5, 6 in parallel after Plan 1 lands.

---

## Definition of Done (whole roadmap)

Every one of the 8 strategies must satisfy:

1. **Backtest runs end-to-end** via `python scripts/backtest.py --strategy <name> ...` with deterministic output.
2. **Equity curve exported** (CSV + Plotly HTML) by reusing the existing `--plot` / `--equity-csv` flags.
3. **`SUPPORTED_STRATEGIES` dict in `scripts/backtest.py` includes the strategy** with a documented CLI flag set.
4. **At least one integration test** in `tests/integration/` (or `tests/unit/backtest/`) that runs the backtest on a tiny fixture and asserts non-zero trades + finite Sharpe.
5. **`docs/strategy_roadmap.md` row updated** from "paper validation" / "partial" → "backtestable".
6. **No regression** in existing `pytest tests/unit -v` (currently ~570 tests; target ≥ +60 after this roadmap).

---

## Conventions (apply to all 6 sub-plans)

- **Tests live at** `tests/unit/<mirror-of-src-path>` for unit tests, `tests/integration/` for end-to-end.
- **Commit message style**: `<type>(<scope>): <subject>` where type ∈ {`add`, `fix`, `enable`, `refactor`, `docs`, `feat`}. One commit per task.
- **Run all tests** with `pytest tests/unit -v` before each commit. Integration tests opt-in via `pytest tests/integration -v -m integration` (requires network/keys).
- **Imports**: `from __future__ import annotations` at top of every new file.
- **Type hints**: PEP 604 (`list[X]`, `X | None`).
- **Dataclasses**: `@dataclass(frozen=True, slots=True)` for pure data.
- **No emojis** in code/docs/commits.
- **Parquet schema**: every new dataset gets a documented schema in a docstring at the writer function. Each row carries `inst_id: str` + `ts_ms: int` (UTC ms epoch) as the primary key.
- **Cache layout**: `${catalog_path}/<dataset>/<inst_id>/<YYYYMM>.parquet` (e.g., `./data/funding/BTC-USDT-SWAP/202604.parquet`). Allows surgical refresh per month.
- **Optional-dep gating**: anything that requires `xgboost`, `statsmodels`, etc., must `raise ImportError` with a clear "install with `pip install -e .[ml-fusion]`" message at strategy `__init__`.

---

## Cross-cutting risks

| Risk | Mitigation |
|---|---|
| OKX REST rate limits (especially funding history × 30+ instruments) | All downloaders use existing `OKXRestClient`'s rate-limiter; `_extended` wrappers already have inter-call sleeps. Test fixtures use respx mocks. |
| `BacktestDataConfig` only supports NT's known data types (Bar/Trade/Quote/OrderBook) | Funding/OI/option-summary cannot be NT data types. They're injected via strategy hooks (`feed_funding_panel()`, `feed_option_snapshots()`) read before `on_start`. |
| Walk-forward training (ml_fusion) is non-deterministic across xgboost versions | Pin `xgboost==2.0.*` minor in pyproject; set seed via `XGBClassifier(random_state=42)`. |
| Orderbook replay file size — 1 month × 1 inst × books5 @ 100ms ≈ 5 GB | Plan 2 includes downsampling (default 1s) + lz4 parquet compression; use 7-day windows by default. |
| basis_arb margin simulator drift vs real OKX behavior | Tag simulator as "approximate, ±5% margin call timing". Document delta vs production. |

---

## Execution mode (per sub-plan)

Each sub-plan ends with the standard writing-plans handoff: **Subagent-Driven** (recommended) or **Inline Execution**. Plans 1–3 are recommended for subagent-driven; plans 4–6 are smaller and fine inline.

---

## Status tracker

| Sub-plan | Status |
|---|---|
| 1. funding rate data | drafted |
| 2. orderbook replay | drafted |
| 3. option summary | drafted |
| 4. ml_fusion walk-forward | drafted |
| 5. factor_portfolio backtest | drafted |
| 6. basis_arb margin sim | drafted |

Update this table as plans are executed.
