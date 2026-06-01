# 主题 B:手续费拖累优化实验 plan

**目标:** 把"毛 alpha 为正但被 taker fee 吃成净负"的策略救回正收益,不误伤 alpha。
**纪律:** 全部走 **paper A/B 或回测验证**,每个实验独立 config flag / 可回滚,验证达标才保留。

## Ledger 拆解(since 05-27 UTC,权威 trades_okx)

| 策略 | 订单 | 毛 alpha | 手续费 | 净 | 延迟敏感 | maker 可行 |
|---|---|---|---|---|---|---|
| ob_imbalance | 236 | **+40.3** | -46.4 | -6.1 | 高(亚秒) | ❌ |
| factor_portfolio | 241 | **+9.4** | -18.7 | -9.3 | 低(4h bar) | ✅ |
| funding_cross_section | 98 | -1.5 | -14.7 | -16.2 | 低 | ✅ 但见下 |

所有入场/出场都是 **market IOC(taker)**,~5bps/腿/边。

---

## 实验优先级

### Exp 1 — factor_portfolio 降频/减腿(最高 ROI,低风险)
低延迟、4h bar → 改动安全。两个 config-only 旋钮(择一或都试):
- **1a:** `rebalance_hours` 4 → 6([factor_portfolio config])→ ~33% 少手续费,持仓更久吃更多 factor alpha。风险:短周期 momentum factor 可能 staler。
- **1b:** `top_k_long/short` (5,5) → (3,3) → ~40% 少腿/费,只留最高信号腿,alpha/$ 反升。
- **验证:** 回测 30d 比 Sharpe + 净 PnL;A/B paper 5-7 天。**通过标准:** 净 PnL 转正或 Sharpe 不降。

### Exp 2 — ob_imbalance 少交易换 fee(中风险,需回测)
延迟敏感 → maker **不可行**(会丢 fill 杀 alpha)。只能提高信号门槛少交易:
- **2a(最安全):** `reentry_cooldown_sec` 60 → 120 → 减少 whipsaw 重入,~10-15% 少费。
- **2b:** `imbalance_threshold` 0.55 → 0.65 + `microprice_premium_bps` 5 → 6.5 → ~15-20% 少费,但牺牲 alpha。
- **关键风险:** ob_imbalance 毛 alpha +40 是真的,过度收紧会把 alpha 一起砍掉得不偿失。**验证必须回测 7d**,对比 `毛alpha − fee` 的净值,只有净改善才上。

### Exp 3 — funding_cross_section 先补归因,再谈优化(阻断:测量缺口)
**不能直接优化** —— 它的真实 alpha 是 **funding 收入**,而 funding bills(sub_type 173/174)
**全部 strategy=NULL,无法归因到策略**(account-wide 净 funding 才 +1.35)。所以现在
无法判断它到底亏不亏;-14.7 的 fee 是真的,但 funding 收入未知。
- **3a(前置):** 改 `scripts/reconcile_pnl_from_okx.py`,把 funding bills 按持仓 inst 归因到策略(或至少按 inst 聚合),让 funding 收入进入 per-strategy 视图。
- **3b(归因后):** 若 funding 收入 < fee → 减腿 (3,3)→(2,2) 或改 post-only maker(低延迟可行,~70% 省费,需 2-rebalance paper 测 fill 率 ≥85%)。

---

## 不做什么(避免误伤)
- **不对 ob_imbalance 上 maker/post-only** —— 亚秒 alpha,限价单会丢 fill。
- **不盲目全局降频** —— ob_imbalance 的高频是 alpha 来源,只在"净值改善被回测确认"后才收紧。

## 执行顺序建议
1. **Exp 1b**(factor 减腿,最安全)先回测 → 若过则 paper A/B → 部署(需 user 确认)。
2. **Exp 3a**(funding 归因)—— 独立的 reconcile 改进,顺手补上测量缺口。
3. **Exp 2a**(ob cooldown)回测后再定。
