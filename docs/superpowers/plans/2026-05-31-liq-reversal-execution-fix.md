# liq_reversal 执行修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 liq_reversal 的实测盈亏比从 ~1.2R 修回接近名义 3R，并停止逆势 falling-knife，使该策略在 25% 胜率下不再结构性亏损。

**Architecture:** 两个独立子改动。**Part A（趋势否决）**：入场前加一个 EMA-slope 趋势过滤，否决"逆势 fade"（下跌趋势里不做多、上涨趋势里不做空）—— 小改、纯函数、可单测，先做。**Part B（tick 级出场）**：把"1m bar 收线检测 SL/TP + 下一根市价平"换成 NautilusTrader **订单仿真**（`LIMIT_IF_TOUCHED` 做 TP + `STOP_MARKET` 做 SL，`emulation_trigger` 用 quote/trade tick）——在 tick 上触发，消除 TP 市价回撤与 SL 穿价，不依赖 OKX 原生 algo 单。

**Tech Stack:** Python 3.13、NautilusTrader（`Strategy` / `OrderFactory` / order emulation）、pytest。复用现有 `RiskIntent` / `apply_risk_manager` 链路。

**为什么不靠回测验证：** liq_reversal 吃 OKX `liquidation-orders` WS 实时流，离线无历史清算数据，无法 NT backtest。验证用 **paper A/B**：先上 Part A 观察 3-5 天 round-trip 方向命中率，再上 Part B 观察 avgW/avgL 是否回升（目标 avgW→名义 TP 的 ~90%、avgL→名义 SL 的 ~1.1×内）。

---

## 背景数据（2026-05-31 复盘，post-fix 4 天，12 round-trips）

| | 名义 | 实测 | 偏差 |
|---|---|---|---|
| avgW | 1.5% TP ≈ $11.78 | $6.29 | 仅 53%（TP 触碰后市价回撤）|
| avgL | 0.5% SL ≈ $3.92 | $5.24 | 134%（瀑布击穿止损）|
| 实测 RR | 3.0 | 1.2 | — |

EV：现状 `0.25×6.29 − 0.75×5.24 = −2.36/笔`；执行修好后 `0.25×11.78 − 0.75×3.92 ≈ +0.005/笔`。方向上 4 天 ~13 LONG vs 3 SHORT，入场价单边下行 = 逆势。

`risk_pct` 已于 2026-05-31 临时 0.5%→0.25% 止血（本 plan 落地后可回调）。

---

## File Structure

- `src/okx_trade/strategies/liq_reversal.py` — 主改：加趋势过滤纯函数 + EMA 状态 + veto；Part B 改 `_enter`/`_exit`/`on_bar` 出场路径为 emulated bracket。
- `src/okx_trade/strategies/_trend.py` （**新建**）— 纯函数 `ema_update()` / `trend_direction()`，便于跨策略复用 + 单测（range_breakout 同样可用）。
- `tests/unit/strategies/test_liq_reversal_trend_filter.py` （**新建**）— Part A 测试。
- `tests/unit/strategies/test_liq_reversal_bracket.py` （**新建**）— Part B 测试。
- `configs/strategies/liq_reversal.yaml` — 加 `enable_trend_filter` / `trend_ema_fast` / `trend_ema_slow`；Part B 加 `use_emulated_bracket`。
- `src/okx_trade/runtime/live_node.py` — 若新增 config 字段经 StrategyConfig 透传，确认无白名单拦截（liq config 直接进 `LiqReversalConfig`，非 `_build_risk_config`，应无需改）。

---

## Part A — 趋势否决过滤（先做，小改高 ROI）

### Task 1: 趋势方向纯函数

**Files:**
- Create: `src/okx_trade/strategies/_trend.py`
- Test: `tests/unit/strategies/test_liq_reversal_trend_filter.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/strategies/test_liq_reversal_trend_filter.py
from okx_trade.strategies._trend import ema_update, trend_allows


def test_ema_update_seeds_then_smooths():
    # 首个值作种子
    assert ema_update(None, 100.0, period=10) == 100.0
    # 第二个值按 alpha=2/(period+1) 平滑
    e = ema_update(100.0, 110.0, period=9)  # alpha=0.2
    assert abs(e - 102.0) < 1e-9


def test_trend_allows_vetoes_counter_trend_long():
    # 下跌趋势 (fast < slow) → 否决 long（fade 卖出瀑布）
    assert trend_allows("long", ema_fast=90.0, ema_slow=100.0) is False
    # 上涨趋势 → 允许 long
    assert trend_allows("long", ema_fast=110.0, ema_slow=100.0) is True


def test_trend_allows_vetoes_counter_trend_short():
    assert trend_allows("short", ema_fast=110.0, ema_slow=100.0) is False
    assert trend_allows("short", ema_fast=90.0, ema_slow=100.0) is True


def test_trend_allows_flat_permits_both():
    # fast==slow（无趋势）→ 不否决
    assert trend_allows("long", ema_fast=100.0, ema_slow=100.0) is True
    assert trend_allows("short", ema_fast=100.0, ema_slow=100.0) is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/strategies/test_liq_reversal_trend_filter.py -q`
