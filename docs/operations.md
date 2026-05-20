# Operations Playbook

okx-trade 在 okx-vps 上 24/7 paper trading。本文档收集**运行时**的 incident 应对、诊断脚本、cron / observation 报告用法。

部署本身（机器选型 / bootstrap / systemd unit / healthcheck）→ [`deploy/README.md`](../deploy/README.md)。
策略状态 + 工程 backlog → [`strategy_roadmap.md`](strategy_roadmap.md)。

---

## 一、健康速查（30 秒判活）

```bash
ssh okx-vps "echo '==SERVICE==' && sudo systemctl is-active okx-trade && sudo systemctl status okx-trade --no-pager | grep -E 'Active|Memory|Tasks|Main PID' && echo '==HEAD==' && sudo -u okxtrade git -C /home/okxtrade/okx-trade log --oneline -1 && echo '==HEARTBEAT==' && now=\$(date +%s%3N) && hb=\$(sudo cat /home/okxtrade/okx-trade/var/heartbeat.ts) && echo \"diff_ms=\$((now-hb))\""
```

正常状态：
- `active`，uptime 至少几分钟
- HEAD == origin/main HEAD
- `diff_ms < 60000`（heartbeat 在最近 60s 内更新）
- Memory < 500 MB

异常 → 走下面的 incident 流程。

---

## 二、Incident 应对流程

### 2.1 服务挂了

```bash
ssh okx-vps "sudo systemctl status okx-trade --no-pager | head -20 && echo '==LAST 50==' && sudo journalctl -u okx-trade -n 50 --no-pager"
```

systemd `Restart=on-failure` 会自动重启；若反复 fail（看 `Active: failed (...) ; Restart=10s`）说明启动期间就崩。常见原因：

| 症状 | 排查 |
|---|---|
| `ImportError` / `ModuleNotFoundError` | `pip install -e ".[strategy]"` 漏装；登 VPS 跑 `.venv/bin/python -c "import okx_trade"` |
| `OKXAPIError ... sCode=50xx` | OKX 凭证过期 / IP 黑名单。`.env` 检查 + `OKX_IS_DEMO` 确认 |
| `OKXNetworkError` | 网络中断；通常自愈，看 retry 是否成功 |
| pre-start `reconcile_okx_positions.py` 卡住 | OKX 慢；`systemctl restart` 即可 |

### 2.2 DD breach 假警报（margin freeze 引起的早期 bug）

**已修**（commit `0c14d03` + `5684763` + `7e7cc73`）：之前 `equity_provider` 读 `USDT.avail_eq` 而不是 `totalEq`，开仓冻保证金时 avail_eq 瞬间下降被误判为权益下跌；strategies 又自己往 tracker 推 NT cached USDT balance 形成双源污染。

**现在的行为**：
- monitor 只推 `totalEq` 到 `AccountDrawdownTracker`（单例）
- per-strategy `DrawdownTracker` 不被喂数据
- account-level breach → 所有策略 kill-switch
- daily_breach 在次日 00:00 UTC 自动恢复；weekly_breach 需手动 `acknowledge_weekly_breach()`

**若再看到 daily/weekly breach**：先看 OKX 账户真的跌了多少：

```bash
.venv/bin/python -c "
import asyncio
from okx_trade import OKXRestClient, OKXSettings
async def main():
    async with OKXRestClient(OKXSettings()) as c:
        b = await c.account.get_balance()
        print(f'totalEq={b.total_eq}  adjEq={b.adj_eq}')
        u = b.get('USDT')
        if u: print(f'USDT eq={u.eq} cashBal={u.cash_bal} availEq={u.avail_eq} upl={u.upl}')
asyncio.run(main())
"
```

如果 totalEq 真跌了 3% 以上 → 找哪个策略产生的亏损：
- 看 `var/pnl.sqlite.trades` 最近 closed PnL
- 看 OKX bills（脚本见 2.4）

如果 totalEq 没动 → bug 复发，请到 `src/okx_trade/runtime/live_node.py:_build_okx_equity_provider` 核对是否被回退到 `avail_eq`。

### 2.3 策略不出单 / 信号不触发

**先排除冷启动**：

| 策略 | 冷启动需求 | warmup 行为 |
|---|---|---|
| `stat_arb_pairs` | 60 天 1H bars（lookback_bars=1440） | on_start REST 拉，5s 完成 |
| `funding_cross_section` | 30 天 1D closes（beta_window_days=30） | on_start REST 拉 |
| `factor_portfolio` | 30 天 1H bars（basis_z_30d / funding_z_30d） | on_start `fetch_panel` 拉 |
| `funding_skew_momentum` | 90 个 funding rate samples | on_start REST 拉 |
| 其他 5 个 | 无 | 即时可用 |

`journalctl -u okx-trade --since "10 minutes ago" | grep -i warmup` 看是否报错。

**再看实际信号**：每个策略都有自己的"决定不触发"的日志，例如：

