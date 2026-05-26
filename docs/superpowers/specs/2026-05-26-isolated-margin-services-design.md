# Phase 1 — IsolatedMarginService + VolatilityFilter + OkxStrategyBase

**日期**：2026-05-26 (later same day as the FundingXS three-layer defense spec)
**作者**：DrZhangXD + Claude
**前置**：[2026-05-26-funding-xs-isolated-margin-design.md](2026-05-26-funding-xs-isolated-margin-design.md) §10 "后续工作"

## 1. 背景

FundingXSStrategy 现在自带 isolated margin + dynamic leverage + outlier guard 三层防御（spec/plan 已落地、936/949 单测通过、VPS 已部署）。该实现的所有"基础设施"都在 strategy 类内部：`_set_lever_cache`、`_get_account_pos_mode`、`_set_leverage_cached`、`_closes_1m_by_inst`、`_is_backtest_context`。

剩余 9 个策略（含 2026-05-22 引发 51169 cascade 的 XSMomentum）面临**完全相同的 wick 风险**——但目前每个策略要复制 ~80 行代码才能享有同样保护。本 spec 把那 80 行抽成**两个共享 service + 一个 thin Strategy base**，让任意策略通过 yaml flag 即可 opt-in。

## 2. 目标 / 非目标

### 目标

- **单一真相**：posMode 缓存、(inst, posSide) → lever 缓存、1m bar buffer 全在 service 层，10 个策略共享同一份。跨策略 cache hit 自动生效。
- **opt-in 成本最小化**：每个策略加 ≤5 行（换父类 + 加一个 config flag + `submit_order` 换 `await submit_isolated_order`）即可享受 isolated margin。
- **保护多腿 abort 语义**：service 暴露 `batch_ensure_leverage`，让 XSMomentum / StatArb / FundingXS 等多腿策略复用两阶段提交，防 directional residual。
- **零回归**：FundingXS 行为完全等价（迁移仅是搬家，外部 contract 不变）。
- **零侵入 backtest**：service 不在 backtest engine 构造，strategy 检查 `iso_service is None` 直接走 cross fallback。

### 非目标

- 不动 trader-level `pos_side_mode=net` 设置——保持现有 order 路径兼容
- 不强制要求每个策略接入；按业务优先级阶段性 enable
- 不实现"全策略禁交易"开关（已有 AccountDrawdownCheck 做这个）
- 不模拟 isolated margin 行为在 backtest（NT MarginAccount 不支持精确隔离）

## 3. 架构

### 3.1 三 tier 调用模型

```
┌─────────────────────────────────────────────────────────────────────┐
│  Tier 3 (multi-leg rebalance, e.g. FundingXS / XSMomentum / StatArb):│
│     result = await self._iso_service.batch_ensure_leverage([...])    │
│     if not result.all_ok: log.warning("ABORT"); return               │
│     for leg in target.values():                                      │
│         await self.submit_isolated_order(order, lever, pos_side)     │
├─────────────────────────────────────────────────────────────────────┤
│  Tier 2 (single-shot, e.g. funding_carry / liq_reversal):            │
│     await self.submit_isolated_order(order, lever=5, pos_side=...)   │
├─────────────────────────────────────────────────────────────────────┤
│  Tier 1 (OKX adapter, already in place — no change):                 │
│     order.tags = ["td_mode:isolated"]                                │
│     submit_order(order)  # adapter sends tdMode=isolated to OKX      │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 DI 拓扑

```
build_live_context (runtime/live_node.py)
  ├── IsolatedMarginService (singleton)
  │   ├── lazy OKXRestClient
  │   ├── posMode cache: str | None
  │   ├── (inst, posSide) → lever cache
  │   └── log: get_logger("iso_margin")
  ├── VolatilityFilter (singleton)
  │   ├── inst → deque[float] 1m closes
  │   ├── config: VolatilityFilterConfig (from live.yaml.volatility_filter)
  │   └── log: get_logger("vol_filter")
  ├── 10 × Strategy instance
  │   ├── strategy._iso_service = iso_service
  │   └── strategy._vol_filter = vol_filter
  └── LiveMonitor
      ├── account_drawdown_tracker (existing)
      └── iso_service / vol_filter (for diagnostic endpoints; later phase)
