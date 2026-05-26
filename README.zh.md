# okx-trade

[English](README.md) · **简体中文**

OKX 量化交易全栈：底层 SDK（REST + WebSocket，纯 asyncio）+ NautilusTrader 适配器 + **10 个策略** + 因子研究 lab + 多层风控 + PnL 跟踪 + 监控 + 回测 + VPS 部署脚本。

**当前状态**：Paper trading 在 Aliyun VPS 24/7 跑中（**9 策略并行 enabled**）。原 **2026-05-22** 观察期评估被 2026-05-25 DOT 插针事故推后，现处于"补防御层 + 适配 OKX 真实长短双向账户"阶段。**949/949 单测全绿**。M7（实盘切换 + Telegram alert）等下面的 Phase 1 收尾完。

---

## Quick Start

```bash
# 1) 安装（开发：含 NT + numpy 等策略层）
pip install -e ".[strategy,dev]"

# 2) 配置 OKX 凭证
cp .env.example .env
# 编辑 .env，填 OKX_API_KEY / SECRET / PASSPHRASE / IS_DEMO=true

# 3) 跑单测（无网络）
pytest tests/unit -v             # 949 个

# 4) 跑集成测试（需 demo 凭证 + 国内代理）
pytest tests/integration -v -m integration
```

## 四层架构

```
┌─────────────────────────────────────────────────────────────────────┐
│  Research Layer (P1, 2026-05-19)                                    │
│    FactorRegistry / FactorPanel / grade_factor / walk_forward       │
│    CLI: list / fetch / eval / grade-all / approve / backtest-...    │
├─────────────────────────────────────────────────────────────────────┤
│  Strategy Layer  (10 NautilusTrader Strategy 子类)                  │
│    funding_carry / xs_momentum / liq_reversal / basis_arb /         │
│    ob_imbalance / funding_cross_section / funding_skew_momentum /   │
│    stat_arb_pairs / option_vol_selling / ml_fusion /                │
│    factor_portfolio (generic factor synthesizer)                    │
├─────────────────────────────────────────────────────────────────────┤
│  Risk + PnL + Portfolio + Monitor                                   │
│    KellyCheck / VolTargetCheck / DrawdownCheck /                    │
│    AccountDrawdownCheck (Phase 0 单源 kill-switch) /                │
│    CorrelationCheck / RegimeGateCheck /                             │
│    PnLTracker / Allocator / LiveMonitor                             │
├─────────────────────────────────────────────────────────────────────┤
│  NT Adapter   (LiveDataClient + LiveExecutionClient)                │
├─────────────────────────────────────────────────────────────────────┤
│  okx_trade SDK   (REST + WS, async, no pandas)                      │
└─────────────────────────────────────────────────────────────────────┘
```

详细数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## SDK 用法（最底层）

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

### WebSocket 订阅

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

更多 SDK 示例见 [`examples/`](examples/)。

## 策略层

`src/okx_trade/strategies/` 下的策略全是 `nautilus_trader.trading.strategy.Strategy` 子类，回测和实盘共享同一份代码：

| 策略 | 思路 | 频率 | 当前状态 |
|---|---|---|---|
| `FundingCarryStrategy` | spot+perp delta-neutral 资金费率套利 | 8h funding cycle | ✅ enabled |
| `XSMomentumStrategy` | 横截面动量（vol-managed），多空各 5 腿 | 每日 UTC 00:00 rebalance | ✅ enabled |
| `LiqReversalStrategy` | 强平瀑布反转（z-score on `liquidation-orders`） | 事件驱动 | ✅ enabled |
| `BasisArbStrategy` (M5) | 交割合约 vs 现货期现套利（spot long + futures short） | 每小时检查 basis | ✅ enabled |
| `OBImbalanceStrategy` (M5) | 订单流 microprice + book imbalance 微观结构反转 | 秒级聚合，分钟级持仓 | ✅ enabled |
| `FundingXSStrategy` (M6+) | funding 横截面多空 + β-hedge | 每 8h funding cycle | ✅ enabled (5/19) |
| `FundingSkewStrategy` (M6+) | funding rate ±2σ 反向 | ~30min poll | ✅ enabled (5/19) |
| `StatArbStrategy` (M6+) | 协整对（BTC-ETH 等）spread 反转 | 每根 1H bar | ✅ enabled (5/19) |
| `FactorPortfolioStrategy` (P1) | 通用因子合成器（z-score + top-K，读 yaml 选因子） | bar-driven 4h rebalance | ✅ enabled (5/19) |
| `OptionVolStrategy` (M6+) | BTC short straddle + perp delta-hedge | 每小时 check | ❌ disabled |
| `MLFusionStrategy` (M6+) | XGBoost meta（多个 alpha 融合预测 4h forward return） | 每 4h | ❌ disabled |

