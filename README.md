# okx-trade

[English](README.en.md) · **简体中文**

OKX 量化交易全栈：底层 SDK（REST + WebSocket，纯 asyncio）+ NautilusTrader 适配器 + 4 个策略 + 风控 + PnL 跟踪 + 监控 + 回测 + VPS 部署脚本。

**当前状态**：Paper trading 在 Aliyun VPS 24/7 跑中，观察期至 **2026-05-22** → 评估推进 M6（实盘切换 + Telegram alert）。449/449 单测全绿。

---

## Quick Start

```bash
# 1) 安装（开发：含 NT + numpy 等策略层）
pip install -e ".[strategy,dev]"

# 2) 配置 OKX 凭证
cp .env.example .env
# 编辑 .env，填 OKX_API_KEY / SECRET / PASSPHRASE / IS_DEMO=true

# 3) 跑单测（无网络）
pytest tests/unit -v             # 449 个

# 4) 跑集成测试（需 demo 凭证 + 国内代理）
pytest tests/integration -v -m integration
```

## 三层架构

```
┌─────────────────────────────────────────────────────────────┐
│  Strategy Layer  (NautilusTrader Strategy 子类)              │
│    xs_momentum / funding_carry / liq_reversal /              │
│    basis_arb / ob_imbalance (M5)                             │
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

| 策略 | 思路 | 频率 | YAML |
|---|---|---|---|
| `FundingCarryStrategy` | spot+perp delta-neutral 资金费率套利 | 8h funding cycle | [funding_carry.yaml](configs/strategies/funding_carry.yaml) |
| `XSMomentumStrategy` | 横截面动量（vol-managed），多空各 5 腿 | 每日 UTC 00:00 rebalance | [xs_momentum.yaml](configs/strategies/xs_momentum.yaml) |
| `LiqReversalStrategy` | 强平瀑布反转（z-score on `liquidation-orders`） | 事件驱动 | [liq_reversal.yaml](configs/strategies/liq_reversal.yaml) |
| `BasisArbStrategy` (M5) | 交割合约 vs 现货期现套利（spot long + futures short） | 每小时检查 basis | [basis_arb.yaml](configs/strategies/basis_arb.yaml) |
| `OBImbalanceStrategy` (M5) | 订单流 microprice + book imbalance 微观结构反转 | 秒级聚合，分钟级持仓 | [ob_imbalance.yaml](configs/strategies/ob_imbalance.yaml) |
| `FundingXSStrategy` (M6+) | funding 横截面多空 + β-hedge | 每 8h funding cycle | [funding_cross_section.yaml](configs/strategies/funding_cross_section.yaml) |
| `FundingSkewStrategy` (M6+) | funding rate ±2σ 反向 | ~30min poll | [funding_skew_momentum.yaml](configs/strategies/funding_skew_momentum.yaml) |
| `StatArbStrategy` (M6+) | 协整对（BTC-ETH 等）spread 反转 | 每根 1H bar | [stat_arb_pairs.yaml](configs/strategies/stat_arb_pairs.yaml) |
| `OptionVolStrategy` (M6+) | BTC short straddle + perp delta-hedge | 每小时 check | [option_vol_selling.yaml](configs/strategies/option_vol_selling.yaml) |
| `MLFusionStrategy` (M6+) | XGBoost meta（多个 alpha 融合预测 4h forward return） | 每 4h | [ml_fusion.yaml](configs/strategies/ml_fusion.yaml) |

M6+ 策略默认 `enabled: false`，需逐个 paper 验证后开启。详见 [docs/strategy_roadmap.md](docs/strategy_roadmap.md)。

已下线：`RangeBreakoutStrategy`（M5.X，alpha 弱 + 实现不稳）。

## 风控管道

`src/okx_trade/risk/` 提供 4 个独立 check，由 `RiskManager` 串联，下单前调一次：

| Check | 行为 |
|---|---|
| `VolTargetCheck` | 按 N 日 realized vol 反推目标仓位 |
| `KellyCheck` | f\* = (p×R - q)/R，× 0.25 折扣（前 20 笔成交后 PnL tracker 接管动态更新） |
| `DrawdownCheck` | 日 -3% / 周 -8% 触发熔断 |
| `CorrelationCheck` | 滚动 30 日策略 PnL 相关性 > 0.7 降权 |

每个 check 接受 `RiskIntent(intent.size)` → 返回 `APPROVE / SCALE / REJECT`。所有 check 都是纯函数 / 纯状态机，不依赖 NT 运行时。

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

# 启动 4 策略并行 paper trading（NT TradingNode + monitor）
.venv/bin/python scripts/live.py --run

# 仅生成今日 daily report 后退出
.venv/bin/python scripts/live.py --report-only
```

VPS 部署见 [deploy/README.md](deploy/README.md)，含 systemd unit + healthcheck timer + bootstrap 脚本。

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
├── strategies/              # 4 个策略 + base + confirmation/OFI + pnl_hook
├── risk/                    # vol_target / kelly / drawdown / correlation / integration
├── pnl/                     # tracker / stats / feed
├── portfolio/               # equal_weight / risk_budget
├── monitor/                 # alerts / live / daily_report
├── runtime/                 # live_node：把 yaml + tracker + allocator 装进 NT TradingNode
└── backtest/                # data_loader / runner

scripts/
├── live.py                  # paper trading entrypoint (--check / --run / --report-only)
├── backtest.py              # xs_momentum 回测（其他策略另有专用脚本）
├── backtest_funding_carry.py
├── backtest_m4_smoke.py
├── healthcheck.py           # systemd timer 调用
└── observation_report.sh    # day_7 / day_14 评估报告生成

configs/
├── live.yaml                # 4 策略并行配置 + risk_defaults + portfolio + monitor
├── risk.yaml                # 风控参数样板（参考用，live.yaml 内联实际值）
└── strategies/*.yaml        # 每个策略的独立参数

deploy/                      # VPS systemd 部署（见 deploy/README.md）
tests/unit/                  # 449 个 unit test
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
- 🟡 **M5 观察期**：2026-05-08 → 2026-05-22 paper trading
- 🔲 **M6**: 实盘切换 + Telegram alert + Portfolio rebalance scheduler
- 🔲 **M7**: 多账户 / 多 venue 路由

每个里程碑详见 [CHANGELOG.md](CHANGELOG.md)。

## License

仓库公开可读但**未授予使用许可**——© 2026 DrZhangXD，All Rights Reserved。如需引用 / 二次开发，请先联系作者获取书面许可。