- `funding_carry funding=+0.0090%/8h apr=+9.83% action=hold pos=True` — APR 没到 8% 阈值
- `funding_skew BTC-USDT-SWAP.OKX: current=+0.01% history_n=90 decision=None` — z-score 没到 ±2σ
- `stat_arb coint: β=0.80 p=0.40 n=1440 tradeable=False` — 未协整
- `XSMomentum rebalance skipped: scored=9 < required=10` — universe 不够

**最后看 risk REJECT**：

```bash
sudo journalctl -u okx-trade --since "1 hour ago" | grep -E "risk REJECT" | head
```

`risk REJECT [account_drawdown]` → kill-switch 触发（见 2.2）
`risk REJECT [drawdown]` → per-strategy DD（Phase 0 后基本不应该看到）
`risk REJECT [kelly]` → Kelly fraction = 0 或 size 太小

### 2.4 账户余额异常下跌但找不到对应成交

跑诊断脚本：

```bash
# 1. OKX 账户流水汇总（窗口可在脚本里改 WINDOW_BEGIN_MS / WINDOW_END_MS）
.venv/bin/python scripts/diag_account_bills.py

# 2. 当前持仓 + 1H candles MTM 对照
.venv/bin/python scripts/diag_mtm_swing.py

# 3. (NEW) 把 OKX bills 写入权威账本 trades_okx + 与策略侧估算 trades 表对比
.venv/bin/python scripts/reconcile_pnl_from_okx.py --days 4
```

`diag_account_bills` 输出 3 块：按 (type, subType) 汇总的 balChg / pnl / fee，top 10 单笔最大 |balChg|，按 instId 净流水。能立即告诉你"那段时间真实现金流动了多少 + 来自什么"。

`diag_mtm_swing` 输出当前所有 open positions + 12 大币 1H 收盘价格变动，让你估算 "MTM swing ≈ Σ position × pct_change" 是否能解释 totalEq 的变化。

`reconcile_pnl_from_okx` 输出每日每策略的"估算 vs 真实"差异表。若发现某策略某天 `divergence_usdt` 远大于 0（如 +7094），说明该策略当天有大量 phantom record（订单失败 / 部分成交 / rate-limit drop，但 trades 表照样写）。详见 §4。

历史教训：5/20 这三个脚本帮我定位到 "5/18 OBImbalance 死亡螺旋" 的实情——
- 策略 trades 表估算 5/18 赚 +6340 USDT (485 笔)
- OKX bills 真实显示 5/18 ob_imbalance 只成交 1757 笔 fills，balChg = **-754 USDT**
- 差距 +7094 USDT 全是 phantom（NT submit_order 后没 fill 但 record 已写）
- 而真正灾难日是 5/19，账户单日 balChg = **-12,205 USDT**（basis_arb -$7,297 + funding_carry -$4,826 为主），当时被 equity_provider 读 avail_eq + DD tracker 双源污染掩盖，**没触发任何 critical alert**。

### 2.5 weekly_breach 卡死需手动 ack

`AccountDrawdownTracker` 进入 WEEKLY_BREACH 后不自动恢复。两种解锁方式：

**方法 1：人工评估后 ack（推荐）**

```bash
# SSH 进 VPS 后用 sudo systemctl restart okx-trade
# 重启会清掉 in-memory tracker state，下一次 alloc_refresh 重置 week_open=当前 totalEq
sudo systemctl restart okx-trade
```

**方法 2：写一次性脚本**（极端情况；目前无现成）调 `LiveMonitor._account_drawdown_tracker.acknowledge_weekly_breach()`。

> 重启前请确认：触发 weekly_breach 是真亏损还是 bug。如果真亏了 8%，应该先停下来人工 review。

### 2.6 PnL 数据可信度（trades 表 vs trades_okx 表）

**核心事实**：`pnl.sqlite.trades` 是策略侧估算（不等 OrderFilled 就写，用 bar.close 估算），不可信。`pnl.sqlite.trades_okx` 是从 OKX bills 同步的权威账本，可信。

| 表 | 写入方 | 时机 | 价格源 | 何时用 |
|---|---|---|---|---|
| `trades` | 策略 `record_strategy_trade(...)` | `submit_order` 后立即 | bar.close（信号时） | 仅 backtest / 实时近似展示 |
| `trades_okx` | `scripts/reconcile_pnl_from_okx.py` | 每日 cron / 手动 | OKX 实际成交 balChg | Kelly handoff / Sharpe / 真实 PnL |

`PnLTracker.get_trades(strategy_id, authoritative=True)` 是默认行为，会优先返回 `trades_okx` 行（按 cl_ord_id 聚合），有数据就 shadow 掉 `trades` 表的 phantom 行。

**已经在 `trades` 表里的 phantom 行不删除**——保留作为 incident 历史 + 对比基准。reconciliation cron 跑过后，权威数据出现在 `trades_okx`，Kelly handoff / Sharpe / 报告会自动用权威数据，phantom 行被静默 shadow。