剩 2 策略 disabled：`option_vol_selling` 需 live_node 动态注入 `option_ulys` filter；`ml_fusion` 需 `pip install xgboost` + 写 retrain 脚本。详见 [docs/strategy_roadmap.md](docs/strategy_roadmap.md)。

已下线：`RangeBreakoutStrategy`（M5.X，alpha 弱 + 实现不稳）。

### 冷启动消除：REST warmup

`funding_skew_momentum` / `stat_arb_pairs` / `funding_cross_section` / `factor_portfolio` 都在 `on_start` 异步调 OKX REST 拉历史 bars/funding 填 buffer，VPS 重启后即时具备全功能，不用等数天-数十天累计 live 数据。配置项 `warmup_via_rest: bool` 或 `warmup_via_rest_days: int`。

### 因子研究 lab (P1, 2026-05-19)

`okx_trade.research` 模块：CLI 评估任意因子的 IC / IR / decay / turnover / net-PnL，通过 grade 的因子直接喂 yaml 上线给 `FactorPortfolioStrategy`：

```bash
# 拉数据 + 跑 15 个 v1 因子 grade
python -m okx_trade.research fetch --start 2025-11-01 --end 2026-05-15 --universe top30
python -m okx_trade.research grade-all --start 2025-11-01 --end 2026-05-15 --horizon 1d

# approve 通过门槛的因子（写入 configs/factor_portfolio.yaml）
python -m okx_trade.research approve --factor basis_z_30d --weight 0.40

# 端到端回测
python -m okx_trade.research backtest-portfolio --total-bars 2000
```

