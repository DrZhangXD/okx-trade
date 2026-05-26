# okx-trade

**English** · [简体中文](README.zh.md)

Full-stack OKX quant trading: async SDK (REST + WebSocket) + NautilusTrader adapter + **10 strategies** + factor research lab + layered risk pipeline + PnL tracker + monitoring + backtest + VPS deploy scripts.

**Status**: paper trading running 24/7 on Aliyun VPS (**9 strategies enabled in parallel**) — observation window extended past the initial **2026-05-22** target while shoring up defensive layers after the 2026-05-25 DOT wick incident. **949/949 unit tests green**. M7 (real-money + Telegram alerts) still gated on the Phase 1 hardening below.

---

## Quick Start

```bash
# 1) Install (dev mode with NT + numpy strategy layer)
pip install -e ".[strategy,dev]"

# 2) Configure OKX credentials
cp .env.example .env
# Edit .env: OKX_API_KEY / SECRET / PASSPHRASE / IS_DEMO=true

# 3) Run unit tests (no network)
pytest tests/unit -v             # 949 tests

# 4) Integration tests (requires demo creds + proxy if in mainland China)
pytest tests/integration -v -m integration
```

## Four-layer architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Research Layer (P1, 2026-05-19)                                    │
│    FactorRegistry / FactorPanel / grade_factor / walk_forward       │
│    CLI: list / fetch / eval / grade-all / approve / backtest-...    │
├─────────────────────────────────────────────────────────────────────┤
│  Strategy Layer  (10 NautilusTrader Strategy subclasses)            │
│    funding_carry / xs_momentum / liq_reversal / basis_arb /         │
│    ob_imbalance / funding_cross_section / funding_skew_momentum /   │
│    stat_arb_pairs / option_vol_selling / ml_fusion /                │
│    factor_portfolio (generic factor synthesizer)                    │
├─────────────────────────────────────────────────────────────────────┤
│  Risk + PnL + Portfolio + Monitor                                   │
│    KellyCheck / VolTargetCheck / DrawdownCheck /                    │
│    AccountDrawdownCheck (Phase 0 single-source kill-switch) /       │
│    CorrelationCheck / RegimeGateCheck /                             │
│    PnLTracker / Allocator / LiveMonitor                             │
├─────────────────────────────────────────────────────────────────────┤
│  NT Adapter   (LiveDataClient + LiveExecutionClient)                │
├─────────────────────────────────────────────────────────────────────┤
│  okx_trade SDK   (REST + WS, async, no pandas)                      │
└─────────────────────────────────────────────────────────────────────┘
```

Detailed data flow: see [ARCHITECTURE.md](ARCHITECTURE.md).

## SDK usage (lowest layer)

### REST

```python
import asyncio
from okx_trade import OKXRestClient, OKXSettings

async def main():
    async with OKXRestClient(OKXSettings()) as client:
        ticker = await client.market.get_ticker("BTC-USDT-SWAP")
        print(ticker.last)

asyncio.run(main())
```

### WebSocket subscription

```python
import asyncio
from okx_trade import OKXSettings, OKXWSClient

async def main():
    async with OKXWSClient(OKXSettings()) as ws:
        async with ws.subscribe("books5", instId="BTC-USDT-SWAP") as stream:
            async for event in stream:
                print(event["data"][0])
                break