### 2.7 数据流断（WS disconnect）

```
ws_disconnected attempt=2 backoff_sec=2.0 error=ConnectionClosedError
ws_connected url=wss://wspap.okx.com:8443/ws/v5/public
ws_resubscribed count=1
```

短断（< 30s）正常，背压 + 自动重连。**长断** → 检查 OKX 状态页 / 本地网络。

---

## 三、Cron / Observation 报告

VPS 上的 `sudo crontab` 维护：

```cron
# 每天 10:00 / 12:00 CST 跑 stat_arb 观察报告
0 10 * * *  /home/okxtrade/okx-trade/scripts/stat_arb_observe.sh stat_arb_24h    > /dev/null 2>&1
0 12 * * *  /home/okxtrade/okx-trade/scripts/stat_arb_observe.sh stat_arb_lunch  > /dev/null 2>&1

# 一次性：2026-05-22 23:30 CST 跑 day_14 完整观察报告
30 23 22 5 *  /home/okxtrade/okx-trade/scripts/observation_report.sh day_14
```

所有报告写到 `var/observation_reports/<name>_<YYYYMMDD>_<HHMM>.md`，远程拉：

```bash
ls /home/okxtrade/okx-trade/var/observation_reports/   # 列文件
sudo cat var/observation_reports/stat_arb_24h_20260520_1000.md
```

### 各 observation 类型

| 类型 | 何时跑 | 内容 |
|---|---|---|
| `adhoc` | 手动 | 7 节：服务状态 + alerts + trades + drawdown + pnl by-strategy + sample logs + healthcheck |
| `day_7` | 5/15 一次 | 同 adhoc，命名带 "day_7" 便于归档 |
| `day_14` | 5/22 一次 | 同 adhoc + 加 14 天回顾区间 PnL/胜率 |
| `stat_arb_24h` | 每日 10:00 cron | stat_arb 专项：协整时序、信号触发、orders、错误、RUNNING 校验 |
| `stat_arb_lunch` | 每日 12:00 cron | 同 24h 但便于看午间状态 |

### 临时跑一份 adhoc

```bash
ssh okx-vps "/home/okxtrade/okx-trade/scripts/observation_report.sh adhoc | xargs cat"
```

---

## 四、三端同步部署

按 [`memory/feedback_deploy_scope.md`](../memory/feedback_deploy_scope.md) 约定，"三端同步 / deploy / 推上去" 是预授权的一整套：

```bash
# 1) 本地确认 + commit
git status && git diff
git add -A && git commit -m "..."

# 2) push
git push origin main

# 3) VPS 拉 + 重启（在一个 SSH 调用里完成）
ssh okx-vps "sudo -u okxtrade git -C /home/okxtrade/okx-trade pull --ff-only origin main 2>&1 | tail -3 && sudo systemctl restart okx-trade && sleep 5 && sudo systemctl is-active okx-trade"

# 4) 等 60-90s 后验证 alloc_refresh + 关键策略已 warm
ssh okx-vps "sleep 70 && sudo tail -5 /home/okxtrade/okx-trade/var/alerts.jsonl && sudo journalctl -u okx-trade --since '90 seconds ago' --no-pager | grep -iE 'warmup|coint|alloc_refresh' | head -10"
```

仅在 deploy 过程中**发现新 bug 需要扩展原始 scope** 时，要先告知人类等批准（不能自行扩展+部署）。

---

## 五、紧急停手

```bash
# 停服务
sudo systemctl stop okx-trade
sudo systemctl stop okx-trade-healthcheck.timer

# OKX demo 账户里手动撤所有未成交单 + 平仓（如有）
# UI: https://www.okx.com/account/positions
```

`paper_trading: true` 模式下不动真钱，但若怀疑代码 bug 立刻停可以避免 paper PnL 失真。

---

## 六、实盘切换前 checklist

1. ✅ paper trading 至少 7-14 天
2. ✅ 看 `var/daily_reports/*.json` 验证 PnL / 胜率符合预期
3. ✅ 看 `var/alerts.jsonl` 是否有 CRITICAL（drawdown 触发）
4. ✅ 14 天 cum PnL 为正，最大 daily drawdown < 3%
5. ✅ AccountDrawdownTracker 没出现过假 breach
6. ✅ 至少跑过 1 次完整周期（funding 8h × 3, daily UTC reset, factor_portfolio 4h × 6+）
7. ⏳ Phase 1 DD per-strategy 隔离做完（推荐 — 单策略爆掉不影响其他）
8. ⏳ Telegram alert 接入（CRITICAL 立刻能收到）
9. 配置改：`account.paper_trading: false` + `OKX_IS_DEMO=false` + **资金减半**上线
10. 头 1 周每天人工对账 OKX bills vs `var/pnl.sqlite.trades`
