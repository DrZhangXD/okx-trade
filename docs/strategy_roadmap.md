# Strategy Roadmap

记录现有策略状态 + 历史演进。每条策略的 alpha 论据、所需数据、预期年化、
工程量评估都列在这里。

最近更新：2026-05-18（M6+ 全部中长期策略一次性交付）。

---

## 当前已上 (10 策略)

| 策略 | 类型 | 频率 | 上线批次 | 默认 enabled | 备注 |
|---|---|---|---|---|---|
| [`FundingCarryStrategy`](../src/okx_trade/strategies/funding_carry.py) | neutral | 8h cycle | M2 | ✅ true | 2026-05-20 entry 8%→15%（5/19+5/20 实测亏 $-8,953，8% 不够覆盖 4-leg 摩擦）<br>✅ backtestable via `scripts/backtest.py --strategy funding_carry` (2026-05-25, Plan 1) |
| [`XSMomentumStrategy`](../src/okx_trade/strategies/xs_momentum.py) | momentum | 每日 UTC 0 | M4 | ✅ true | regime gate 已接入 |
| [`LiqReversalStrategy`](../src/okx_trade/strategies/liq_reversal.py) | reversal | event-driven | M4 | ✅ true | regime gate 已接入 |
| [`BasisArbStrategy`](../src/okx_trade/strategies/basis_arb.py) | neutral | 1h check | M5.X | ❌ false | 5/19 -$7,297 单日真亏（futures 短腿被强平 hedge 破裂），需修 margin 隔离才能重启（todo #8）<br>✅ backtestable via `scripts/backtest.py --strategy basis_arb` (2026-05-25, Plan 1) |
| [`OBImbalanceStrategy`](../src/okx_trade/strategies/ob_imbalance.py) | reversal | 中频 30s–5min | M5.X | ✅ true | books5 WS 订阅<br>✅ backtestable via scripts/backtest.py --strategy ob_imbalance (2026-05-25, Plan 2; requires capture_orderbook.py to populate catalog first) |
| [`FundingXSStrategy`](../src/okx_trade/strategies/funding_cross_section.py) | neutral β-hedged | 8h cycle | **M6+** | ✅ true | 多空 funding 横截面 + β hedge (2026-05-19 启用)<br>✅ backtestable via `scripts/backtest.py --strategy funding_cross_section` (2026-05-25, Plan 1) |
| [`FundingSkewStrategy`](../src/okx_trade/strategies/funding_skew_momentum.py) | reversal | ~30min poll | **M6+** | ✅ true | funding ±2σ 反向 (2026-05-19 启用)<br>✅ backtestable via `scripts/backtest.py --strategy funding_skew_momentum` (2026-05-25, Plan 1) |
| [`StatArbStrategy`](../src/okx_trade/strategies/stat_arb_pairs.py) | mean-reverting | 1H bar | **M6+** | ✅ true | BTC-ETH 协整套利。2026-05-20 加 REST warmup (lookback_bars=1440 即时填齐) |
| [`OptionVolStrategy`](../src/okx_trade/strategies/option_vol_selling.py) | vol carry | 1h check | **M6+** | ❌ false | BTC short straddle + delta hedge。启用前需 live_node 动态注入 `option_ulys=["BTC-USD"]` filter<br>✅ backtestable via scripts/backtest.py --strategy option_vol_selling (2026-05-25, Plan 3; requires capture_option_summary.py + live needs data.option_ulys yaml) |
| [`MLFusionStrategy`](../src/okx_trade/strategies/ml_fusion.py) | meta | 每 4h | **M6+** | ❌ false | XGBoost 多空均匀腿。启用前需 `pip install xgboost` + 写 retrain 脚本 |
| [`RangeBreakoutStrategy`](../src/okx_trade/strategies/range_breakout.py) | breakout | 1H signal × 1D range | M5.X **重建 2026-05-20** | ❌ false | 5/18 下线（alpha 弱论据来自 phantom 数据），5/20 重建并加上今天的架构修复。enable 后 14 天 paper（**2026-05-22 从 7 天延长**，因低波动期 0 fills）：真实日 PnL > -$50 + 与 xs_momentum 相关 < 0.7 |
| [`FactorPortfolioStrategy`](../src/okx_trade/strategies/factor_portfolio.py) | meta | bar-driven (4h default) | **P1** | ✅ true | Generic factor synthesizer; reads configs/factor_portfolio.yaml populated by research lab (2026-05-19 启用)<br>✅ backtestable via `scripts/backtest.py --strategy factor_portfolio` (2026-05-25, Plan 5; auto-warms panel cache) |

---

## M6+ 启用清单（依次 paper 验证后切 enabled=true）

每个策略上线前的验证步骤：