asyncio.run(main())
```

More SDK examples in [`examples/`](examples/).

## Strategy layer

`src/okx_trade/strategies/` ships strategies as subclasses of `nautilus_trader.trading.strategy.Strategy`. Backtest and live share the same code:

| Strategy | Idea | Cadence | Status |
|---|---|---|---|
| `FundingCarryStrategy` | Spot+perp delta-neutral funding rate carry | 8h funding cycle | ✅ enabled |
| `XSMomentumStrategy` | Cross-sectional momentum, vol-managed, 5 long + 5 short legs | Daily UTC 00:00 rebalance | ✅ enabled |
| `LiqReversalStrategy` | Liquidation cascade reversal (z-score on `liquidation-orders`) | Event-driven | ✅ enabled |
| `BasisArbStrategy` (M5) | Delivery futures vs spot basis arbitrage | Hourly basis check | ✅ enabled |
| `OBImbalanceStrategy` (M5) | Order-flow microprice + book imbalance microstructure reversion | Sub-second aggregation | ✅ enabled |
| `FundingXSStrategy` (M6+) | Funding cross-section long/short with β-hedge | 8h funding cycle | ✅ enabled (5/19) |
| `FundingSkewStrategy` (M6+) | Funding rate ±2σ reversal | ~30min poll | ✅ enabled (5/19) |
| `StatArbStrategy` (M6+) | Cointegration pair (BTC-ETH default) spread reversal | Per 1H bar | ✅ enabled (5/19) |
| `FactorPortfolioStrategy` (P1) | Generic factor synthesizer (z-score + top-K, factor list from yaml) | 4h rebalance, bar-driven | ✅ enabled (5/19) |
| `OptionVolStrategy` (M6+) | BTC short straddle + perp delta-hedge | Hourly check | ❌ disabled |
| `MLFusionStrategy` (M6+) | XGBoost meta-model fusing multiple alphas | Every 4h | ❌ disabled |

Two remain disabled: `option_vol_selling` needs `live_node` to dynamically inject `option_ulys` instrument-provider filter; `ml_fusion` needs `pip install xgboost` + a retrain script. See [docs/strategy_roadmap.md](docs/strategy_roadmap.md).

Retired: `RangeBreakoutStrategy` (M5.X, weak alpha + unstable implementation).

### Cold-start elimination: REST warmup

`funding_skew_momentum` / `stat_arb_pairs` / `funding_cross_section` / `factor_portfolio` all spawn an async OKX REST fetch in `on_start` to pre-fill their history buffers, so on VPS restart they're immediately full-functional instead of waiting days-to-weeks for live data to accumulate. Config knobs: `warmup_via_rest: bool` or `warmup_via_rest_days: int`.

### Factor research lab (P1, 2026-05-19)

`okx_trade.research` module: a CLI to evaluate arbitrary candidate factors on IC / IR / decay / turnover / net-PnL, and pipe approved factors directly into `FactorPortfolioStrategy` via yaml:

```bash
# Fetch data + grade 15 v1 factors
python -m okx_trade.research fetch --start 2025-11-01 --end 2026-05-15 --universe top30
python -m okx_trade.research grade-all --start 2025-11-01 --end 2026-05-15 --horizon 1d

# Approve a factor that passes the gate (writes configs/factor_portfolio.yaml)
python -m okx_trade.research approve --factor basis_z_30d --weight 0.40

