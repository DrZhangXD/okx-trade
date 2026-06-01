# option_vol_selling 上线前 Go/No-Go 清单

**状态(2026-06-01):** `enabled: false`,0 成交。`fceeb2a` 已自动注入 option_ulys。
**结论:暂不可 flip `enabled:true`** —— 有 1 个已修、2 个未做的关键项。

策略本质:卖 ATM 跨式(short call + short put)+ perp delta 对冲 → **short gamma/vega**,
尾部风险大,核心风控是"持续 delta 中性"。

---

## 阻断项(必须做完才能上 paper)

- [x] **delta-hedge 阈值 bug** —— ✅ 已修(`64b81cd`)。旧 `×strike/10000` 使 re-hedge 永不触发(0.35 BTC vs 物理上限 0.014 BTC);改为 size-aware `needs_rehedge`(USD 价值 > 单腿名义 × 0.2)。

- [ ] **isolated margin(需代码,非 config)** —— `OptionVolStrategy(Strategy)` 不在 `OkxStrategyBase`,加 `enable_isolated_margin` 是 no-op(只有 9 个 OkxStrategyBase 策略读它,见 [live_node.py:357](../../../src/okx_trade/runtime/live_node.py#L357))。short-gamma 单次爆仓会波及 cross 全账户。**选项**:
  - (a) 迁移 OptionVolStrategy 到 OkxStrategyBase(大改);或
  - (b) 给 option + perp 腿下单时加 `tags=["td_mode:isolated"]` + 预设 set-leverage(中改);或
  - (c) 接受 cross,但把 `max_notional_per_leg_usdt` 调小 + 收紧 drawdown,靠账户级 kill-switch 兜底(临时,paper 期可接受)。
  - **建议**:paper 期先走 (c) + 调小 notional;实盘前做 (b)。

- [ ] **paper gate:验证 re-hedge 真触发** —— 上 paper 后,确认 `_manage_position` 的 `needs_rehedge` 在 spot 移动时确实触发 perp re-hedge(看 journal `HEDGE perp ...` 日志)。这是验证 bug 修复在真实数据下生效的唯一手段(离线无法)。

## 验证项(paper 1-2 周观察)

- [ ] **OKX 期权 fill 行为** —— IOC 单 + ATM 薄流动性,确认成交率;`max_notional_per_leg_usdt: 1000` 是否被实际限制。
- [ ] **IV/RV 入场信号** —— `iv_rv_ratio_min: 1.20` 是否真有触发(不要太严永不进场)。
- [ ] **perp 对冲 fill 可靠性** —— delta 中性依赖 perp 及时成交;看 re-hedge 单成交率。
- [ ] **5% spot 硬止损 + 到期前 1 天平仓** —— 确认这两个退出路径在 paper 真触发。

## 风控补强(建议,非阻断)

- [ ] 加 `enable_trade_rate`(防 re-hedge 抖动 churn perp)。
- [ ] 加逐仓最大亏损(目前只有 5% spot 止损 + 账户级 3%/8% drawdown;short-gamma 应有 position-level 止损)。
- [ ] entry 时 log gamma/vega,gamma 过大时拒入/降 size(目前只 hedge delta,不看 gamma/vega 敞口)。

## Go/No-Go 判定

**No-Go(当前)** → 至少完成上面 3 个阻断项的 (c) 临时方案 + 上 paper 验证 re-hedge 触发,
再观察 1-2 周 fill / 信号,**全绿才 flip `enabled:true`**。实盘前补 isolated margin (b)。