### `funding_cross_section`
1. paper 跑 ≥ 1 天，确保 universe + β 历史填充完整（β 需 ≥ 30 个 1D 收盘价）
2. log 里看 funding rate batch 拉取无 OKX rate limit 错误
3. 第一次 rebalance 时验证选股 + β-hedge 比例合理
4. 与 funding_carry 同时持仓 BTC 的冲突（监控 monitor alert）

### `funding_skew_momentum`
1. paper 跑 ≥ 1 天，确认 90 个 funding rate 历史 bootstrap 完成
2. 等待 funding z-score > 2σ 的第一次触发，验证 ATR SL/TP 合理
3. hold_max_hours=72h 超时检查正常

### `stat_arb_pairs`
1. `pip install -e ".[stat-arb]"`（启用 statsmodels）
2. paper 跑 ≥ 1 周看 BTC-ETH 协整 p-value 稳定性
3. 验证 spread_z 入场 + mean-reversion 出场

### `option_vol_selling` ⚠️ 最复杂
1. **修改 `live_node._build_trading_node`**：让 instrument_provider 加 `option_ulys=["BTC-USD"]`
   filter，否则启动加载 ~500 个期权超时
2. paper 跑 ≥ 2 周看 OKX 期权 fill / mark IV 数据稳定性
3. 验证 BS pricer 算出的 Greeks 跟 OKX opt-summary 一致
4. 第一次 ATM straddle 入场 + delta hedge 路径完整

### `ml_fusion`
1. `pip install -e ".[ml-fusion]"`（装 xgboost）
2. 写 `scripts/ml_fusion_retrain.py` 跑 walk-forward 训练，模型 pickle 到
   `var/ml_fusion_model.pkl`
3. paper 跑 ≥ 2 周看分类准确率 > 52%（高于随机基准 50%）
4. monitor 加 alert：CV 分数掉到 < 51% 触发 WARN（v1 暂不实现）

---

## Factor Research Lab (P1, 2026-05-19)

新模块 `okx_trade.research`：CLI-driven 因子评估 pipeline + 通用 FactorPortfolio 策略。

- 设计文档: [`docs/superpowers/specs/2026-05-19-factor-research-lab-design.md`](superpowers/specs/2026-05-19-factor-research-lab-design.md)
- 实施计划: [`docs/superpowers/plans/2026-05-19-factor-research-lab.md`](superpowers/plans/2026-05-19-factor-research-lab.md)
- 入口: `python -m okx_trade.research <list|fetch|eval|approve|reject|backtest-portfolio|report>`
- 因子库 (v1, 15 个): momentum × 4, funding/OI × 4, basis × 2, volatility × 3, flow × 2

启用步骤：

1. `python -m okx_trade.research fetch --start 2025-11-01 --end 2026-05-15 --universe top30`
2. `python -m okx_trade.research grade-all --start 2025-11-01 --end 2026-05-15 --universe top30 --horizon 1d`
3. 看 `var/factor_research/reports/*.md` 选 3-5 个 verdict=pass 的因子
4. `python -m okx_trade.research approve --factor <id> --weight <0.1-0.4>` 逐个加
5. `python -m okx_trade.research backtest-portfolio --total-bars 2000` 离线回测合成组合
6. 改 `configs/live.yaml`: `factor_portfolio.enabled: true`，三端同步
7. paper 跑 7 天看与 xs_momentum 相关系数（< 0.7 OK，否则砍权重）

### CLI subcommand cheat sheet

| Cmd | Online? | Args | Notes |
|---|---|---|---|
| `list` | offline | — | 列所有注册的因子 + 最新 grade |
| `fetch` | **online** | `--start --end --universe --bar` | 拉数据落 parquet 缓存 |
| `eval` | **online** | `--factor --start --end --universe --bar --horizon` | 单因子 IC/IR/decay |
| `grade-all` | **online** | `--start --end --universe --bar --horizon` | 跑所有 15 因子 |
| `approve` | offline | `--factor --weight [--force]` | 写 yaml + sqlite |
| `reject` | offline | `--factor` | 从 yaml 移除 |
| `report` | offline | `--factor` | 回放最新 grade 为 markdown |
| `backtest-portfolio` | **online** | `--bar --total-bars --catalog` | NT 回测整个合成组合 |

"online" = 构造 `OKXRestClient(OKXSettings())`，需要 `.env` 里 OKX 凭证（即使是 demo）。
所有 online 子命令首次跑会缓存到 `var/factor_research/panel/*.parquet`，再次跑同参数零网络。

---

## Backtest Capability (2026-05-25, Plan 1 landed)

All 4 funding-aware strategies (`funding_carry`, `funding_cross_section`, `funding_skew_momentum`,
`basis_arb`) can now be backtested end-to-end via `scripts/backtest.py`. The backtest path:

