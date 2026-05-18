# Strategy Roadmap

记录现有策略状态 + 中长期策略规划。每条策略的 alpha 论据、所需数据、预期年化、
工程量评估都列在这里，避免临时拍脑袋决定开发优先级。

最近更新：2026-05-18（M5.X 策略优化批次）。

---

## 当前已上 (M5)

| 策略 | 类型 | 频率 | 状态 | 备注 |
|---|---|---|---|---|
| [`FundingCarryStrategy`](../src/okx_trade/strategies/funding_carry.py) | neutral (delta-neutral) | 8h cycle | live | 入场阈值 8% APR 偏低，建议提升到 15%；未建模反向 carry |
| [`XSMomentumStrategy`](../src/okx_trade/strategies/xs_momentum.py) | momentum | 每日 UTC 0 | live | 单 lookback=7d 易在反转月份亏；regime gate 已接入 |
| [`LiqReversalStrategy`](../src/okx_trade/strategies/liq_reversal.py) | reversal | event-driven | live | 阈值 3.5σ 经多次微调，建议改成动态分位；regime gate 已接入 |
| [`BasisArbStrategy`](../src/okx_trade/strategies/basis_arb.py) | neutral (delta-neutral) | 1h check | **M5 新增** | 默认 enabled=false，需要 paper 验证 FUTURES inst_id 解析 + 两腿 fill 时差 |
| [`OBImbalanceStrategy`](../src/okx_trade/strategies/ob_imbalance.py) | reversal | 中频（30s–5min 持仓） | **M5 新增** | 默认 enabled=false；回测困难（OKX 不开 books5 历史），主要靠 paper 验证 |

---

## 已淘汰

- **`RangeBreakoutStrategy`** (M5.X, 2026-05-18 下线)。原因：
  - crypto 区间假突破历史胜率 < 30%，TP/SL=2 不足以覆盖；
  - 与 `xs_momentum` 高度同向（都吃趋势），但噪音更大；
  - 实现层 commits 34666fc / a6a3c8b 是事故性 fix（margin leak + pending order 清理），说明
    实现不稳定，难以维护。

---

## 中期 (M6 / 计划 2026-Q3)

### Funding Cross-Section Arbitrage

**思路**：同一时刻不同币种 funding rate 差异巨大（BTC 0.01% vs 小币 0.1%）。
做多最负费率币 + 做空最正费率币，组合 delta ≈ 0（用 β 配比对冲，因为多数 altcoin
对 BTC β≈1）。同时收两边 funding。

- **数据**：OKX universe ~100 个 USDT 永续的 funding rate 实时表 +
  rolling β（30d 简单回归）。
- **执行**：每 8h funding cycle 调一次，挑最负 funding rank top-3 + 最正 funding rank top-3。
- **预期 alpha**：15-25% 年化（结合资金费率分布的扁尾性）。
- **工程量**：3-5 天。复用 `funding_carry` 的两腿下单逻辑 + 新增 universe ranking。

### Funding-Skew Momentum

**思路**：funding 突然冲高（≥ +2σ vs 30d 均值）= 多头过度拥挤 → 短期反向做空 perp；
funding 急速转负 → 反向做多。基于持仓拥挤度而非价格延续。

- **数据论文**：Soska/Chen 2022 在 Coinbase Derivatives 上有实证。
- **数据**：OKX `funding-rate-history` 每个标的 30 天历史（已有 REST 端点）。
- **执行**：按 funding rate z-score 触发，持仓 1-3 天，止盈止损按 ATR。
- **预期 alpha**：8-15% 年化。
- **工程量**：2-3 天。共享 `liq_reversal` 的 z-score / 阈值触发框架。

---

## 长期 (M7+ / 计划 2026-Q4 及之后)

### Option Volatility Selling

**思路**：OKX BTC/ETH 期权 IV 长期高于 realized vol（5-15% 年化溢价）。卖跨式
（put + call 同 strike）+ delta-hedge perp。学术上 vol risk premium 是 crypto
最稳定的 carry source。

- **数据**：OKX 期权链（mark / IV / Greeks），现货 / perp 用于 delta hedge。
- **执行**：每日 / 每周 roll，根据 IV vs RV 决定 strike 与 expiry。Δ-hedge 频率
  与 Γ exposure 挂钩。
- **预期 alpha**：10-20% 年化，但下行 tail risk 大（需 cap notional）。
- **工程量**：10-20 天。Greeks 引擎 + IV surface + risk monitor 全新建。

### Statistical Arbitrage (Cointegration Pairs)

**思路**：BTC-ETH、SOL-AVAX、LDO-RPL 等高度协整 token 对。Engle-Granger 检验
+ OU 过程估计均值回归速度，spread 偏离 > 2σ 入场。

- **数据**：universe pairwise daily returns（已有）。
- **执行**：每日重做 cointegration 检验，spread 偏离时 long-short pair。
- **预期 alpha**：5-12% 年化，但容量受限于流动性差的小币对。
- **工程量**：5-8 天。

### ML Signal Fusion (XGBoost / LightGBM)

**思路**：把所有已有 alpha 信号（momentum / liq z / funding skew / OB imbalance）
作为 features，XGBoost 学习未来 1h / 4h 涨跌方向。

- **风险**：crypto 上 ML 类策略 OOS 衰减极快（半衰期 2-4 周）。需要重型
  walk-forward + 周度重训管线。
- **工程量**：> 20 天。建议**最后再做**——先把规则版 alpha 榨干。

---

## 工程基础设施 todo（不是策略本身但策略层依赖）

按 plan 的"二、回测真实性问题"清单，待完成项：

1. **回测加 slippage + funding model**：当前 `enable_fees=False` 默认 + `PerfectFill`，
   所有现有策略回测的 Sharpe 都虚高。需要：
   - 默认开 `enable_fees=True`（taker 5bp）。
   - 加 simple slippage model（mid ± k×spread）。
   - 注入历史 funding-rate tick 让 `xs_momentum` 长仓被收取 funding。
2. **walk-forward 框架**：`scripts/backtest.py` 加 `--walk-forward` 模式（6m train + 1m test 滚动）。
3. **portfolio rebalance scheduler**：[live.yaml:67](../configs/live.yaml#L67) 写了周一 UTC 0
   rebalance 但**未实接调度**。需要在 monitor.live 主循环里按 wall-clock 触发
   `allocator.allocate(...)` 切到 risk_budget。