```

## 4. 组件设计

### 4.1 `IsolatedMarginService`

**文件**：`src/okx_trade/risk/isolated_margin_service.py`

**接口**：

```python
@dataclass(slots=True, frozen=True)
class BatchEnsureResult:
    all_ok: bool
    failed: list[tuple[str, str]]   # [(inst_id, error_msg), ...]


class IsolatedMarginService:
    def __init__(self, rest_settings: OKXSettings, log) -> None: ...

    async def get_pos_mode(self) -> str:
        """Cached fetch of OKX account-level posMode.

        Return value is one of 'net_mode' / 'long_short_mode'.
        On unexpected OKX response, log WARN and fall back to 'net_mode'.
        On REST failure, log WARN and fall back to 'net_mode'.
        Cached for the lifetime of the service (posMode is account-level,
        switching it requires explicit user action + position closure).
        """

    async def ensure_leverage(
        self, inst_id: str, lever: float, pos_side: PosSide | None,
    ) -> tuple[bool, str | None]:
        """Idempotent set-leverage call.

        Args:
            inst_id: OKX format ("BTC-USDT-SWAP") OR NT format with .OKX
                suffix — service strips the suffix internally.
            lever: target leverage; rounded to int for OKX.
            pos_side: PosSide.LONG / PosSide.SHORT in long_short_mode;
                None in net_mode (account.py auto-fills PosSide.NET).

        Returns:
            (True, None) on success or cache hit.
            (False, err_msg) on REST failure; caller should skip the leg.
        """

    async def batch_ensure_leverage(
        self, items: list[tuple[str, float, PosSide | None]],
    ) -> BatchEnsureResult:
        """Multi-leg pre-validation. Calls ensure_leverage sequentially
        for each item, collecting results. Returns BatchEnsureResult
        with all_ok + list of (inst_id, err) failures.

        Sequential (not asyncio.gather) to:
          (a) preserve OKX rate-limit headroom
          (b) keep error attribution clear
          (c) cache hits on later items if earlier ones touched same inst
        """

    def make_isolated_tags(self) -> list[str]:
        """Returns ['td_mode:isolated']. Convenience for orders."""

    def is_backtest(self) -> bool:
        """True if rest_settings.api_key is empty / SecretStr(''). Used by
        OkxStrategyBase.submit_isolated_order to fall back to cross."""
```

### 4.2 `VolatilityFilter`

**文件**：`src/okx_trade/risk/volatility_filter.py`

**接口**：

```python
@dataclass(slots=True, frozen=True)
class VolatilityFilterConfig:
    enable: bool = False
    window_min: int = 60
    baseline_min: int = 1440
    warmup_min: int = 1440
    ratio_threshold: float = 3.0
    buffer_max: int = 2000


class VolatilityFilter:
    def __init__(self, config: VolatilityFilterConfig, log) -> None: ...

    def feed_bar(self, inst_id: str, close: float) -> None:
        """Strategy calls from on_bar(1m). inst_id is OKX format (no .OKX
        suffix). Lazy-creates deque(maxlen=buffer_max) on first call.

        Multi-strategy sharing: NT bar subscription is deduped at the
        DataEngine level, so if two strategies subscribe DOT-USDT-SWAP/1m
        and both call feed_bar, only the first call's bar actually arrives
        (subsequent strategies get the same Bar object via shared cache).
        Service has no internal dedup needed.
        """

    def allow(self, inst_id: str) -> tuple[bool, str]:
        """Wraps the existing _isolated_helpers.outlier_check pure function
        with the service's buffer + config.

        Returns:
          (True, "disabled")    if config.enable is False
          (True, "warmup")      if buffer has < warmup_min entries
          (True, "no_baseline") if std(log_returns) == 0
          (True, "ok")          if recent vol within ratio_threshold
          (False, "vol_ratio=R>T") otherwise
        """

    def buffer_size(self, inst_id: str) -> int:
        """Diagnostics: how many bars accumulated for inst_id. 0 if not fed yet."""