# End-to-end portfolio backtest
python -m okx_trade.research backtest-portfolio --total-bars 2000
```

Details in [strategy_roadmap.md](docs/strategy_roadmap.md#factor-research-lab-p1-2026-05-19).

## Risk pipeline

`src/okx_trade/risk/` provides independent checks chained by `RiskManager`, invoked once before every order:

| Check | Behavior | Tier |
|---|---|---|
| `AccountDrawdownCheck` | OKX `totalEq` -3%/day or -8%/week → halts ALL strategies (kill switch) | **Account (single instance)** |
| `VolTargetCheck` | Size from N-day realized vol → annualized vol target | Per-strategy |
| `KellyCheck` | f\* = (p×R - q)/R, × 0.25 fractional Kelly (PnL tracker takes over after first 20 trades) | Per-strategy |
| `DrawdownCheck` | -3% daily / -8% weekly equity drawdown (per-strategy; Phase 0 not fed, Phase 1 will wire to PnLTracker) | Per-strategy |
| `CorrelationCheck` | Rolling 30-day strategy PnL correlation > 0.7 → down-weight | Per-strategy |
| `RegimeGateCheck` | BTC trending / mean_reverting / neutral → scale by `strategy_kind` map | Shared detector |

Each check accepts `RiskIntent(size)` → returns `APPROVE / SCALE / REJECT`. All checks are pure functions / pure state machines, decoupled from NT runtime.

### DD architecture split (Phase 0, 2026-05-20)

- **Account-level `AccountDrawdownTracker`**: a singleton owned by `LiveMonitor`; each alloc_refresh pushes OKX `totalEq` into it
- **Per-strategy `DrawdownTracker`**: one per strategy, **currently unfed** (Phase 1 will wire it to per-strategy realized PnL from `PnLTracker` for true single-strategy isolation)
- Any `AccountDrawdownCheck` breach kill-switches every strategy via a single tracker + single alert, eliminating the 27% phantom-breach cascade caused by the prior multi-source feed.

### FundingXS three-layer defense (2026-05-26)

Added after a DOT-USDT-SWAP wick on the OKX demo exchange ($1.45 → $121.985 in one 1-minute bar) force-liquidated a `FundingXSStrategy` short and burned -$51,128 of paper equity in a single event. Three independent layers, each with an `enable_*` kill switch in `configs/strategies/funding_cross_section.yaml`:

1. **Isolated margin per leg** — submitted with `tags=["td_mode:isolated"]`; OKX caps each leg's loss to its allocated margin (~0.5–5% of account), making cross-account cascade liquidation impossible.
2. **Dynamic leverage** — `clip(2 + 3 × |funding_z + basis_z|/2, 2, 10)`. Higher conviction → higher leverage → smaller isolated margin → lower loss ceiling per leg. Spec recasts leverage as an inverted conviction signal.
3. **Outlier guard** — pre-entry filter: skip a leg if its last-1h realized vol > 3 × the last-24h baseline. 1-minute bar feed is subscribed independently of the strategy's β-hedge 1D feed.

`_execute_diff` does a **two-phase commit**: pre-validate `set_leverage` for every leg in the rebalance; if any fails, abort the entire open-phase to prevent directional residual. Spec + plan + runbook are at:

- [docs/superpowers/specs/2026-05-26-funding-xs-isolated-margin-design.md](docs/superpowers/specs/2026-05-26-funding-xs-isolated-margin-design.md)
- [docs/superpowers/plans/2026-05-26-funding-xs-isolated-margin.md](docs/superpowers/plans/2026-05-26-funding-xs-isolated-margin.md)
- Rollback runbook in [`docs/operations.md` §五·B](docs/operations.md)

Phase-1 follow-up: extract `IsolatedMarginPolicy` + `OutlierGuard` as shared risk handles so `FundingCarry` / `XSMomentum` / `FundingSkew` can opt in.

## PnL tracking + portfolio optimization

| Module | Purpose |
|---|---|
| `pnl/tracker.py` | Persists trades + daily equity to SQLite (`var/pnl.sqlite`) |
| `pnl/stats.py` | Computes `win_rate` / `avg_R` / Sharpe / Sortino |
| `pnl/feed.py` | Pipes stats into `KellyCheck.set_stats()` / `CorrelationCheck.update_strategy_pnl()` |
| `portfolio/equal_weight.py` | Cold-start: even split across 4 strategies |
| `portfolio/risk_budget.py` | Kicks in after 30 days of data: inverse-vol + correlation penalty |

## Monitoring + alerts + daily report

`src/okx_trade/monitor/`:

- **`LiveMonitor`** polls risk state every 60s, emits `Alert` on threshold breach
- **Sinks**: `LogSink`, `JsonlSink` (`var/alerts.jsonl`), `TelegramSink` (M6 wiring)
- **`DailyReporter`** writes JSON daily reports to `var/daily_reports/`

## Backtest

```bash
# xs_momentum 30-day backtest
.venv/bin/python scripts/backtest.py \
  --strategy xs_momentum \
  --instrument-ids "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP" \
  --signal-bar 1H --total-bars 720 --equity 10000

# funding_carry 1-year cashflow estimator
.venv/bin/python scripts/backtest_funding_carry.py \
  --perp-symbol BTC-USDT-SWAP --total 1095 --equity 10000
```

Backtest engine is NautilusTrader's `BacktestNode`, data via `ParquetDataCatalog`. Use `--reuse-data` to skip download on subsequent runs.

## Live paper trading

```bash
# Validate config + universe resolution (no NT engine)
.venv/bin/python scripts/live.py --check

# Start 9 strategies in parallel (NT TradingNode + monitor)
.venv/bin/python scripts/live.py --run

# Generate today's daily report and exit
.venv/bin/python scripts/live.py --report-only
```

VPS deployment: see [deploy/README.md](deploy/README.md), includes systemd unit + healthcheck timer + bootstrap script.

Operations playbook (incident response, diagnostic scripts, cron config): see [docs/operations.md](docs/operations.md).

Pull observation report any time:

```bash
ssh okx-vps '/home/okxtrade/okx-trade/scripts/observation_report.sh adhoc | xargs cat'
```

## Project layout

```
src/okx_trade/
├── auth.py                  # HMAC-SHA256 signing (pure functions)
├── config.py                # OKXSettings (pydantic-settings)
├── exceptions.py            # OKXAPIError hierarchy (sCode/sMsg surfacing)
├── enums.py                 # InstType / TdMode / Side / OrdType / BarSize ...
├── models/                  # pydantic v2 data models
├── rest/                    # REST client: account / market / public / trade / transport
├── ws/                      # WS client: public / private / business (3 connections)
├── adapter/                 # NT adapter: data / execution / parsing / instrument_provider / factories
├── strategies/              # 10 strategies + base + confirmation/OFI + pnl_hook + _features
├── research/                # P1 factor research lab: panel/registry/compute/data/grade/store/report/cli + factors/
├── pricing/                 # Black-Scholes pricer + Greeks (for option_vol_selling)
├── risk/                    # vol_target / kelly / drawdown (+ Account-level) / correlation / regime / stats / integration
├── pnl/                     # tracker / stats / feed
├── portfolio/               # equal_weight / risk_budget
├── monitor/                 # alerts / live / daily_report
├── runtime/                 # live_node: yaml + tracker + allocator + account-DD-tracker → NT TradingNode
└── backtest/                # data_loader / runner / plotting / walk_forward

