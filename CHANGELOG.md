# Changelog

格式约定：[Conventional Commits](https://www.conventionalcommits.org/)。日期为 Asia/Shanghai。

`d382446` 之前的代码（M1–M5 主体）以一次性 "Initial commit" 入库（项目早期跨 ~3 周在单一对话中迭代），之后切换到 PR 流程。本文按里程碑回顾，不严格对应 git commit。

---

## [Unreleased] — Paper trading 观察期

### Added (M6+ — 中长期策略一次性交付)

- **`feat(pricing)`** — 新增 `src/okx_trade/pricing/options.py`：纯 Python Black-Scholes pricer + Greeks（delta/gamma/vega/theta）+ implied vol Newton-Raphson + vol_premium。
- **`feat(risk)`** — 新增 `src/okx_trade/risk/stats.py`：`linreg` / `rolling_beta` / `zscore` / `engle_granger_coint` / `ou_fit` / `ou_half_life`。ADF 优先用 statsmodels，未装时降级为手写单滞后。
- **`feat(backtest)`** — 新增 `src/okx_trade/backtest/walk_forward.py`：滚动 train/test 切分 + 二分类 / 回归评估。
- **`feat(adapter)`** — 启用 `InstType.OPTION`；`parse_okx_instrument` 加 OPTION 分支（CryptoOption + OptionKind）；`Instrument` 模型加 strike/opt_type/underlying；`instrument_provider.load_all_async` 支持 `option_ulys` filter（避免一次拉 ~1000 期权）。
- **`feat(rest)`** — `public.get_option_summary(uly, exp_time_ms)`：拉 OKX opt-summary（IV + Greeks）。新增 `models.OptionSummary`。
- **`feat(strategies)`** — 新增 5 个策略：
  - `funding_cross_section` (M6)：多空 funding 横截面 + β-hedge，每个 funding cycle rebalance
  - `funding_skew_momentum` (M6)：funding rate 30d z-score ±2σ 反向交易 + ATR SL/TP
  - `stat_arb_pairs` (M7)：BTC-ETH 协整套利（Engle-Granger + OU），每日重测协整
  - `option_vol_selling` (M7)：BTC ATM short straddle + perp delta hedge
  - `ml_fusion` (M7)：XGBoost meta，每 4h 预测 → top-K 多空腿
- **`feat(strategies)`** — 新增 `strategies/_features.py`：跨策略 feature 聚合（momentum / RV / funding_z / regime_state / btc_corr）。
- **`docs`** — `strategy_roadmap.md` 全面重写，10 策略上线状态 + 5 个 M6+ 启用前置条件清单。
- **`pyproject`** — 新增 optional deps：`[stat-arb]` (statsmodels)、`[ml-fusion]` (xgboost)。

5 个 M6+ 策略默认 `enabled: false`，需要逐个 paper 验证后开启。

### Added (M5.X strategy optimization)

- **`feat(strategies)`** — 新增 `BasisArbStrategy`：OKX 交割合约 vs 现货期现套利（spot long + futures short），结算前 basis 锁定，比 funding_carry 更稳。需要 `InstType.FUTURES` 加载到 NT cache（本次顺便启用）。
- **`feat(strategies)`** — 新增 `OBImbalanceStrategy`：订单流 microprice + book imbalance 微观结构反转，订阅 OKX `books5` 频道，30s–5min 中频持仓。
- **`feat(risk)`** — 新增 `RegimeGate` 风控插件（规则版默认开 + 可选 HMM 实现），按 BTC 1d MA + 30d 实现波动率分位数判 `trending / mean_reverting / neutral`，对 momentum / reversal 类策略按映射表缩仓。
- **`feat(adapter)`** — `parse_okx_instrument` 增加 `CryptoFuture` 解析分支；`OKXInstrumentProvider` 默认加载 SWAP + SPOT + FUTURES。
- **`feat(strategies)`** — 新增 `_signals.py` 模块：`microprice` / `book_imbalance` / `ob_signal` / `annualized_basis` / `basis_decision` 跨策略共享纯函数。
- **`docs`** — 新增 `docs/strategy_roadmap.md`：记录中长期策略（funding cross-section / funding skew momentum / option vol selling）。

### Removed (M5.X)

- **`refactor(strategies)`** — 下线 `RangeBreakoutStrategy`。理由：crypto 区间假突破在 OOS 上 alpha 弱、与 xs_momentum 高度同向但噪音更大；近期事故性 fix（commits 34666fc / a6a3c8b）反映实现层不稳。删除 `src/okx_trade/strategies/range_breakout.py`、`configs/strategies/range_breakout.yaml`、`tests/unit/test_strategy_range_breakout.py`，并清理 11 处引用（live_node 注册表、live.yaml、`scripts/backtest.py` SUPPORTED_STRATEGIES、`scripts/backtest_oneyear.py`、README、ARCHITECTURE）。

### Fixed

- **`fix(exec)`** [`c966a47`](https://github.com/DrZhangXD/okx-trade/commit/c966a47) (2026-05-09) — `_build_order_request` 用 `self._instrument_provider.find()` 而非 `self.cache`（NT `LiveExecutionClient` 没有 `cache` 属性，前一版导致全部订单 reject）
- **`fix(rest)`** [`b6225da`](https://github.com/DrZhangXD/okx-trade/commit/b6225da) (2026-05-09) — `OKXAPIError` 把 `data[0].sCode/sMsg` 拼进 `str(err)`；`OKXLiveExecutionClient._build_order_request` 加 min-lot 防线（`order.quantity < instrument.min_quantity` 本地直接 raise → OrderRejected with 清晰原因，避免浪费 OKX 配额）
- **`fix(risk)`** [`2c766ce`](https://github.com/DrZhangXD/okx-trade/commit/2c766ce) (2026-05-09) — `configs/live.yaml.risk_defaults` 补上 `kelly_win_rate: 0.55, kelly_avg_r: 1.5`（之前缺失，回退到 dataclass 默认 0.5/1.0 → kelly_fraction = 0 → REJECT 全部下单，paper trading 跑了 10h 0 笔成交）
- **`fix(risk)`** [`e4eeb06`](https://github.com/DrZhangXD/okx-trade/commit/e4eeb06) (2026-05-09) — `configs/risk.yaml`（参考样板）同步更新冷启动 Kelly 默认值，加注释解释 (0.5,1.0) 为何不是"中性"
- **`fix(ops)`** [`aafce6f`](https://github.com/DrZhangXD/okx-trade/commit/aafce6f) (2026-05-08) — `observation_report.sh` 用 `ts_ms` 而非 `ts`（pnl.sqlite 的 `equities` 表列名是 `ts_ms`，毫秒时间戳）

### Added

- **`feat(ops)`** [`84dee27`](https://github.com/DrZhangXD/okx-trade/commit/84dee27) (2026-05-08) — `scripts/observation_report.sh` 生成 markdown 观察报告（服务存活 / OOM / PnL / WARN top / daily reports / alerts），由 root cron 在 day_7（2026-05-15 23:30）和 day_14（2026-05-22 23:30）自动跑

### Bug-fix history（PR #1 + #2，merge 5/8 22:00）

- **`fix(parsing)`** [`875214f`](https://github.com/DrZhangXD/okx-trade/commit/875214f) — bar close time 在 parser 内部派生，支持所有 aggregation
- **`fix(adapter)`** [`ba2ac2b`](https://github.com/DrZhangXD/okx-trade/commit/ba2ac2b) — instrument 预加载 + 容忍 preopen + 修 xs_momentum universe
- **`feat(backtest)`** [`2584053`](https://github.com/DrZhangXD/okx-trade/commit/2584053) — xs_momentum 接入 CLI + `backtest_funding_carry.py` 现金流估算器
- **`fix(backtest)`** [`6a9347f`](https://github.com/DrZhangXD/okx-trade/commit/6a9347f) — 解锁 1 年历史回测

---

## M5 — Paper trading runtime + monitoring（2026-05-08 入库到 `d382446`）

### Added

- **PnL tracker**（`src/okx_trade/pnl/`）
  - `PnLTracker` 把每笔成交 + 每日 equity 写 SQLite（`var/pnl.sqlite`），跨进程重启不丢
  - `compute_win_rate_avg_r` / `compute_daily_returns` / `compute_sharpe` 纯函数 stats
  - `feed_risk_handles` 把 stats 回灌进 `KellyCheck.set_stats()` / `CorrelationCheck.update_strategy_pnl()`
- **Portfolio optimizer**（`src/okx_trade/portfolio/`）
  - `EqualWeightAllocator` 冷启动用
  - `RiskBudgetingAllocator` 30 日数据后切（inverse-vol + correlation penalty）
- **Monitor + Alerts + Daily Report**（`src/okx_trade/monitor/`）
  - `LiveMonitor` 60s 轮询风控状态
  - sinks: `LogSink` / `JsonlSink` / `TelegramSink`（M6 接入）
  - `DailyReporter` 每日 JSON 报表
- **Live runtime**（`src/okx_trade/runtime/live_node.py`）
  - `build_live_context(yaml)` → 构造 NT `TradingNode` + tracker + allocator + monitor
- **Live entrypoint**（`scripts/live.py`）
  - `--check` / `--run` / `--report-only`
- **VPS 部署**（`deploy/`）
  - `bootstrap.sh` 全自动 setup（venv + systemd unit + healthcheck timer）
  - `okx-trade.service` / `okx-trade-healthcheck.service` / `.timer`
  - `scripts/healthcheck.py` 由 timer 每 5 分钟跑

---

## M4 — Cross-sectional + event-driven 策略 + 回测（initial commit）

### Added

- **`XSMomentumStrategy`**（`src/okx_trade/strategies/xs_momentum.py`）
  - 横截面动量，多腿 5 多 5 空，target_vol_annualized=15%，每日 UTC 00:00 rebalance
- **`LiqReversalStrategy`**（`src/okx_trade/strategies/liq_reversal.py`）
  - 强平瀑布反转，订阅 `liquidation-orders` 频道，z-score > 阈值触发反向 entry
- **`OFIFilter`**（`src/okx_trade/strategies/confirmation.py`）
  - 共用 helper：trades 频道 OFI 反向时降仓 50% / 跳过
- **回测套件**（`src/okx_trade/backtest/`）
  - `data_loader.py`：OKX 历史 bars → NT `ParquetDataCatalog`
  - `runner.py`：`build_okx_venue_config` + `run_backtest` + `BacktestSummary`
  - `scripts/backtest.py` CLI（range_breakout / xs_momentum）
  - `scripts/backtest_m4_smoke.py`（端到端 sanity）
- **Universe 解析**（`src/okx_trade/adapter/instrument_provider.py`）
  - 按 24h volume 排序 top N USDT-settled 永续

---

## M3 — Risk pipeline + NT 集成（initial commit）

### Added

- **风控 4 件套**（`src/okx_trade/risk/`）
  - `VolTargetCheck`：N 日 realized vol → 目标仓位
  - `KellyCheck`：f\* = (p×R - q)/R × 0.25（冷启动用 dataclass 默认）
  - `DrawdownCheck` + `DrawdownTracker`：日 / 周 PnL 状态机
  - `CorrelationCheck`：滚动相关性矩阵
- **`RiskManager`**（`src/okx_trade/risk/base.py`）：串联 N 个 check，`check_all(intent)` 返回 APPROVE / SCALE / REJECT
- **`build_risk_manager(config)`**（`integration.py`）：yaml 工厂
- **`apply_risk_manager(strategy, manager, intent)`**（M3.6）：策略基类在 `submit_order` 前调，避免侵入 NT `RiskEngine`

---

## M2 — 策略雏形 + Strategy 基类（initial commit）

### Added

- **`RangeBreakoutStrategy`**（`src/okx_trade/strategies/range_breakout.py`）
  - 区间假突破回归，1H 信号 / 1D 区间，移植自上一版 scalp 项目
- **`FundingCarryStrategy`**（`src/okx_trade/strategies/funding_carry.py`）
  - spot+perp delta-neutral 资金费率套利，8h funding cycle 触发
- **`BarBuffer`** / **`position_contracts`**（`base.py`）：策略共用 helper
- **`pnl_hook`**（`pnl_hook.py`）：把 NT `PositionEvent` 转成 `PnLTracker.record_trade`
- **NautilusTrader 集成**（`src/okx_trade/adapter/`）
  - `parsing.py`：OKX raw → NT `Instrument` / `Bar` / `QuoteTick`
  - `data.py`：`OKXLiveDataClient`
  - `execution.py`：`OKXLiveExecutionClient`（含 `tdMode` / `posSide` 解析）
  - `factories.py`：装配 NT `TradingNode`

---

## M1 — REST + WebSocket SDK（initial commit）

### Added

- **REST 客户端**（`src/okx_trade/rest/`）
  - `transport.py`：`httpx` 重试 + 限频 + 业务码分类
  - `account.py` / `market.py` / `public.py` / `trade.py`：各 endpoint 类型化封装
  - 限频组（`/api/v5/trade/order` 60 req/2s 等）
- **WebSocket 客户端**（`src/okx_trade/ws/`）
  - 三连接（public / private / business）
  - 订阅幂等 + 自动重连 + ping/pong
  - WS 下单（`ws.trade.place_order`）
- **认证**（`src/okx_trade/auth.py`）
  - HMAC-SHA256 签名（纯函数，便于单测）
- **Models**（`src/okx_trade/models/`）
  - pydantic v2，价格 / 数量用 `Decimal`
- **配置**（`src/okx_trade/config.py`）
  - `OKXSettings` 从 env 加载凭证 / 代理
- **异常体系**（`src/okx_trade/exceptions.py`）
  - `OKXError` → `OKXNetworkError` / `OKXRateLimitError` / `OKXAPIError`
  - `OKXAPIError` 子类：`OKXAuthError` / `OKXInsufficientBalance` / `OKXOrderNotFound`
  - `classify_business_error` 根据 sCode 派子类
- **131 个 unit test**，覆盖签名 / 限频 / 订阅幂等 / 重连 / WS 下单