```

### 4.3 `OkxStrategyBase`

**文件**：`src/okx_trade/strategies/_okx_base.py`

**接口**：

```python
class OkxStrategyBase(Strategy):
    """Thin optional base for OKX-aware strategies. Inheriting it gives:
    - DI slots for IsolatedMarginService / VolatilityFilter
    - submit_isolated_order(...) one-call helper for the typical path
    - vol_filter_allow(...) convenience wrapper

    Strategies that don't inherit it can still use the services via
    direct attribute access (DI works the same). Inheriting is the
    recommended path for new strategies.
    """

    _iso_service: IsolatedMarginService | None = None
    _vol_filter: VolatilityFilter | None = None

    async def submit_isolated_order(
        self, order, *, lever: float, pos_side: PosSide | None = None,
    ) -> bool:
        """One-call isolated submit. Returns True iff submitted to OKX.

        Reads enable_isolated_margin from self.config (each subclass's
        Config dataclass must declare ``enable_isolated_margin: bool = False``).

        Flow:
          1. Guard rails: if self.config.enable_isolated_margin is False,
             or self._iso_service is None, or service.is_backtest() →
             just submit_order(order); return True. (Cross fallback.)
          2. await iso_service.get_pos_mode() (cached).
          3. In long_short_mode and caller didn't pass pos_side, derive:
             - order.side == BUY  → PosSide.LONG
             - order.side == SELL → PosSide.SHORT
             In net_mode, force pos_side = None.
          4. await iso_service.ensure_leverage(inst_id_okx, lever, pos_side).
             On failure (returns False) → log + return False.
          5. order.tags = list(order.tags or []) + iso_service.make_isolated_tags()
          6. submit_order(order). Return True.
        """

    def vol_filter_allow(self, inst_id_okx: str) -> tuple[bool, str]:
        """Convenience wrapper; returns (True, 'no_filter') if not injected."""
```

### 4.4 Pure helpers (existing, unchanged)

- `src/okx_trade/strategies/_isolated_helpers.py` (`compute_leverage`, `compute_edge_score`, `outlier_check`) — already in place and reused by service + strategies.

## 5. 配置 schema

### 5.1 `configs/live.yaml`

```yaml
# 2026-05-26 Phase 1: global volatility filter
volatility_filter:
  enable: true
  window_min: 60
  baseline_min: 1440
  warmup_min: 1440
  ratio_threshold: 3.0
  buffer_max: 2000

# 每个策略加 enable_isolated_margin flag（默认 false）
strategies:
  funding_cross_section:
    config:
      enable_isolated_margin: true   # 已 enable
      # 旧的 outlier_* 字段 deprecated（保留兼容老 yaml，但实际不读）
  funding_carry:
    config:
      enable_isolated_margin: true
      isolated_lever: 5
  xs_momentum:
    config:
      enable_isolated_margin: true
      isolated_lever: 3
  funding_skew_momentum:
    config:
      enable_isolated_margin: true
      isolated_lever: 5
  stat_arb_pairs:
    config:
      enable_isolated_margin: true
      isolated_lever: 3
  basis_arb:
    config:
      enable_isolated_margin: true
      isolated_lever: 3
  factor_portfolio:
    config:
      enable_isolated_margin: true
      isolated_lever: 3
  ml_fusion:
    config:
      enable_isolated_margin: true   # 装好 xgboost 再 enable
      isolated_lever: 3
  liq_reversal:
    config:
      enable_isolated_margin: false  # wick = alpha
  ob_imbalance:
    config:
      enable_isolated_margin: false  # 微秒级，isolated 反拖累
