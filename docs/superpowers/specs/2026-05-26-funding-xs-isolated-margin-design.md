# FundingXSStrategy 三层防御 — 设计文档

**日期**：2026-05-26
**作者**：DrZhangXD + Claude
**触发事故**：2026-05-25 17:40 CST DOT-USDT-SWAP forced liquidation, -$51,128 USDT (demo paper trading account)

## 1. 背景 / 问题陈述

### 1.1 事故链

| 时间 (CST) | 事件 |
|---|---|
| 2026-05-25 16:00 | FundingXSStrategy 资金费窗口 rebalance，开 DOT-USDT-SWAP **738.8 contracts short** @ avg $1.45（两笔 369.4 fill），clOrdId 前缀 `O-20260525-080000-001-006-` |
| 2026-05-25 17:38 (UTC 09:38) | DOT 1m K 线下插针到 $0.381（high $1.520，low $0.381） |
| 2026-05-25 17:39 (UTC 09:39) | 反向爆拉，1m K 线 high=$3.000，巨量 1.6M contracts |
| 2026-05-25 17:40:47 (UTC 09:40) | 1m K 线 high=**$121.985**，OKX 在 $68.975 forced-close 738.8 contracts，**bal_chg = -$51,128.73** |
| 2026-05-25 17:41 (UTC 09:41) | DOT 价格瞬间收敛回 $1.264 |

### 1.2 事故根因（按重要性排序）

1. **Cross-margin + 无单腿损失隔离**：单腿浮亏 $445k 远超账户净值 $50k 但仍被允许浮亏，触发强平时一次性扫掉账户大半。
2. **静态 leverage（默认 10×）**：未与 conviction 挂钩，所有腿不分信号强弱都用同样杠杆。
3. **无市况异常感知**：在某些标的进入异常波动区间时仍机械下单。

### 1.3 现状

- 账户实际 `totalEq = $30,518`（USDT 377 + BTC 0.257 + ETH 1 + OKB 100）。
- 仅 2 个微仓（ADA short 0.31 + SOL long 0.08），mgnRatio 3257（零风险）。
- `account_drawdown_tracker` 重启后已自动 reset，DD gate state = NORMAL。

## 2. 目标 / 非目标

### 目标

- **单腿最大损失硬上限**：通过 isolated margin，让任意单腿 wick 不能消耗超过分配给该腿的 margin。
- **conviction-aware sizing**：信号越强（funding + basis spread 综合 z-score）杠杆越高，但因为 isolated margin = notional/lever，高 conviction 实际占用 margin 更少 → 单腿损失上限更低。
- **入场前异常市况过滤**：当某标的近 1h 波动率 > 24h baseline 的 3× 时，跳过该腿。
- **可回滚**：两个独立 enable 开关（`enable_dynamic_lever`、`enable_outlier_guard`），关掉即回到现有 cross + lever 10× 行为。

### 非目标

- 不主动迁移已有 cross-margin 持仓为 isolated（让自然 roll off）。
- 不改造其他 multi-leg 策略（FundingSkew / XSMomentum 等）；本次只动 FundingXSStrategy。可后续抽公共 handle。
- 不在 NT backtest engine 中模拟 isolated margin（NT MarginAccount 不支持精确 isolated 模拟）；回测保留 cross + `lever_max`。
- 不实现 client-side 主动 stop-loss（被 isolated margin 的 OKX-side auto-liquidation 覆盖）。

## 3. 架构

### 3.1 数据流（FundingXSStrategy._rebalance_async）

```
Step 1: pull funding rates + spot/perp basis (NEW: basis)
Step 2: 排序 → top_n long + bot_n short legs (unchanged)
Step 3: outlier guard ──── (NEW: 拒掉异常 vol leg) ───→ skip
Step 4: compute edge_score = (funding_z + basis_z) / 2 per leg
Step 5: leverage(edge_score) ──── 动态 leverage 公式 ────→ x∈[2,10]
Step 6: per_leg_isolated_margin = notional / leverage
Step 7: OKX set-leverage(inst, mgnMode=isolated, lever=x) (NEW)
Step 8: submit_order(tdMode=isolated) ────────────→ exchange
```

