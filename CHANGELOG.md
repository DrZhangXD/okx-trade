# Changelog

格式约定：[Conventional Commits](https://www.conventionalcommits.org/)。日期为 Asia/Shanghai。

`d382446` 之前的代码（M1–M5 主体）以一次性 "Initial commit" 入库（项目早期跨 ~3 周在单一对话中迭代），之后切换到 PR 流程。本文按里程碑回顾，不严格对应 git commit。

---

## [Unreleased] — Paper trading 观察期

### Changed (减配止血:ob_imbalance / liq_reversal, 2026-06-09)

- **`config`** — 针对 8 天 -3.8% 的 paper 持续回撤做精准减配止血(allocator 是等权、无按策略权重旋钮 → 用 per-strategy `risk_pct`)。7 天账本净额排名定位失血源:
  - **`ob_imbalance` risk_pct 0.15% → 0.05%(1/3)** —— 头号失血:7 天净 **-139**,手续费 -174 是毛 alpha(+35)的 **5 倍** = 灾难性 fee churn(920 单)。砍仓把绝对失血降到 ~1/3,保留观察;真正修法(降频)需 books5 回测,blocked。
  - **`liq_reversal` risk_pct 0.25% → 0.125%(再减半)** —— #2 失血(7 天 -32,执行滑点修复未落地)。
  - **不动赢家**:factor_portfolio(3,3,7 天 **+62**,全场最佳)、funding_cross_section(+2)、funding_carry(+9)。也修正了之前"factor 在亏/funding_xs 该减配"的误判 —— 那些是转换日噪声 / funding 收入未计。reversible。

### Fixed (daily_report 漏报 factor_portfolio, 2026-06-04)

- **`fix(daily_report)`** — 把策略枚举从 `list_strategies()`(只扫 `trades` ∪ `equities`)扩到也纳入 `trades_okx` 的 strategy_id。
  - 根因:`factor_portfolio` 从不调 `record_strategy_equity_daily`(其他策略都调),所以不在 `equities`/`trades` 表里 → 不在 `list_strategies()` → **自上线(05-19)起每一份日报都静默漏掉它**。后果:日报 TOTAL 一直系统性低估账户亏损(把 factor 这个较大的费拖累户整个排除),与权益实际跌幅对不上。
  - 修:新增 `PnLTracker.list_okx_strategies()`(trades_okx 里去重的 strategy_id);`daily_report.build_report` 把它们也分桶。任何"有成交但没写 equity"的策略(含将来新策略)不再被漏。
  - 实测:factor 06-03 有 13 个平仓回合却不在报告里;修复后纳入。回归测试(只 seed trades_okx 的策略必须出现)。全套 1022 passed。

### Changed (factor_portfolio 减腿 paper A/B, 2026-06-02)

- **`config(factor_portfolio)`** — `top_k_long/short` 5,5 → **3,3**(paper 前向 A/B)。3 窗回测(1000/1500/2000 bars,靠新 `--reuse-data` 同窗跑成):减腿性能 ≈中性(3,3 赢 2 输 1,pnl 差 ~0.2% / sharpe 差 ~0.4,在噪声内)但**一致少交易 15-20%** → 省手续费。回测边际太小不足以单凭回测拍板 → 上 paper 观察 2-4 周前向表现 vs 5,5 历史。reversible(改回 5,5 即可)。

### Fixed (factor_portfolio / funding_xs 回测不成交, 2026-06-01)

- **`fix(backtest)`** — factor_portfolio / funding_cross_section 的 NT 回测**零成交、PnL 恒 0**(degenerate)修复。
  - 根因:`on_bar`(同步回调)用 `loop.create_task(self._rebalance(...))` 派发 async rebalance。NT `BacktestEngine` 同步跑数据流,这些 detached task 要等 run 结束才被事件循环执行 —— 那时 `BacktestExecClient` 已断开,`submit_order` 抛 `ValueError: not connected` → 744→**0** 成交、`pnl_pct=0`/`sharpe=nan`。`scripts/backtest.py` 又因 `.env` 有真实 key 使 `IsolatedMarginService.is_backtest()`(api_key 启发式)误判,掩盖了表层。
  - 修:`OkxStrategyBase.dispatch_rebalance(coro)` —— backtest(`TestClock`)下用 `drive_coro_sync` 把协程同步泵到完成(其 await 在 backtest 都不阻塞:iso set-leverage 走 cross 分支),订单在 `on_bar` 同步上下文、exec client 连接时提交;live(`LiveClock`)仍走 `create_task`(并保 task 引用防 GC)。factor_portfolio / funding_cross_section 的 on_bar 改用它。
  - 验证:同一回测从 0 成交 → **744 成交 / 377 持仓 / nt_sharpe 4.83 / win 57.3%**,`not connected` 归零。Exp 1b A/B(5,5 vs 3,3,短窗)now 可比:减腿反而 pnl/sharpe/win 全降,**不支持"减腿提升"假设**(需更长窗复核)。
  - 顺带:修了 `fceeb2a` 引入的 `test_build_live_context_...` 失败(monkeypatch lambda 漏 `option_ulys` kwarg)。`drive_coro_sync` 单测覆盖完成/挂起两路。全套 1021 passed。
- **`fix(backtest)`** — factor_portfolio 回测真正支持 `--reuse-data`:`cmd_backtest_portfolio` 加 `reuse_data` 跳过全部网络下载(bars + warmup panel + panel pre-warm),直接读已有 catalog。解决"窗口随 `time.time()` 漂移 → 第二轮重跑触发 catalog 非-disjoint 写冲突 + 多标的长窗下载太慢/太脆"。验证:download 一次后 `--reuse-data` 重跑 **1.3s 完成、0 网络调用、结果完全一致**。这让"下载一次 → 跑多组 top_k 的稳定 A/B"成为可能。
  - 已知遗留(非本次):CLI `--top-n/--bot-n` 未接 factor_portfolio(它读 yaml 的 top_k);equity_* 指标短窗年化失真(用 nt_* 为准)。

### Fixed (option_vol_selling delta-hedge 上线前 bug, 2026-06-01)

- **`fix(option_vol_selling)`** — delta re-hedge 阈值 size-aware 化(上线前 hardening;该策略仍 `enabled:false`,零实盘影响)。
  - 根因:`abs(net_delta) > delta_hedge_threshold × strike / 10000` —— `0.05` 算成 **0.35 BTC**(@70k,且随价格越涨越松,方向反了),而 `$1000/腿` ATM 跨式的 net_delta 物理上最多漂移 ~**0.014 BTC**(≈`1000/spot`)→ re-hedge **永不触发**,入场对冲一次后裸奔短 gamma 直到 5% spot 止损 —— 对 short-gamma 策略等于核心风控静默失效。即便去掉 strike-scaling,常数 `0.05 BTC` 仍是漂移上限 3.5×。
  - 修:抽纯函数 `needs_rehedge(net_delta, spot, threshold_frac, leg_notional)` —— net delta 的 USD 价值 > 单腿名义 × `threshold_frac` 才再平衡,与 strike/size 无关。`delta_hedge_threshold` 默认 0.05→**0.2**(漂移达单腿名义 20% ≈ $200 触发)。5 个单测覆盖(含"0.014 BTC 现在正确触发"回归)。
  - 上线前剩余项:补 isolated margin + paper gate 验证 re-hedge 真触发 / 期权 fill 率(见 go/no-go)。

### Changed (liq_reversal 止血 + 修复 plan, 2026-05-31)

- **`fix(liq_reversal)`** — `risk_pct` 0.5% → **0.25%** 临时止血。复盘(4 天 12 round-trips)发现执行滑点把名义 3R 压成实测 ~1.2R(TP 市价回撤只吃 53%、SL 穿价 134%)+ 逆势 falling-knife 偏向(13 LONG vs 3 SHORT,入场价单边下行),结构性 -2.4 USDT/笔(4 天 -34)。减半把出血砍半,保留 paper 观察。
- **`docs`** — 正经修复实施计划 `docs/superpowers/plans/2026-05-31-liq-reversal-execution-fix.md`:Part A 趋势否决过滤(EMA-slope veto 逆势 fade,小改)+ Part B tick 级 emulated bracket 出场(消除 TP 回撤/SL 穿价)。离线无清算数据,验证走 paper A/B;落地后回调 risk_pct。

### Added (trade-rate 熔断 + 全策略复盘, 2026-05-31)

- **`feat(risk)`** — 新增 `TradeRateCheck`(per-strategy 下单频率熔断,risk pipeline 末尾)。滚动 `window_sec` 窗口内放行的 entry 数达 `max_trades` 即 REJECT 后续 entry。opt-in(`enable_trade_rate`,默认关)。
  - 动机:`DrawdownCheck` 看净值跌幅,挡不住"高频小亏 churn"。2026-05-25 stat_arb 三小时刷 4394 单 -$1412(每笔 1.5σ 毛收益被 4 legs taker fee 吃光),整日 -1.7% 未触发 3% daily drawdown。策略层 `cooldown_sec_after_exit`(05-27)已是主防御,本 check 是风控层兜底(防 cooldown 回归 / 新策略漏配)。
  - 在 `stat_arb_pairs.yaml` 启用(60 单/小时;正常 ~24 round-trip/天,绝不触发)。`_build_risk_config` 白名单同步加 3 个新键(否则 yaml 配置被静默丢弃)。
- **复盘**(基于权威 `trades_okx` 按新回合口径重算,post-fix 4 天):发现并定性三件事 ——
  - **−51k DOT 清算(05-25)**:`bill_type=5` 清算事件,即已知"DOT 事故",已被 isolated-margin + 单腿 wick 三层防御(05-26)覆盖;`strategy_id` NULL 故在 per-strategy 日报里不可见。05-26 后无新清算。
  - **stat_arb churn(05-25)**:已被 cooldown 修复,现 ~24 round-trip/天 PF 1.46。
  - **遗留待优化**:liq_reversal 执行滑点(3R 实测压成 ~1.2R)、ob_imbalance/funding_cross_section/factor_portfolio 手续费拖累(ob_imbalance 毛 alpha PF 2.12 被开仓费吃成净 -1.3)。需回测验证后再改。

### Fixed (daily_report win_rate / trade_count 口径, 2026-05-29)

- **`fix(daily_report)`** — `trade_count` / `win_rate` 改为按"已平仓回合"统计，不再把开仓单当成交。
  - 根因：`_fetch_trades_okx` 按 `cl_ord_id` 分组，每个 OKX *订单* 算一笔 trade。但开仓单（subType 3/4）`pnl=0` 只有负 `fee`、且与平仓单（5/6）是不同 `cl_ord_id` → 每个开仓单都被算成一笔"亏损 trade"，把持仓过夜策略（xs_momentum 日内 rebalance）的 `win_rate` 结构性压到 0、`trade_count` 翻倍。
  - 修：新增 `PnLTracker.get_round_trips()`，只返回 `SUM(pnl) != 0` 的回合（`_fetch_trades_okx` 加 `realized_only` 参数）；`daily_report` 的 `trade_count` / `win_rate` 改用它，胜负按 net(`pnl+fee`)> 0 判定。`pnl_usdt` 总额仍用 `get_trades`（保留开仓手续费这笔真实成本，数值不变）。
  - 实测 05-28 prod ledger：ob_imbalance 真实胜率 41.7% → **83.3%**（之前被开仓费单稀释）；xs_momentum "479 笔 0%" → "0 个平仓回合"；liq_reversal 确认为 4 个平仓全亏的真实亏损 -21.5。
  - 纯报表/可观测性修复，不碰下单逻辑（`feed_risk_handles` → Kelly 在 live 运行时本就未接线，仓位 sizing 不受影响）。回归测试 + 全套 1008 passed。

### Rolled out (Phase 1c — opt-in all active strategies, 2026-05-26 final)

- **`feat(strategies)`** ([82aafe8](https://github.com/DrZhangXD/okx-trade/commit/82aafe8) merge) — 6 个活跃策略全部接入 `OkxStrategyBase` + `submit_isolated_order` / `batch_ensure_leverage`：
  - `funding_carry` (lever=5, perp 腿)：spot 腿 cash 路径不变；perp 走 isolated + pre-validate
  - `funding_skew_momentum` (lever=5)：单腿 SWAP
  - `xs_momentum` (lever=3)：单 funnel `_submit_delta` → isolated（含 reduce-only，Option B）
  - `stat_arb_pairs` (lever=3)：两腿 `batch_ensure_leverage` 两阶段提交
  - `basis_arb` (lever=3, futures 腿)：与现有 `td_mode_override` 共存；spot 腿 cash 不变
  - `factor_portfolio` (lever=3)：单腿 `_open_leg` async
- **`config`** ([05b2838](https://github.com/DrZhangXD/okx-trade/commit/05b2838)) — 6 个 strategy yaml 翻 `enable_isolated_margin: true`。明确不接入的：`liq_reversal` / `ob_imbalance`（wick = alpha / 亚秒延迟敏感）+ `ml_fusion`（xgboost 未装）+ `range_breakout`（retired）。
- Phase 1 全部就位：当任意接入策略下次 open 位时 lazily 调 `set-leverage` + 加 `td_mode:isolated` tag。FundingXS 自 Phase 1b 起已在跑此路径。

### Added / Refactored (Phase 1 — shared isolated-margin services, 2026-05-26 later)

- **`feat(risk)`** ([f738830](https://github.com/DrZhangXD/okx-trade/commit/f738830) merge) — 抽 FundingXS 的 isolated margin + outlier guard 成共享 service，让其他 9 策略可 opt-in。
  - `IsolatedMarginService`（单例）：posMode cache + (inst, posSide)→lever cache + 共享 OKXRestClient。`ensure_leverage` (idempotent) + `batch_ensure_leverage` (多腿两阶段 commit) + `is_backtest()`。
  - `VolatilityFilter`（单例）：per-inst 1m close deque + `allow(inst_id)` 包 `outlier_check` 纯函数。
  - `OkxStrategyBase` thin Strategy base，提供 `submit_isolated_order(order, lever, pos_side)` 7-分支 helper + `vol_filter_allow`。
  - DI in `build_live_context` + LiveMonitor 持服务引用（同 `account_drawdown_tracker` 路径）。
  - 新 `volatility_filter:` top-level block in `configs/live.yaml`（全局，不再每策略）。
- **`refactor(funding_xs)`** — FundingXS 迁完，从 inline state 改为消费 service。`_set_lever_cache` / `_get_account_pos_mode` / `_set_leverage_cached` / `_closes_1m_by_inst` / `_is_backtest_context` 全删。基类换 `OkxStrategyBase`。`_execute_diff` 用 `batch_ensure_leverage`，`_open_leg` 用 `ensure_leverage`。行为等价。
- Phase 1c-1f（其他 9 策略 opt-in）是后续 PR，每个翻 `enable_isolated_margin: true` yaml flag + 改基类。
- Spec: `docs/superpowers/specs/2026-05-26-isolated-margin-services-design.md`；Plan: `docs/superpowers/plans/2026-05-26-isolated-margin-services.md`（21 task）。
- 单测从 949 → **~985**（+~36 新测：service + filter + base 各分支）。

### Fixed / Added (Defense hardening, 2026-05-26)

- **`fix(adapter)`** ([3078d00](https://github.com/DrZhangXD/okx-trade/commit/3078d00)) — `generate_position_status_reports` 按 inst_id 字符串推 `instType` 调 OKX `/account/positions`，修 7 个月 latent bug `51015 Instrument ID doesn't match instrument type`。SWAP / FUTURES / OPTION 各按对应 instType 查，SPOT 直接跳过（positions 端点本就不返 SPOT）。同时把 NT reconcile 看不到真实持仓 → XSMomentum 等策略 reduce-only 拒单 cascade（`sCode 51169`）的源头切断。每次重启从 4-5 个 `positions_failed` warning 变 0。
- **`fix(funding_xs)`** ([910d6e9](https://github.com/DrZhangXD/okx-trade/commit/910d6e9)) — `_set_leverage_cached` / `_fetch_basis` 剥 NT InstrumentId 的 `.OKX` venue 后缀再调 REST。修 P-Task 16 验证时发现的 `51001 Instrument ID doesn't exist`。
- **`feat(funding_xs)`** ([79796f2](https://github.com/DrZhangXD/okx-trade/commit/79796f2) merge of Plan 7) — 三层防御 against single-leg wick (2026-05-25 DOT 事故 -$51,128 → 单腿损失上限 ~0.5-5% 账户)。
  - Layer 1: 每腿 isolated margin (`tags=["td_mode:isolated"]` + OKX `set-leverage` per leg)
  - Layer 2: 动态 leverage `clip(2 + 3 × |funding_z + basis_z|/2, 2, 10)`
  - Layer 3: outlier guard，1m bar 独立订阅，近 1h vol > 24h baseline × 3 时跳腿
  - `_execute_diff` 两阶段提交 — 任一 `set_leverage` 失败 abort 整轮 open-phase，杜绝单向 residual
  - 配 OKX demo `posMode=long_short_mode` 真实状态（不是 spec 假设的 net mode）
  - 三个独立 `enable_*` config 开关 + 完整 rollback runbook（`docs/operations.md` §五·B）
  - 全套 design / plan / addendum 文档 + 30+ TDD 单测
- **`fix(equity-publisher)`** ([0f46278](https://github.com/DrZhangXD/okx-trade/commit/0f46278)) — strategy `_feed_risk_data` 不再读 NT USDT 单币 `balance_total(USDT)`（事故时账户真实 totalEq $30,518 但 USDT-only 是 $377，写到 equities 表导致 dashboard 误报 99% 回撤）。改读 LiveMonitor 注入的 `_account_total_equity_usdt` cache（账户多币种 totalEq）。13 个 strategy 文件共改。
- **`fix(reconcile)`** ([771c02a](https://github.com/DrZhangXD/okx-trade/commit/771c02a)) — `reconcile_pnl_from_okx.py` 按日分块拉 bills，绕过 OKX 单调用 20k 上限。高频日（stat_arb_pairs 单日 19,624 fill）下 7 天窗口不再丢早段。新增 `--chunk-hours` flag + hit-cap WARN。
- **`fix(rest)`** ([ca2a502](https://github.com/DrZhangXD/okx-trade/commit/ca2a502)) — `account.set_leverage` 在 `mgnMode=ISOLATED` + caller 没传 `pos_side` 时自动补 `PosSide.NET`，避免 OKX `51000 Parameter posSide error`。
- 单测从 803 → **949**（+146 新测，全绿）。

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