1. `prepare_funding_panel()` downloads and caches historical funding rates as
   `${catalog}/funding/<inst_id>/<YYYYMM>.parquet`.
2. The strategy's `Config` accepts `funding_panel_parquet_path`; `on_start` auto-loads via
   `read_funding_parquet()` and calls `feed_funding_panel()`.
3. NT BacktestNode runs as usual; strategy reads the panel during normal bar callbacks.

Quick smoke:
```bash
python scripts/backtest.py --strategy funding_carry \
    --spot-instrument-id BTC-USDT --perp-instrument-id BTC-USDT-SWAP \
    --signal-bar 1H --total-bars 500 --reuse-data

python scripts/backtest.py --strategy funding_cross_section \
    --instrument-ids BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \
    --signal-bar 1D --total-bars 200 --reuse-data
```

`basis_arb` backtest uses NT's single-MARGIN-account model, which understates the
margin-isolation tail risk seen on real OKX. Plan 6 (separate roadmap) will add
a cross-account simulator for production-grade `basis_arb` backtesting.

### Plan 2: ob_imbalance (2026-05-25)

`ob_imbalance` can be backtested by first capturing live WS books5 snapshots to parquet:

````bash
# Step 1: capture (long-running; one terminal)
python scripts/capture_orderbook.py --inst-id BTC-USDT-SWAP \
    --duration-hours 168 --downsample-sec 1

# Step 2: backtest
python scripts/backtest.py --strategy ob_imbalance \
    --orderbook-instrument-id BTC-USDT-SWAP --signal-bar 1m \
    --total-bars 1440 --reuse-data
````

OKX has no historical orderbook REST endpoint — capture is mandatory before
backtest. Frames are stored at `${catalog}/books5/<inst_id>/<YYYYMMDD>.parquet`
(lz4-compressed). The strategy's `on_start` auto-loads via the
`orderbook_parquet_path` config field, and `on_bar` drains frames through
`process_orderbook()` before invoking normal bar logic.

### Plan 3: option_vol_selling (2026-05-25)

`option_vol_selling` backtest requires capturing option summary snapshots first
(OKX has no historical option REST endpoint):
````bash
# Step 1: capture (long-running)
python scripts/capture_option_summary.py --underlying BTC-USD \
    --interval-sec 60 --duration-hours 168

# Step 2: backtest (also needs perp bars for delta hedge)
python scripts/backtest.py --strategy option_vol_selling \
    --option-underlying BTC-USD --perp-instrument-id BTC-USDT-SWAP \
    --signal-bar 1H --total-bars 500 --reuse-data
````

Plan 3 also added a `data.option_ulys: ["BTC-USD"]` field in `configs/live.yaml`
that restricts OPTION instrument loading at startup (avoids loading ~500
contracts; required for option_vol_selling to start in reasonable time on live).

---

## 已淘汰

（暂无。2026-05-20 RangeBreakout 已重建——见上表。）

### 历史记录：RangeBreakoutStrategy 下线 + 重建

**2026-05-18 下线** (commit 47f0225)。原因：
- crypto 区间假突破历史胜率 < 30%，TP/SL=2 不足以覆盖；
- 与 `xs_momentum` 高度同向（都吃趋势），但噪音更大；
- 实现层 commits 34666fc / a6a3c8b 是事故性 fix (margin leak + pending order 清理)。

**2026-05-20 重审 + 重建**。复盘发现：
- 第 1 条 "alpha 弱" 基于 `pnl.sqlite.trades` 估算数据，**未用 OKX 真实账户验证**（5/20 才发现该表是 phantom）；
- 第 2 条 "与 xs_momentum 同向" 是直觉判断，未实证日 PnL 相关系数；
- 第 3 条 "工程不稳" 是真，但已被 34666fc / a6a3c8b 修复，且今天的架构改进（`on_order_rejected` phantom 清理 / `resolve_pos_side` hedged 模式 / `AccountDrawdownCheck` 单源 / `trades_okx` 权威账本）进一步强化。

**重建动作**: `git checkout 47f0225^ -- src/.../range_breakout.py configs/.../range_breakout.yaml tests/.../test_strategy_range_breakout.py` 还原 3 个文件，
应用今天的 `record_strategy_trade DD push 移除`，重注册 `live_node._strategy_registry`，`live.yaml` 默认 `enabled: false`。

**重启动验证标准**（用户 enable 后 14 天）：
1. truth dashboard 真实日 PnL > -$50/天 → 保持
2. 与 `xs_momentum` 日 PnL 相关系数 < 0.7（用 `pnl/stats.compute_daily_returns` 算）
3. 不再出现 margin leak / pending order 异常（journal 监控）

任一不达标则按数据再次下线（这次会有 OKX bills 真实数据支撑）。