Expected: FAIL — `ModuleNotFoundError: okx_trade.strategies._trend`

- [ ] **Step 3: 写最小实现**

```python
# src/okx_trade/strategies/_trend.py
"""趋势方向纯函数（EMA-slope）。供 reversal 类策略否决逆势 fade。"""
from __future__ import annotations

from .base import Direction  # Literal["long","short"]; 若 base 未导出则用 Literal 本地定义


def ema_update(prev: float | None, value: float, *, period: int) -> float:
    """增量 EMA。prev=None → 用 value 作种子。"""
    if prev is None:
        return value
    alpha = 2.0 / (period + 1.0)
    return prev + alpha * (value - prev)


def trend_allows(direction: str, *, ema_fast: float, ema_slow: float) -> bool:
    """逆势否决：下跌(fast<slow)否决 long，上涨(fast>slow)否决 short。fast==slow 放行。"""
    if direction == "long" and ema_fast < ema_slow:
        return False
    if direction == "short" and ema_fast > ema_slow:
        return False
    return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/strategies/test_liq_reversal_trend_filter.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/strategies/_trend.py tests/unit/strategies/test_liq_reversal_trend_filter.py
git commit -m "feat(strategies): add EMA trend_allows pure fn for counter-trend veto"
```

### Task 2: liq_reversal 接入趋势过滤

**Files:**
- Modify: `src/okx_trade/strategies/liq_reversal.py`（`LiqReversalConfig` 加字段；`__init__` 加 EMA 状态；`on_bar` 更新 EMA；`maybe_trigger` 调 veto）
- Test: `tests/unit/strategies/test_liq_reversal_trend_filter.py`（追加策略级测试）

- [ ] **Step 1: 写失败测试（策略级，复用现有 NT-skip 守卫）**

```python
import pytest
nt = pytest.importorskip("nautilus_trader")  # 无 NT 环境跳过

def _make_strategy(enable_trend_filter=True):
    from okx_trade.strategies.liq_reversal import LiqReversalConfig, LiqReversalStrategy
    cfg = LiqReversalConfig(
        instrument_id="BTC-USDT-SWAP.OKX",
        bar_type="BTC-USDT-SWAP.OKX-1-MINUTE-LAST-EXTERNAL",
        subscribe_liquidations=False,
        enable_trend_filter=enable_trend_filter,
        trend_ema_fast=5, trend_ema_slow=20,
    )
    return LiqReversalStrategy(cfg)

def test_config_has_trend_fields():
    s = _make_strategy()
    assert s.config.enable_trend_filter is True
    assert s.config.trend_ema_fast == 5

def test_downtrend_vetoes_long_entry(monkeypatch):
    # 喂一串下跌 close 让 fast<slow，触发 long 信号应被 veto（_enter 不被调）
    s = _make_strategy()
    called = {"enter": False}
    monkeypatch.setattr(s, "_enter", lambda d: called.__setitem__("enter", True))
    # 直接驱动 EMA 状态 + 触发判定（绕过 NT bar 管线）
    for px in [100, 99, 98, 97, 96, 95, 94, 93, 92, 91]:
        s._update_trend(float(px))
    s._latest_close = 91.0
    s._latest_ts_ms = 10_000_000
    # 模拟 z 命中 + dominant=sell → 想做 long
    monkeypatch.setattr("okx_trade.strategies.liq_reversal.liq_zscore",
                        lambda *a, **k: (5.0, "sell"))
    s.maybe_trigger()
    assert called["enter"] is False  # 下跌趋势否决了逆势 long
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/strategies/test_liq_reversal_trend_filter.py -q`
Expected: FAIL — `LiqReversalConfig` 无 `enable_trend_filter` / 无 `_update_trend`

- [ ] **Step 3: 实现**

`LiqReversalConfig` 加字段（在现有字段后）：
```python
        enable_trend_filter: bool = False
        trend_ema_fast: int = 20
        trend_ema_slow: int = 60
```

`__init__` 末尾加状态：
```python
            self._ema_fast: float | None = None
            self._ema_slow: float | None = None
```