scripts/
├── live.py                  # Paper trading entrypoint (--check / --run / --report-only)
├── backtest.py              # Multi-strategy backtest (--strategy ...)
├── backtest_funding_carry.py
├── backtest_oneyear.py
├── backtest_m4_smoke.py
├── healthcheck.py           # Invoked by systemd timer
├── observation_report.sh    # day_7 / day_14 evaluation reports
├── stat_arb_observe.sh      # stat_arb 24h / lunch snapshot
├── reconcile_okx_positions.py  # ExecStartPre: reconcile before start
├── factor_research_smoke.sh # Factor lab end-to-end smoke
├── diag_account_bills.py    # OKX bills diagnostic (grouped by type/subType)
└── diag_mtm_swing.py        # Current positions + 1H candles MTM correlation

configs/
├── live.yaml                # 9 enabled strategies + risk_defaults + portfolio + monitor + alerts
├── risk.yaml                # Risk parameter reference (live.yaml inlines actual values)
├── factor_portfolio.yaml    # FactorPortfolioStrategy config (5 approved factors)
└── strategies/*.yaml        # Per-strategy parameters

deploy/                      # VPS systemd deployment (see deploy/README.md)
docs/
├── strategy_roadmap.md      # Strategy status + engineering backlog
├── operations.md            # Operations playbook (incident response, cron, diag scripts)
└── superpowers/             # Spec / plan docs (P1 factor lab etc.)

tests/unit/                  # 803 unit tests
tests/integration/           # Skipped by default; run manually with credentials
```

## Design principles

- Fully async (httpx + websockets)
- `Decimal` for prices and quantities (no float precision errors)
- SDK layer has zero pandas / numpy dependency; strategy layer pulls them in via `[strategy]` extras
- Risk / PnL / Portfolio modules are pure functions / pure state machines for easy unit testing
- Strategy code is shared between backtest and live (NT abstraction guarantees this)
- Mainland China proxy via `.env`, shared between REST and WS

## Development

```bash
# Run all unit tests
pytest tests/unit -v

# Run a specific module
pytest tests/unit/test_exceptions.py -v
pytest tests/unit/ -k "kelly or risk" -v

# Type checking (in dev extras)
mypy src/

# Lint
ruff check src/ tests/
```

Commit conventions: [Conventional Commits](https://www.conventionalcommits.org/) (`feat:` / `fix:` / `refactor:` / `docs:` / `test:`).

## Roadmap

- ✅ **M1**: REST + WS SDK
- ✅ **M2**: range_breakout (retired) + funding_carry strategies + risk pipeline scaffolding
- ✅ **M3**: full risk pipeline (vol_target / kelly / drawdown / correlation)
- ✅ **M3.6**: NT integration (`RiskAwareStrategy` injects check before `submit_order`)
- ✅ **M4**: xs_momentum + liq_reversal + OFI confirmation + backtest engine
- ✅ **M5**: PnL tracker + portfolio optimizer + monitor + alerts + daily report + paper trading runtime
- ✅ **M6+**: 5 mid-/long-term strategies (funding_xs / funding_skew / stat_arb / option_vol / ml_fusion) + regime gate + walk-forward + OPTION adapter
- ✅ **P1 (2026-05-19)**: factor research lab (15 factors + grade pipeline + `FactorPortfolioStrategy` generic synthesizer)
- ✅ **Phase 0 DD (2026-05-20)**: `AccountDrawdownTracker` single-source kill-switch + REST warmup eliminating cold-start
- 🟡 **Observation window**: 2026-05-08 → **2026-05-22 (day_14)** paper trading
- 🔲 **Phase 1 DD**: per-strategy `DrawdownTracker` wired to PnLTracker realized PnL (true isolation)
- 🔲 **M7**: real-money switch + Telegram alerts + portfolio rebalance scheduler
- 🔲 **M8**: multi-account / multi-venue routing / option strategies on / ml_fusion on

Per-milestone detail: see [CHANGELOG.md](CHANGELOG.md).

## License

Source visible but **not licensed for use** — © 2026 DrZhangXD, All Rights Reserved. Contact the author in writing before reuse or derivative work.
