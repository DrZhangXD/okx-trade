# Strategy Roadmap

记录现有策略状态 + 历史演进。每条策略的 alpha 论据、所需数据、预期年化、
工程量评估都列在这里。

最近更新：2026-05-18（M6+ 全部中长期策略一次性交付）。

---

## 当前已上 (10 策略)

| 策略 | 类型 | 频率 | 上线批次 | 默认 enabled | 备注 |
|---|---|---|---|---|---|
| [`FundingCarryStrategy`](../src/okx_trade/strategies/funding_carry.py) | neutral | 8h cycle | M2 | ✅ true | 入场阈值 8% APR 偏低，建议提升到 15% |
| [`XSMomentumStrategy`](../src/okx_trade/strategies/xs_momentum.py) | momentum | 每日 UTC 0 | M4 | ✅ true | regime gate 已接入 |
| [`LiqReversalStrategy`](../src/okx_trade/strategies/liq_reversal.py) | reversal | event-driven | M4 | ✅ true | regime gate 已接入 |
| [`BasisArbStrategy`](../src/okx_trade/strategies/basis_arb.py) | neutral | 1h check | M5.X | ✅ true | 季度合约滚动需手动改 yaml |
| [`OBImbalanceStrategy`](../src/okx_trade/strategies/ob_imbalance.py) | reversal | 中频 30s–5min | M5.X | ✅ true | books5 WS 订阅 |
| [`FundingXSStrategy`](../src/okx_trade/strategies/funding_cross_section.py) | neutral β-hedged | 8h cycle | **M6+** | ✅ true | 多空 funding 横截面 + β hedge (2026-05-19 启用) |
| [`FundingSkewStrategy`](../src/okx_trade/strategies/funding_skew_momentum.py) | reversal | ~30min poll | **M6+** | ✅ true | funding ±2σ 反向 (2026-05-19 启用) |
| [`StatArbStrategy`](../src/okx_trade/strategies/stat_arb_pairs.py) | mean-reverting | 1H bar | **M6+** | ✅ true | BTC-ETH 协整套利。2026-05-20 加 REST warmup (lookback_bars=1440 即时填齐) |
| [`OptionVolStrategy`](../src/okx_trade/strategies/option_vol_selling.py) | vol carry | 1h check | **M6+** | ❌ false | BTC short straddle + delta hedge。启用前需 live_node 动态注入 `option_ulys=["BTC-USD"]` filter |
| [`MLFusionStrategy`](../src/okx_trade/strategies/ml_fusion.py) | meta | 每 4h | **M6+** | ❌ false | XGBoost 多空均匀腿。启用前需 `pip install xgboost` + 写 retrain 脚本 |
| [`FactorPortfolioStrategy`](../src/okx_trade/strategies/factor_portfolio.py) | meta | bar-driven (4h default) | **P1** | ✅ true | Generic factor synthesizer; reads configs/factor_portfolio.yaml populated by research lab (2026-05-19 启用) |

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

## 已淘汰

- **`RangeBreakoutStrategy`** (M5.X, 2026-05-18 下线)。原因：
  - crypto 区间假突破历史胜率 < 30%，TP/SL=2 不足以覆盖；
  - 与 `xs_momentum` 高度同向（都吃趋势），但噪音更大；
  - 实现层 commits 34666fc / a6a3c8b 是事故性 fix（margin leak + pending order 清理），说明实现不稳定。

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
   只停那一个，其他正常跑（5/18 ob_imbalance 死亡螺旋场景的最终防线）。