### 3.2 损失边界数学

账户 $30k，`max_position_pct=40%`，6 腿：

| 场景 | per-leg notional | leverage | isolated margin | wick @ 80× 后的损失上限 |
|---|---|---|---|---|
| 低 edge (lever=2×) | $2,000 | 2× | $1,000 | $1,000 (3.3% 账户) |
| 中 edge (lever=5×) | $2,000 | 5× | $400 | $400 (1.3% 账户) |
| 高 edge (lever=10×) | $2,000 | 10× | $200 | $200 (0.7% 账户) |

6 腿同时强平最坏情况：~$1k - $6k（vs. 事故 -$51k）。

## 4. 公式 / 算法

### 4.1 edge_score（per leg）

```
funding_z   = (f_8h_i - mean(f_8h_universe)) / std(f_8h_universe)
basis_z     = (b_i - mean(b_universe)) / std(b_universe)
              where b = (perp_mark - spot_mark) / spot_mark
edge_score  = sign(direction) × (funding_z + basis_z) / 2
              # direction = +1 if leg is short, -1 if leg is long
              # funding 正 → 应 short；basis 正 → 应 short；方向应一致
```

若 `lever_edge_combine_basis=false`：`edge_score = sign(direction) × funding_z`（不掺 basis）。

### 4.2 leverage 映射

```
lever = clip(lever_base + lever_slope × |edge_score|, lever_min, lever_max)
      = clip(2 + 3 × |edge_score|, 2, 10)
```

| `|edge_score|` | leverage |
|---|---|
| 0 | 2× (lever_base) |
| 1.0 (1σ) | 5× |
| 2.0 (2σ) | 8× |
| ≥ 2.67 (~3σ+) | 10× (lever_max) |

### 4.3 outlier guard

```python
def _outlier_check(self, inst_value: str) -> tuple[bool, str]:
    """返回 (allow, reason)。allow=False 时这条腿不开仓。"""
    closes = self._closes_by_inst.get(inst_value, [])
    if len(closes) < self.config.outlier_warmup_min:
        return True, "warmup"
    log_returns = np.diff(np.log(closes))
    recent_vol = np.std(log_returns[-self.config.outlier_window_min:])
    baseline_vol = np.std(log_returns[-self.config.outlier_baseline_min:])
    if baseline_vol <= 0:
        return True, "no_baseline"
    ratio = recent_vol / baseline_vol
    if ratio > self.config.outlier_vol_ratio:
        return False, f"vol_ratio={ratio:.2f}>{self.config.outlier_vol_ratio}"
    return True, "ok"
```

全 top_n+bot_n 都被 outlier guard 拒 → 整轮 rebalance abort + emit WARN alert，下个 funding 窗口（8h 后）再试。

## 5. 实现细节

### 5.1 td_mode 注入（A vs B）

**选 B**：strategy 实例上设 `_default_td_mode = "isolated"`，OKX `ExecClient` 提交单时优先看 strategy attr 覆盖 venue config 默认值。

理由：侵入面小（不改 `RiskIntent` 数据结构），其他策略零影响。

### 5.2 set-leverage 缓存

```python
class FundingCrossSectionStrategy:
    def __init__(...):
        self._set_lever_cache: dict[str, float] = {}  # inst_value → last_set_lever

    async def _set_leverage_cached(self, inst_value: str, lever: float) -> bool:
        last = self._set_lever_cache.get(inst_value)
        if last == lever:
            return True
        try:
            await self._rest.set_leverage(
                inst_id=inst_value, mgn_mode="isolated", lever=str(lever),
            )
            self._set_lever_cache[inst_value] = lever
            return True
        except Exception as exc:
            self.log.warning(f"set_leverage failed inst={inst_value} lever={lever}: {exc}")
            return False  # caller 跳过这条腿
```

