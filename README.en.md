# okx-trade

**English** · [简体中文](README.md)

Full-stack OKX quant trading: async SDK (REST + WebSocket) + NautilusTrader adapter + 4 strategies + risk pipeline + PnL tracker + monitoring + backtest + VPS deploy scripts.

**Status**: paper trading running 24/7 on Aliyun VPS, observation window through **2026-05-22** → evaluate progression to M6 (real-money switch + Telegram alerts). 449/449 unit tests green.

---

## Quick Start

```bash
# 1) Install (dev mode with NT + numpy strategy layer)
pip install -e ".[strategy,dev]"

# 2) Configure OKX credentials
cp .env.example .env
# Edit .env: OKX_API_KEY / SECRET / PASSPHRASE / IS_DEMO=true

# 3) Run unit tests (no network)
pytest tests/unit -v             # 449 tests

# 4) Integration tests (requires demo creds + proxy if in mainland China)
pytest tests/integration -v -m integration
```

## Three-layer architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Strategy Layer  (4 strategies, NautilusTrader subclasses)   │
│    range_breakout / xs_momentum / funding_carry /            │
│    liq_reversal                                              │
├─────────────────────────────────────────────────────────────┤
│  Risk + PnL + Portfolio + Monitor                            │
│    KellyCheck / DrawdownTracker / VolTargetCheck /           │
│    CorrelationCheck / PnLTracker / Allocator / LiveMonitor   │
├─────────────────────────────────────────────────────────────┤
│  NT Adapter   (LiveDataClient + LiveExecutionClient)         │
├─────────────────────────────────────────────────────────────┤
│  okx_trade SDK   (REST + WS, async, no pandas)               │
└─────────────────────────────────────────────────────────────┘
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

`src/okx_trade/strategies/` ships four strategies, all subclasses of `nautilus_trader.trading.strategy.Strategy`. Backtest and live share the same code:

| Strategy | Idea | Cadence | YAML |
|---|---|---|---|
| `RangeBreakoutStrategy` | Mean-reversion on false breakout | 1H signal / 1D range | [range_breakout.yaml](configs/strategies/range_breakout.yaml) |
| `FundingCarryStrategy` | Spot+perp delta-neutral funding rate carry | 8h funding cycle | [funding_carry.yaml](configs/strategies/funding_carry.yaml) |
| `XSMomentumStrategy` | Cross-sectional momentum, vol-managed, 5 long + 5 short legs | Daily UTC 00:00 rebalance | [xs_momentum.yaml](configs/strategies/xs_momentum.yaml) |
| `LiqReversalStrategy` | Liquidation cascade reversal (z-score on `liquidation-orders`) | Event-driven | [liq_reversal.yaml](configs/strategies/liq_reversal.yaml) |

## Risk pipeline

`src/okx_trade/risk/` provides four independent checks chained by `RiskManager`, invoked once before every order:

| Check | Behavior |
|---|---|
| `VolTargetCheck` | Size from N-day realized vol → annualized vol target |
| `KellyCheck` | f\* = (p×R - q)/R, × 0.25 fractional Kelly (handed off to PnL tracker after first 20 trades) |
| `DrawdownCheck` | Halts on -3% daily / -8% weekly equity drawdown |
| `CorrelationCheck` | Rolling 30-day strategy PnL correlation > 0.7 → down-weight |

Each check accepts `RiskIntent(size)` → returns `APPROVE / SCALE / REJECT`. All checks are pure functions / pure state machines, decoupled from NT runtime.

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

# Start 4 strategies in parallel (NT TradingNode + monitor)
.venv/bin/python scripts/live.py --run

# Generate today's daily report and exit
.venv/bin/python scripts/live.py --report-only
```

VPS deployment: see [deploy/README.md](deploy/README.md), includes systemd unit + healthcheck timer + bootstrap script.

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
├── strategies/              # 4 strategies + base + confirmation/OFI + pnl_hook
├── risk/                    # vol_target / kelly / drawdown / correlation / integration
├── pnl/                     # tracker / stats / feed
├── portfolio/               # equal_weight / risk_budget
├── monitor/                 # alerts / live / daily_report
├── runtime/                 # live_node: assemble yaml + tracker + allocator → NT TradingNode
└── backtest/                # data_loader / runner

scripts/
├── live.py                  # Paper trading entrypoint (--check / --run / --report-only)
├── backtest.py              # range_breakout / xs_momentum backtest
├── backtest_funding_carry.py
├── backtest_m4_smoke.py
├── healthcheck.py           # Invoked by systemd timer
└── observation_report.sh    # day_7 / day_14 evaluation report

configs/
├── live.yaml                # 4-strategy parallel config + risk_defaults + portfolio + monitor
├── risk.yaml                # Risk parameter reference (live.yaml inlines actual values)
└── strategies/*.yaml        # Per-strategy parameters

deploy/                      # VPS systemd deployment (see deploy/README.md)
tests/unit/                  # 449 unit tests
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
- ✅ **M2**: range_breakout + funding_carry strategies + risk pipeline scaffolding
- ✅ **M3**: full risk pipeline (vol_target / kelly / drawdown / correlation)
- ✅ **M3.6**: NT integration (`RiskAwareStrategy` injects check before `submit_order`)
- ✅ **M4**: xs_momentum + liq_reversal + OFI confirmation + backtest engine
- ✅ **M5**: PnL tracker + portfolio optimizer + monitor + alerts + daily report + paper trading runtime
- 🟡 **M5 observation**: 2026-05-08 → 2026-05-22 paper trading
- 🔲 **M6**: real-money switch + Telegram alerts + portfolio rebalance scheduler
- 🔲 **M7**: multi-account / multi-venue routing

Per-milestone detail: see [CHANGELOG.md](CHANGELOG.md).

## License

Private repository, all rights reserved by the author.