```

### 5.2 默认值 + 决策

| 策略 | enable | lever | 理由 |
|---|---|---|---|
| funding_cross_section | ✓ | dynamic (2-10) | 已 enabled，复用 dynamic_lever logic |
| funding_carry | ✓ | 5 | spot+perp delta-neutral，perp 腿 wick 险 |
| funding_skew_momentum | ✓ | 5 | trend on funding tail |
| xs_momentum | ✓ | 3 | 多腿，触发 51169 那个 |
| stat_arb_pairs | ✓ | 3 | 多腿配对 |
| basis_arb | ✓ | 3 | futures 腿 wick 险 |
| factor_portfolio | ✓ | 3 | 多腿 |
| ml_fusion | ✓ | 3 | 多腿，但 xgboost 未装 |
| liq_reversal | ✗ | — | wick 入场是 alpha |
| ob_imbalance | ✗ | — | 亚秒级，isolated 拖延 |
| range_breakout | ✗ | — | retired |

## 6. 实现 / Rollout

> **Scope note**：本 spec 对应的 **implementation plan 只覆盖 Phase 1a + 1b**
> （infrastructure + FundingXS 迁移），因为这两步有硬依赖：先建 service /
> base / DI，然后 FundingXS 必须迁过去验证零回归。Phase 1c-1f（依次接入
> 9 个其他策略）是独立 PR，每个策略一个 follow-up plan / 各自的 yaml flag
> 翻开 + 各自的 funding window 验证。

### 6.1 Phase 1a — Infrastructure (no strategy changes)

- `IsolatedMarginService` + `VolatilityFilter` + `OkxStrategyBase` 落地
- DI in `build_live_context`
- 单元测试覆盖 service + base helper 所有分支
- 部署：service 构造但没策略用 → 业务零变化

### 6.2 Phase 1b — Migrate FundingXS to new architecture

- 删 strategy 内的 `_set_lever_cache` / `_get_account_pos_mode` / `_set_leverage_cached` / `_closes_1m_by_inst` / `_is_backtest_context`
- 改父类 `FundingXSStrategy(OkxStrategyBase)`
- 把 `_open_leg` / `_execute_diff` 内 set-leverage 调用换成 `self._iso_service.batch_ensure_leverage(...)` + `self.submit_isolated_order(...)`
- 把 1m bar `on_bar` 内的 `_closes_1m_by_inst[inst].append` 换成 `self._vol_filter.feed_bar(inst_id_okx, close)`
- 把 `_compute_target_positions` 内的 outlier check 换成 `self.vol_filter_allow(inst_value)`
- **验收**：next funding window 行为 100% 等价（set-leverage 次数、ABORT 触发、open_legs、OKX positions mgnMode）

### 6.3 Phase 1c — funding_carry (single-leg, simplest)

- 继承 OkxStrategyBase
- Config: enable_isolated_margin + isolated_lever
- 在 `_enter_long_position` / `_enter_short_position` 把 `submit_order(perp_order)` 换成 `await self.submit_isolated_order(perp_order, lever=self.config.isolated_lever)`
- spot 腿继续走 cash mode（OKX 现货账户不需要 isolated）
- 部署 7 天观察

### 6.4 Phase 1d — xs_momentum (multi-leg)

- 继承 OkxStrategyBase
- 在 daily rebalance 加 batch_ensure_leverage + 两阶段开仓循环
- 解决 2026-05-22 stat_arb 残仓引发的 51169 cascade 在结构上的源头
- 部署 7 天

### 6.5 Phase 1e — 其他 4 个高优先

并行接入：funding_skew_momentum / stat_arb_pairs / basis_arb / factor_portfolio。各自 PR / 各自验证。

### 6.6 Phase 1f — 清理

- 旧 outlier_* config 字段从 funding_cross_section.yaml 删除
- FundingXSConfig 里 deprecated 字段加 deprecation warning + 1 个 release 后删
- CHANGELOG + ARCHITECTURE 更新

## 7. 测试矩阵

| 层 | 测试 |
|---|---|
| 单元 | `IsolatedMarginService.ensure_leverage` cache miss/hit/REST-fail/`.OKX` suffix strip |
| 单元 | `IsolatedMarginService.ensure_leverage` 三 pos_side 状态 (None/LONG/SHORT) |
| 单元 | `IsolatedMarginService.batch_ensure_leverage` all_ok / 部分失败 / failed list |
| 单元 | `IsolatedMarginService.get_pos_mode` 首次 / cached / unknown WARN / REST fail |
| 单元 | `IsolatedMarginService.is_backtest` empty/None/real api_key |
| 单元 | `VolatilityFilter.feed_bar` lazy create deque + maxlen truncation |
| 单元 | `VolatilityFilter.allow` disabled / warmup / no_baseline / ok / threshold breach |
| 单元 | `VolatilityFilter` 多策略共享 buffer（一次 feed_bar 后两个 allow 调用一致） |
| 单元 | `OkxStrategyBase.submit_isolated_order` 全 7 分支 (disabled / no service / backtest / net / long_short / lever-fail / 成功) |
| 单元 | `OkxStrategyBase.vol_filter_allow` no-filter / inject 路径 |
| 集成 | mock OKX REST，FundingXS 迁后跑一轮 rebalance，行为与 Phase 1a 前等价 |
| 集成 | xs_momentum 一轮 daily rebalance：order tags 含 td_mode:isolated + set-leverage 调对 |
| Smoke (live) | VPS 重启后 service log "cached account posMode=long_short_mode" 一次 + 多策略 cache-hit 计数 |

## 8. 验收指标

| 指标 | 期望 |
|---|---|
| Phase 1a 部署 | service 构造日志各 1 次，无业务行为变化 |
| Phase 1b 部署后 next funding window | FundingXS 行为 100% 等价 baseline（set-leverage 次数、ABORT、open_legs） |
| Phase 1c-e 各策略部署 | journal 含 "set leverage isolated"，OKX `/positions` 显 mgnMode=isolated |
| 跨策略 cache hit 率 | 第二个接入 service 的策略在同 (inst, side) 下应 100% cache hit（看 service log） |
| 全套单测 | 949 baseline + ~40 新测，全绿 |
| VPS 每次重启 51015 warning | 仍是 0（已修） |

## 9. 风险 / 未敦定

1. **OKX rate limit on set-leverage**：spec 假设 20 req/2s 足够。10 策略 × 平均 3 个 inst × 2 side = 60 calls per startup burst。需观察是否触发 429（service 已包 try/except，不致命）。
2. **Service singleton 并发**：多策略可能同时 await `ensure_leverage(same_inst, same_side)`。当前实现非线程安全但 asyncio 单线程，写 cache 是原子 dict assign，应 OK。如果未来加 lock 防 race，记得用 `asyncio.Lock` 不是 `threading.Lock`。
3. **NT bar subscription dedup**：spec 假设 NT DataEngine 自动 dedup 多策略订同 inst-1m bar。需在实现时 verify（看 DataEngine 文档或加 1 行测试）。
4. **Phase 1b 行为等价**：迁移前后必须 byte-byte 一致。需要 baseline log diff 工具或人肉对照下次 funding window。
5. **Backtest 路径**：NT BacktestEngine 不构造 service。strategy `_iso_service is None` 走 cross fallback。需 verify 没有非 None 检查路径漏。

## 10. 决策记录

| 决策 | 选项 | 理由 |
|---|---|---|
| 抽象形状 | service 单例 | 跨策略 cache + 单一 REST 入口 + 避免 mixin/subclass 复杂度 |
| Opt-in 机制 | OkxStrategyBase + yaml flag | 调用点最少 + 显式 + 可继承可不继承 |
| Tier 数量 | 3 (adapter tag / base helper / service batch) | 单腿 + 多腿 + 已有 adapter path 各得其所 |
| Service 数量 | 2 (isolated + vol_filter 分开) | 单一职责，vol_filter 也用于 disable isolated 的策略（liq_reversal 想看 vol 但不要 isolated） |
| 默认 enable | 8/10 yes, 2/10 no | wick = alpha 的两个明确 opt-out |
| Phase order | infra → FundingXS → 1 单腿 → 1 多腿 → 4 并行 → 清理 | 风险递增，每段独立 verify |

## 11. 后续工作（out of scope，下次议）

- VolatilityFilter 全策略 disable 开关（紧急避险）
- IsolatedMarginService 写入 PnL DB（leverage 历史归档）
- Per-strategy lever 上限强约束（service 拒绝超限）
- LiveMonitor 给 service cache 暴露 healthcheck endpoint