### 5.3 配置 schema（configs/live.yaml）

```yaml
strategies:
  funding_cross_section:
    config:
      # === 已有 ===
      max_position_pct: 0.40
      top_n: 3
      bot_n: 3

      # === NEW: dynamic leverage ===
      enable_dynamic_lever: true
      lever_min: 2.0
      lever_max: 10.0
      lever_base: 2.0
      lever_slope: 3.0
      lever_edge_combine_basis: true
      margin_mode: isolated   # live; backtest forced cross

      # === NEW: outlier guard ===
      enable_outlier_guard: true
      outlier_vol_ratio: 3.0
      outlier_window_min: 60
      outlier_baseline_min: 1440
      outlier_warmup_min: 1440
```

### 5.4 backtest 兼容

- NT MarginAccount 不模拟 isolated；`margin_mode=isolated` 在 backtest 上下文里降级为 cross + `lever_max`。
- 检测路径：strategy `__init__` 时看 `self._is_live` 或 venue type；非 live 则 force cross。
- 回测 PnL 因此可能仍显示同样级别的 wick 损失（NT 撮合也会在 demo data 上吃这种事件）；只有 live 才能验证 isolated 损失隔离效应。

### 5.5 Migration

- 不主动迁移现有 ADA short 0.31 / SOL long 0.08。
- 部署后第一轮 rebalance（next 0/8/16 UTC funding 窗口）走新路径开新腿。

## 6. 测试矩阵

| 层 | 测试 |
|---|---|
| 单元 | `_compute_leverage(edge_z)` 边界 0/1/2/3 + clip 上下限 |
| 单元 | `_compute_edge_score` funding-only vs combined-with-basis + universe 单元素退化情况 |
| 单元 | `_outlier_check`：warmup / no_baseline / 正常 / 超阈值四态 |
| 单元 | `_set_leverage_cached` 缓存 hit / miss / 失败 |
| 集成 | mock OKX REST 跑一次完整 rebalance：assert set-leverage 次数 == 新 instruments，tdMode=isolated 进 ExecClient submit |
| 集成 | wick 模拟：构造 80× spike 的 closes feed，outlier guard 拦下入场 |
| 集成 | 全 leg 都被 outlier 拒 → emit WARN + skip rebalance |
| Smoke (live) | dry-run 一次，OKX REST 返回 set-leverage 成功 + `/positions` 显示 mgnMode=isolated |

## 7. Rollout

```
1. Local: 代码 + 全部单测 + 集成测试 (mock OKX REST)
2. Local: backtest 7d FundingXS 数据，确认 cross-mode fallback 不破坏回测 PnL
3. Commit + push origin/main
4. VPS: git pull + systemctl restart
5. 监控 next funding window (0/8/16 UTC 最近一个):
   - log "set-leverage isolated lever=X for inst=Y"
   - OKX REST /positions 返回 mgnMode=isolated
   - outlier_guard 日志
6. 24h 观察后回看
```

### 7.1 验收指标

| 指标 | 期望 |
|---|---|
| set-leverage 调用次数 / rebalance | == 新 inst 数 |
| OKX 持仓 mgnMode | `isolated` for FundingXS 开的所有腿 |
| 单腿 isolated margin / 账户 | 0.5% - 5%（按 edge_score） |
| outlier_guard 拦截率 | 正常市况 < 5%；wick 期间会 spike |

### 7.2 回滚

- `enable_dynamic_lever: false` → 回到 cross + lever_max 10× 行为
- `enable_outlier_guard: false` → 关掉过滤，依旧用 isolated
- 同时关 + `margin_mode: cross` → 完全回到事故前行为

## 8. 风险 / 未敦定的项