详见 [strategy_roadmap.md](docs/strategy_roadmap.md#factor-research-lab-p1-2026-05-19)。

## 风控管道

`src/okx_trade/risk/` 提供独立 check，由 `RiskManager` 串联，下单前调一次：

| Check | 行为 | 层级 |
|---|---|---|
| `AccountDrawdownCheck` | OKX `totalEq` 日 -3% / 周 -8% 触发整账户 kill-switch | **账户级（单源单实例）** |
| `VolTargetCheck` | 按 N 日 realized vol 反推目标仓位 | 策略级 |
| `KellyCheck` | f\* = (p×R - q)/R，× 0.25 折扣（前 20 笔成交后 PnL tracker 接管动态更新） | 策略级 |
| `DrawdownCheck` | 日 -3% / 周 -8% 触发熔断（per-strategy；Phase 0 暂未喂数据，Phase 1 接 PnLTracker） | 策略级 |
| `CorrelationCheck` | 滚动 30 日策略 PnL 相关性 > 0.7 降权 | 策略级 |
| `RegimeGateCheck` | BTC trending / mean_reverting / neutral，按 strategy_kind 映射缩仓 | 全局 detector |

每个 check 接受 `RiskIntent(intent.size)` → 返回 `APPROVE / SCALE / REJECT`。所有 check 都是纯函数 / 纯状态机，不依赖 NT 运行时。

### DD 架构分层（Phase 0, 2026-05-20）

- **账户级 `AccountDrawdownTracker`**：单例，由 `LiveMonitor` 持有，每次 alloc_refresh 推送 OKX `totalEq` 进去
- **策略级 `DrawdownTracker`**：每策略各一个，**当前不喂数据**（Phase 1 后续接 PnLTracker 的 per-strategy realized PnL 实现真隔离）
- 任一 `AccountDrawdownCheck` 触发即所有策略 kill-switch；单源单告警，杜绝之前多源喂 tracker 的 27% 假 breach

### FundingXS 三层防御（2026-05-26）

DOT-USDT-SWAP demo 撮合在一根 1 分钟 K 内从 $1.45 → $121.985 插针，把一笔 `FundingXSStrategy` short 强平烧掉 -$51,128 paper equity。新增三个独立防御层，每层一个 `enable_*` 开关在 `configs/strategies/funding_cross_section.yaml`：

1. **每腿 isolated margin** —— 下单时附 `tags=["td_mode:isolated"]`，OKX 把单腿最大损失锁在分配 margin 内（账户的 0.5–5%），跨腿级联强平不再可能。
2. **动态 leverage** —— `clip(2 + 3 × |funding_z + basis_z|/2, 2, 10)`。conviction 越高 leverage 越高 → isolated margin 越小 → 单腿损失上限更低。把 leverage 从纯成本工具升级成 conviction 表达。
3. **Outlier guard** —— 入场前过滤：近 1h vol > 近 24h baseline × 3 倍即跳过该腿。1m bar feed 独立订阅，与策略 β-hedge 的 1D feed 解耦。

`_execute_diff` 走**两阶段提交**：先对每条要开的腿 pre-validate `set_leverage`，任何一条失败就 abort 整轮 open-phase（保留 closes），避免留单向 residual。Spec + plan + runbook：

- [docs/superpowers/specs/2026-05-26-funding-xs-isolated-margin-design.md](docs/superpowers/specs/2026-05-26-funding-xs-isolated-margin-design.md)
- [docs/superpowers/plans/2026-05-26-funding-xs-isolated-margin.md](docs/superpowers/plans/2026-05-26-funding-xs-isolated-margin.md)
- 回滚 runbook：[`docs/operations.md` §五·B](docs/operations.md)

Phase-1 后续：抽 `IsolatedMarginPolicy` + `OutlierGuard` 成公共 risk handle，让 `FundingCarry` / `XSMomentum` / `FundingSkew` 也能接入。

## PnL 跟踪 + 组合优化

| 模块 | 用途 |
|---|---|
| `pnl/tracker.py` | 每笔成交 + 每日 equity 写 SQLite (`var/pnl.sqlite`) |
| `pnl/stats.py` | 算 `win_rate` / `avg_R` / Sharpe / Sortino |
| `pnl/feed.py` | 把 stats 回灌进 `KellyCheck.set_stats()` / `CorrelationCheck.update_strategy_pnl()` |
| `portfolio/equal_weight.py` | 冷启动用，4 策略平均分钱 |
| `portfolio/risk_budget.py` | 30 日数据后切，inverse-vol + correlation penalty |

## 监控 + 告警 + 日报

`src/okx_trade/monitor/`：

- **`LiveMonitor`** 每 60s 轮询风控状态，触发条件 emit `Alert`
- **Sinks**: `LogSink`、`JsonlSink` (`var/alerts.jsonl`)、`TelegramSink`（M6 接入）
- **`DailyReporter`** 每日把 tracker 数据写 JSON 到 `var/daily_reports/`

## 回测

```bash
# xs_momentum 30 天回测
.venv/bin/python scripts/backtest.py \
  --strategy xs_momentum \
  --instrument-ids "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP" \
  --signal-bar 1H --total-bars 720 --equity 10000

# funding_carry 1 年现金流估算
.venv/bin/python scripts/backtest_funding_carry.py \
  --perp-symbol BTC-USDT-SWAP --total 1095 --equity 10000
```

回测引擎是 NautilusTrader 的 `BacktestNode`，数据走 `ParquetDataCatalog`。`scripts/backtest.py --reuse-data` 可复用本地 catalog 跳过下载。

## Live Paper Trading

```bash
# 配置校验（不启动 NT）
.venv/bin/python scripts/live.py --check

# 启动 9 策略并行 paper trading（NT TradingNode + monitor）
.venv/bin/python scripts/live.py --run

# 仅生成今日 daily report 后退出
.venv/bin/python scripts/live.py --report-only
```

VPS 部署见 [deploy/README.md](deploy/README.md)，含 systemd unit + healthcheck timer + bootstrap 脚本。

运维手册（incident 应对、诊断脚本、cron 配置）见 [docs/operations.md](docs/operations.md)。

观察期里随时拉报告：

```bash
ssh okx-vps '/home/okxtrade/okx-trade/scripts/observation_report.sh adhoc | xargs cat'
```

## 项目结构

```
src/okx_trade/
├── auth.py                  # HMAC-SHA256 签名（纯函数）
├── config.py                # OKXSettings (pydantic-settings)
├── exceptions.py            # OKXAPIError 体系（含 sCode/sMsg 透传）
├── enums.py                 # InstType / TdMode / Side / OrdType / BarSize ...
├── models/                  # pydantic v2 数据模型
├── rest/                    # REST 客户端：account / market / public / trade / transport
├── ws/                      # WS 客户端：public / private / business 三连接
├── adapter/                 # NT 适配器：data / execution / parsing / instrument_provider / factories
├── strategies/              # 10 个策略 + base + confirmation/OFI + pnl_hook + _features
├── research/                # P1 因子研究 lab：panel / registry / compute / data / grade / store / report / cli + factors/
├── pricing/                 # Black-Scholes pricer + Greeks（option_vol_selling 用）
├── risk/                    # vol_target / kelly / drawdown (+ Account 级) / correlation / regime / stats / integration
├── pnl/                     # tracker / stats / feed
├── portfolio/               # equal_weight / risk_budget
├── monitor/                 # alerts / live / daily_report
├── runtime/                 # live_node：把 yaml + tracker + allocator + account DD tracker 装进 NT TradingNode
└── backtest/                # data_loader / runner / plotting / walk_forward

scripts/
├── live.py                  # paper trading entrypoint (--check / --run / --report-only)
├── backtest.py              # 多策略回测（--strategy ...）
├── backtest_funding_carry.py
├── backtest_oneyear.py
├── backtest_m4_smoke.py
├── healthcheck.py           # systemd timer 调用
├── observation_report.sh    # day_7 / day_14 评估报告
├── stat_arb_observe.sh      # stat_arb 24h / lunch 观察
├── reconcile_okx_positions.py  # ExecStartPre：启动前对账
├── factor_research_smoke.sh    # 因子 lab end-to-end 烟测
├── diag_account_bills.py    # OKX 流水诊断（按 type/subType 汇总）
└── diag_mtm_swing.py        # 当前持仓 + 1H candles MTM 对照

configs/
├── live.yaml                # 9 enabled 策略 + risk_defaults + portfolio + monitor + alerts
├── risk.yaml                # 风控参数样板（参考用，live.yaml 内联实际值）
├── factor_portfolio.yaml    # FactorPortfolioStrategy 配置（5 approved factors）
└── strategies/*.yaml        # 每个策略的独立参数

deploy/                      # VPS systemd 部署（见 deploy/README.md）
docs/
├── strategy_roadmap.md      # 策略状态 + 工程基础设施 todo
├── operations.md            # 运维手册（incident 应对、cron、diag 脚本）
└── superpowers/             # spec / plan 文档（P1 因子 lab 等）

tests/unit/                  # 803 个 unit test
tests/integration/           # 默认 skip，配凭证后手动跑
```

## 设计原则

- 全异步（httpx + websockets）
- 价格 / 数量用 `Decimal`，杜绝浮点误差
- SDK 层不依赖 pandas / numpy；策略层按需引入
- 风控 / PnL / Portfolio 都是纯函数 / 纯状态机，便于单测
- 策略代码在回测和实盘共享一份（NT 抽象保证）
- 国内代理通过 `.env` 配置，REST 与 WS 共用

## 开发

```bash
# 运行所有 unit test
pytest tests/unit -v

# 跑特定模块
pytest tests/unit/test_exceptions.py -v
pytest tests/unit/ -k "kelly or risk" -v

# 类型检查（开发依赖里）
mypy src/

# Lint
ruff check src/ tests/
```

提交规范：Conventional Commits（`feat:` / `fix:` / `refactor:` / `docs:` / `test:`）。

## Roadmap

- ✅ **M1**: REST + WS SDK
- ✅ **M2**: range_breakout (已下线) + funding_carry 策略 + 风控雏形
- ✅ **M3**: 完整风控管道（vol_target / kelly / drawdown / correlation）
- ✅ **M3.6**: NT 集成（`RiskAwareStrategy` 在 `submit_order` 前注入）
- ✅ **M4**: xs_momentum + liq_reversal + OFI confirmation + 回测引擎
- ✅ **M5**: PnL tracker + portfolio optimizer + monitor + alerts + 日报 + paper trading runtime
- ✅ **M6+**: 5 个中长期策略（funding_xs / funding_skew / stat_arb / option_vol / ml_fusion）+ regime gate + walk-forward + OPTION 适配
- ✅ **P1 (2026-05-19)**: 因子研究 lab（15 因子 + grade pipeline + FactorPortfolioStrategy generic synthesizer）
- ✅ **Phase 0 DD (2026-05-20)**: AccountDrawdownTracker 单源 kill-switch + REST warmup 消除冷启动
- 🟡 **观察期**：2026-05-08 → **2026-05-22 (day_14)** paper trading
- 🔲 **Phase 1 DD**: per-strategy DrawdownTracker 接 PnLTracker realized PnL（真隔离）
- 🔲 **M7**: 实盘切换 + Telegram alert + Portfolio rebalance scheduler
- 🔲 **M8**: 多账户 / 多 venue 路由 / 期权策略上线 / ml_fusion 上线

每个里程碑详见 [CHANGELOG.md](CHANGELOG.md)。

## License

仓库公开可读但**未授予使用许可**——© 2026 DrZhangXD，All Rights Reserved。如需引用 / 二次开发，请先联系作者获取书面许可。
