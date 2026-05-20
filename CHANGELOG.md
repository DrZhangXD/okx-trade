# Changelog

格式约定：[Conventional Commits](https://www.conventionalcommits.org/)。日期为 Asia/Shanghai。

`d382446` 之前的代码（M1–M5 主体）以一次性 "Initial commit" 入库（项目早期跨 ~3 周在单一对话中迭代），之后切换到 PR 流程。本文按里程碑回顾，不严格对应 git commit。

---

## [Unreleased] — Paper trading 观察期

### Added (P1 — 因子研究实验室 + FactorPortfolioStrategy, 2026-05-19)

- **`feat(rest)`** — `public.get_open_interest` / `get_open_interest_history` / `get_open_interest_history_extended`：OI 当前快照 + 历史回放 + 分页扩展。`models.OpenInterest` / `OpenInterestPoint` 解析。
- **`feat(research)`** — 新增 `src/okx_trade/research/` 模块：`FactorPanel` dataclass（多 inst 多频特征容器）、`@register_factor` 装饰器、`compute_factor` 单因子求值、`grade_factor` (IC/IR/decay/turnover/net-PnL)、`walk_forward_grade` (OOS 滚窗)、`fetch_panel` parquet 缓存、`FactorStore` sqlite 元数据 + grade 历史、markdown report 渲染器。
- **`feat(research/factors)`** — 15 个 v1 因子分 5 类：momentum (`momentum_1d_reversal` / `momentum_3d` / `momentum_7d` / `momentum_risk_adj_7d`)、funding/OI (`funding_current` / `funding_z_30d` / `oi_change_1d` / `oi_to_volume_ratio`)、basis (`basis_apr` / `basis_z_30d`)、volatility (`rv_pct_365d` / `rv_skew_up_down` / `vol_of_vol_30d`)、flow (`spread_avg_1d` / `taker_buy_ratio_1d`)。
- **`feat(research/cli)`** — `python -m okx_trade.research <list|fetch|eval|approve|reject|backtest-portfolio|report|grade-all|wf-grade|corr-matrix>`。online 子命令直连 OKX REST + parquet 缓存到 `var/factor_research/`。
- **`feat(strategies)`** — `FactorPortfolioStrategy`（generic factor synthesizer，z-score + top-K 多空 + 4h rebalance）。`--warmup-days` 从 panel 缓存预填 buffer 避免 30 天冷启动。spot bar 订阅 + REST polling 让 basis/funding/OI 因子在 live 模式实时可用。
- **`config`** — `configs/factor_portfolio.yaml`：5 个 approved 因子（basis_z_30d 0.40 / basis_apr 0.30 / momentum_1d_reversal 0.20 / funding_z_30d 0.10 / funding_current 0.05），10 个 USDT-perp universe，2026-05-19 paper trading 启用。
- **`docs`** — `docs/superpowers/specs/2026-05-19-factor-research-lab-design.md` (545 行设计) + `docs/superpowers/plans/2026-05-19-factor-research-lab.md` (4128 行 / 21 TDD task)。

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

### Fixed (2026-05-20 — DD architecture + stat_arb warmup)

- **`fix(monitor)`** — `_build_okx_equity_provider` 改读 `Balance.total_eq`（整账户净值，所有币种 + 未实现 PnL）而非 `USDT.avail_eq`（USDT 单币扣保证金后可用）。后者在开仓冻结保证金时瞬间下降，让 DD tracker 把"margin freeze"误判为"权益下跌"。2026-05-20 00:00 UTC FundingXS 整点 rebalance 开 6 仓 ~$46k notional 冻 ~$3099 USDT 保证金，触发全部 9 策略 daily_breach 假警报。totalEq 实际完全没动；OKX bills 仅 -4.75 USDT 真实现金流。
- **`fix(strategies)`** — 10 处策略侧 `drawdown_tracker.record_equity(...)` 清零（funding_carry / xs_momentum / liq_reversal / basis_arb / ob_imbalance / funding_cross_section / funding_skew_momentum / stat_arb_pairs / ml_fusion / option_vol_selling）。M6+.X fix #4 已把 push 统一到 monitor 中央源，策略侧旧 push 用的是 NT 内部 USDT cached balance，与 monitor 喂的 totalEq 差异巨大（59k vs 82k），让 tracker 看到 27% 假"暴跌"。
- **`feat(risk)`** — Phase 0 DD 架构分层：新增 `AccountDrawdownTracker` 单例 + `AccountDrawdownCheck`。`LiveMonitor` 持有一份，`_refresh_allocations` 只推送给它（不再推给 N 个 per-strategy tracker）。`build_risk_manager(...)` 接收 `account_drawdown_tracker=` 注入到所有策略 risk pipeline 前置。任一策略命中即全员 kill-switch。per-strategy `DrawdownTracker` 保留但不再被喂数据（Phase 1 后续接 PnLTracker 做真正的每策略隔离）。
- **`feat(strategies)`** — `StatArbConfig.warmup_via_rest: bool = True`。`on_start` 启动 async 任务调 OKX REST 一次性拉 1440 根 1H BTC + ETH 历史 close 喂 deque，立即触发首次 engle_granger_coint 检验。之前 `lookback_bars=1440`（60 天 × 24h）需要服务连续运行 60 天才能跑协整，新部署/重启永远不开仓。
- **`add(scripts)`** — `scripts/diag_account_bills.py` + `scripts/diag_mtm_swing.py`：OKX 账户流水按 type/subType 汇总诊断 + 当前持仓 MTM × 1H candles 变化对照，用于将来怀疑 MTM/ledger 异常时定位。

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