1. **OKX adapter 是否已支持 `tdMode=isolated`**：需要先验证。若不支持，需先在 `src/okx_trade/adapter/` 加这条 path。**这是 implementation plan 第一步要 verify 的事**。
2. **多策略共用 inst**：若 FundingXS 持 DOT short isolated，另策略（如 XSMomentum）下 DOT cross 单子 — OKX 这两仓位的合并/隔离行为需查文档。最坏情况：限制 FundingXS 用专属 inst universe（与其他策略 universe 不重叠的子集）。
3. **OKX set-leverage 在 net mode 下行为**：当前 `pos_side_mode=net`。需确认 isolated + net mode 组合可用。`long_short` 模式可能必需。
4. **lever_base / lever_slope / outlier_vol_ratio 是假设值**，部署后跑数据观察再调。
5. **若 OKX demo data feed 自身有 wick bug**，outlier guard 在 demo 里可能频繁触发（demo 数据噪声大）。需要在 live 部署后观察拦截率，若 >20% 则放宽 `outlier_vol_ratio`。

## 9. 决策记录

| 决策 | 选项 | 理由 |
|---|---|---|
| 防御底层 | **isolated margin**（vs client-side stop-loss） | OKX-side 强制，gap-move 不会穿透 |
| Leverage 映射 | **结合 funding + basis z-score** | edge 不只是 funding，奥现价差也是 carry edge 的一部分 |
| Outlier guard 范围 | **保留** | 即使 isolated 已硬限上限，"不在 bad regime 下单"降低损失发生概率 |
| 范围 | **只动 FundingXS** | 这次事故主战场；其他策略可后续抽公共 handle |
| td_mode 注入 | **strategy attr B 路径** | 侵入面小，其他策略零影响 |
| Migration | **不主动迁** | 现持仓只有 2 个微仓，roll off 成本低 |

## 9a. Addendum 2026-05-26 — Discovery during P-Task 1

Pre-flight probe revealed **OKX demo 账户实际 `posMode='long_short_mode'`**（不是 spec 假设的 `net`）。set-leverage 在 long_short 账户下要求 `posSide=long` 或 `posSide=short`（不能 net）。

**调整决策**（不改 spec §3-7 主架构）：

1. **不切换 trader-level `pos_side_mode`**——避免影响其他策略的 OmsType 行为（NETTING→HEDGING）。
2. `_set_leverage_cached(inst, lever, pos_side)` 接受第三个参数：caller 传 `PosSide.LONG`/`PosSide.SHORT` 基于 leg direction。
3. Strategy 启动时 query `/api/v5/account/config` 一次缓存 `posMode`，根据它决定 set-leverage 的 posSide 参数：
   - `net_mode` → `posSide=None`（`account.py` 自动补 `PosSide.NET`）
   - `long_short_mode` → 按 leg direction 传 `PosSide.LONG` / `PosSide.SHORT`
4. **订单本身不变**：trader pos_side_mode 仍是 `net`，adapter `resolve_pos_side` 见 `pos_side_mode != "long_short"` 返 `None`，不发 posSide 字段。OKX 已经在长这个组合（订单不带 posSide × 账户 long_short）下成功成交（重启后 64+ fill 验证）。
5. **`account.py:set_leverage` 已有 helper-side auto-fill**（在 P-Task 1 commit `ca2a502`）：`mgn_mode=ISOLATED + pos_side=None` 自动补 `PosSide.NET`。long_short 账户下 caller 必须显式传 LONG/SHORT，不能依赖 helper 默认。

## 10. 后续工作（out of scope，下次议）

- 抽 `IsolatedMarginPolicy` / `OutlierGuard` 为公共 risk handle，让 FundingCarry / FundingSkew / XSMomentum 接入
- 51015 reconcile 失败的根因 fix（独立 bug，不在本 spec 范围）
- Daily-report 增加"per-leg isolated margin 利用率"