**2026-05-22 窗口延长**：原 7 天窗口（截止 5/27）走完 2 天 0 fills。诊断结论：策略加载正常、equity 分配正常，但当前 BTC 处于 mean_reverting 低波动期，1H 没有出现「收破日 K 区间 → 收回区间内」的连续两根模式。延长到 14 天（截止 **2026-06-03**），给市场更多时间产生触发条件；如仍 0 fills，再考虑放宽阈值或下线。

---

## 工程基础设施 todo

按 plan 的"回测真实性问题"清单，待完成项：

1. **回测 fees / slippage / funding model**：
   - M5.X 已加 `enable_fees` 开关到 `build_okx_venue_config`，但默认 False。建议改默认 True。
   - 注入历史 funding-rate tick 让 `xs_momentum` 长仓被收取 funding（NT SimulatedExchange 不自动 settle funding）。
   - simple slippage model（mid ± k×spread）未实现。
2. **walk-forward 框架**：M6+ 已提供 `okx_trade/backtest/walk_forward.py`（splits + 评估），
   待接入 `scripts/backtest.py` 的 `--walk-forward` 模式（6m train + 1m test 滚动）。
3. **portfolio rebalance scheduler**：`live.yaml.portfolio_optimizer` 写了周一 UTC 0
   rebalance 但**未实接调度**。需要在 monitor.live 主循环里按 wall-clock 触发
   `allocator.allocate(...)` 切到 risk_budget。
4. **OPTION instrument loading**：`option_vol_selling` 启用前 `live_node` 需要根据策略是否
   enabled 动态注入 `option_ulys=["BTC-USD"]` 到 instrument_provider filter；目前需手动改代码。
5. **DD 架构 Phase 1（per-strategy 真隔离）**：2026-05-20 已落 Phase 0（账户级 kill-switch
   `AccountDrawdownTracker` + `AccountDrawdownCheck`，单源单告警）。Phase 1 接 `PnLTracker`
   做按 strategy_id 的 daily realized PnL 喂 per-strategy `DrawdownTracker`，让单一策略爆掉
   只停那一个，其他正常跑（防 high-frequency 策略持续亏损 fee 拖累其他策略）。
6. **pnl_hook 重构走 OrderFilled 事件**：当前各策略 `record_strategy_trade` 在 `submit_order`
   后立即写 record，不等 OrderFilled，用 bar.close 估算价。OBImbalance 5/18 写 485 条
   trade 但 OKX 实际只 31 笔 fills，估算 PnL +6340 vs OKX 真实 balChg -754 USDT
   （详见 [operations.md §4](operations.md) reconcile 步骤）。已临时方案：`scripts/reconcile_pnl_from_okx.py`
   每日把 OKX bills 同步到 `trades_okx` 表，`PnLTracker.get_trades(authoritative=True)`
   默认读权威表。Phase 1 应把策略的 record 路径改为 NT `on_order_filled` event。
7. **ob_imbalance 5/19 修复参数 fine-tune**（需 walk-forward CLI 后做）：
   - `imb_thr 0.35→0.45`：当前过滤掉大量信号（5/18 vs 5/19 笔数 -90%）；可能过严，尝试 0.40 中间值
   - `risk_pct 0.3%→0.2%`：fees 是绝对值不是比例，缩仓不直接帮助；若 alpha 验证为正可回 0.3%
   - SL 30bp / cooldown 60s / reversal-required / fee 扣减：保留，是基础不变式
   触发条件：完成 todo #2 walk-forward CLI 后跑一遍 OOS 对比看 sharpe / hit rate / 净 PnL。
8. **basis_arb margin 隔离修复**（2026-05-20 disable 后阻塞）：
   5/19 流水显示 futures 短腿出现 `sub_type=6`（强制平仓）→ hedge 破裂 → spot 单边裸 long
   → 后续价格反向再亏。根因是 spot 用现金账户 vs futures 用 cross margin，margin 池不
   共享，futures 端 margin 抽干后被独立强平。修复方向：(a) 让两腿都用统一 margin（OKX
   portfolio margin mode）；或 (b) 在策略层独立监控 futures margin level，临界时主动
   reduce 而非等强平；或 (c) 把 spot+futures 改成 perp+futures 双 perp 套利（避免现金/
   保证金跨账户）。
9. **strategy enable 流程纪律恢复**：5/19 一日内同时 enable 4 策略
   (funding_xs/skew/stat_arb/factor_portfolio) 违反了 roadmap 自己定的"每策略 ≥ 1 天
   paper 验证"规则。修复方向：写 `scripts/enable_strategy_with_audit.sh` 强制每次只能
   enable 一个，且要求前一个 enable 至少 7 天且 truth dashboard 显示净 PnL > -1% 账户。