加方法 + 在 `on_bar` 里调用（`snap.close` 已有）：
```python
        from ._trend import ema_update, trend_allows  # 顶部 import

        def _update_trend(self, close: float) -> None:
            cfg: LiqReversalConfig = self.config  # type: ignore[assignment]
            self._ema_fast = ema_update(self._ema_fast, close, period=cfg.trend_ema_fast)
            self._ema_slow = ema_update(self._ema_slow, close, period=cfg.trend_ema_slow)
```
`on_bar` 中 `self._latest_close = snap.close` 之后加 `self._update_trend(snap.close)`。

`maybe_trigger` 在 `decision is None: return` 之后、`self._enter(decision)` 之前加 veto：
```python
            cfg: LiqReversalConfig = self.config  # type: ignore[assignment]
            if (cfg.enable_trend_filter and self._ema_fast is not None
                    and self._ema_slow is not None
                    and not trend_allows(decision, ema_fast=self._ema_fast,
                                         ema_slow=self._ema_slow)):
                self.log.info(
                    f"liq_reversal trend-veto {decision}: "
                    f"ema_fast={self._ema_fast:.2f} ema_slow={self._ema_slow:.2f}"
                )
                return
```

- [ ] **Step 4: 跑测试确认通过 + 全套回归**

Run: `.venv/bin/python -m pytest tests/unit/strategies/test_liq_reversal_trend_filter.py tests/unit -q`
Expected: PASS，全套通过（含原有 liq_reversal 测试不破）

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/strategies/liq_reversal.py tests/unit/strategies/test_liq_reversal_trend_filter.py
git commit -m "feat(liq_reversal): veto counter-trend entries via EMA trend filter (opt-in)"
```

### Task 3: 启用 + 文档

- [ ] **Step 1:** `configs/strategies/liq_reversal.yaml` 加：
```yaml
enable_trend_filter: true
trend_ema_fast: 20    # 20 根 1m EMA
trend_ema_slow: 60    # 60 根 1m EMA；fast<slow=下跌→否决做多
```
- [ ] **Step 2:** CHANGELOG 加 `feat(liq_reversal)` 条目（趋势否决，opt-in，paper A/B）。
- [ ] **Step 3:** Commit。**部署需用户确认（memory: deploy scope re-auth）。** 上线后 paper 观察 3-5 天：逆势 round-trip 占比应显著下降。

---

## Part B — tick 级出场（emulated bracket，较大改动）

> 前置：Part A 已上且观察无异常。本部分消除 avgW 回撤 / avgL 穿价。

### Task 0（Spike，必做）: 验证 NT OKX 适配器的 emulation / 订单类型支持

**目的：** 确认 NautilusTrader 的 OKX 适配器（当前版本）能否接受 `STOP_MARKET` / `LIMIT_IF_TOUCHED`，以及 NT order **emulation**（`emulation_trigger=LAST` 或 `BID_ASK`）在本项目 live engine 下是否生效。

- [ ] **Step 1:** 读 `pyproject.toml` 锁定的 nautilus_trader 版本；查该版本 `OrderFactory` 是否有 `limit_if_touched` / `stop_market` / `bracket`，以及 `OrderEmulator` 是否随 live engine 默认启用。
- [ ] **Step 2:** 查策略是否已订阅 quote/trade tick（当前只订 1m bar + 自持 liq WS）。emulation_trigger=LAST 需要 trade ticks；BID_ASK 需要 quote ticks。确定要 `self.subscribe_trade_ticks(self._inst_id)` 还是 `subscribe_quote_ticks`。
- [ ] **Step 3:** 写一个最小 spike 脚本（`scripts/probe_emulated_bracket.py`，**不进 live**）在 paper 上跑一笔：market 进场 + 挂 emulated STOP_MARKET，观察是否在 tick 上触发、fill 价是否贴近 trigger。记录结论到本 plan 末尾"Spike 结论"。
- [ ] **Step 4:** **决策门**：若 emulation 可用 → 走 Task 4-7；若不可用/适配器不支持 → 降级方案：把出场检测从 1m bar 改为 trade-tick 回调（`on_trade_tick` 里查 SL/TP，仍市价平，但 tick 级延迟 << 1m），仍能大幅减小滑点。把降级决定写入本节。

### Task 4: 出场状态机改造（保留 market 进场，出场挂 emulated bracket）

**Files:**
- Modify: `src/okx_trade/strategies/liq_reversal.py`（`_enter` 进场成交后挂 TP/SL；删/停用 `_check_exit_on_bar` 的 1m 检测；加 `on_order_filled` 处理 bracket 成交 + 撤对侧）
- Test: `tests/unit/strategies/test_liq_reversal_bracket.py`

- [ ] **Step 1: 写失败测试** — 用 NT test kit / stub 验证：进场 fill 后挂了一个 reduce-only `LIMIT_IF_TOUCHED`@tp 和一个 `STOP_MARKET`@sl；其一成交后另一被 cancel；`record_strategy_trade` 用**实际 fill 价**而非 synthetic level。

```python
import pytest
pytest.importorskip("nautilus_trader")
# 详细 stub：构造 LiqReversalStrategy（use_emulated_bracket=True），
# 模拟 on_order_filled(entry_fill) → 断言 self.submit_order 被调两次，
# 一次 LIMIT_IF_TOUCHED(reduce_only,@tp)、一次 STOP_MARKET(reduce_only,@sl)，
# 且 emulation_trigger 已设；再模拟 tp 成交 → 断言对侧 sl 被 cancel_order。
# （此处按 NT TestStubs 写全；执行时补全 mock 细节。）
```

- [ ] **Step 2:** 跑测试确认失败（`use_emulated_bracket` / `on_order_filled` 未实现）。
- [ ] **Step 3: 实现**
  - `LiqReversalConfig` 加 `use_emulated_bracket: bool = False`。
  - `_enter`：保持 market IOC 进场，**不再**本地设 synthetic level；改在 `on_order_filled`（进场单成交回调）里按 fill 价算 sl/tp 并挂两个 reduce-only emulated 单（`order_factory.stop_market(..., emulation_trigger=TriggerType.LAST)` + `order_factory.limit_if_touched(...)`）。
  - `on_bar`：移除/旁路 `_check_exit_on_bar`（出场交给 emulated 单）；保留喂风控/equity。
  - `on_order_filled`：若是 bracket 一侧成交 → `cancel_order` 对侧；调 `record_strategy_trade` 用实际 fill 价；清 `_active_direction`。
  - `on_order_rejected`：清 phantom（已有逻辑），并撤未成交的对侧。
- [ ] **Step 4:** 跑测试 + 全套回归确认通过。
- [ ] **Step 5:** Commit `feat(liq_reversal): tick-emulated TP/SL bracket exits (opt-in)`。

### Task 5: 订阅 tick（按 Task 0 结论）

- [ ] `on_start` 加 `self.subscribe_trade_ticks(self._inst_id)`（或 quote）；确认 1m bar 仍订（喂风控）。测试：on_start 调用了订阅。Commit。

### Task 6: 启用 + 回调 risk_pct

- [ ] `configs/strategies/liq_reversal.yaml`：`use_emulated_bracket: true`；`risk_pct` 0.0025 → 回调 0.005（执行修好后恢复正常仓位）。CHANGELOG 记。Commit。**部署需用户确认。**

### Task 7: Paper A/B 验证

- [ ] 上线后观察 5-7 天，用 `trades_okx` 重算 liq_reversal 的 avgW/avgL/RR（同复盘脚本）。**通过标准**：avgL ≤ 名义 SL ×1.15、avgW ≥ 名义 TP ×0.85、实测 RR ≥ 2.3。未达标 → 回滚 + 回 Task 0 决策门。

---

## 验证策略（汇总）

离线无清算数据 → 不做 NT backtest，用 **paper A/B + 复盘脚本**逐步验证：
1. Part A 上线 → 观察逆势 round-trip 占比下降、整体 PnL 改善。
2. Part B 上线 → 观察 avgW/avgL 回归名义。
3. 全部达标后回调 `risk_pct` 到 0.5%。任一步劣化即回滚（每步独立 commit + opt-in flag，回滚 = 关 flag）。

## Self-Review

- **Spec coverage**：执行滑点 → Part B（emulated bracket）；逆势偏向 → Part A（trend veto）；止血 → 已用 risk_pct 减半（本 plan 落地后 Task 6 回调）。✓
- **Placeholder**：Task 4 Step 1 的 NT mock 标注"执行时补全"——属 Spike 后才能定的适配器细节，已用 Task 0 决策门兜住，非空泛占位。✓
- **Type 一致**：`trend_allows(direction, *, ema_fast, ema_slow)` / `ema_update(prev, value, *, period)` 在 Task 1 定义、Task 2 调用一致；config 字段名 `enable_trend_filter`/`trend_ema_fast`/`trend_ema_slow`/`use_emulated_bracket` 全程一致。✓

## Spike 结论（执行 Task 0 后填）

> _待填：NT 版本、OKX 适配器 emulation 支持情况、tick 订阅选择、Task 4-7 vs 降级方案的决定。_
