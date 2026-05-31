"""把 ``RiskManager`` 接入 NT Strategy 的辅助层（M3.6）。

为什么单独一个 module？
-------------------
策略层（``xs_momentum`` / ``funding_carry`` / ``liq_reversal`` ...）和风控层（``risk/*``）应保持
正交：策略不知道有哪些 check，风控不知道策略的下单细节。这个 module 是两者间的
**唯一胶水**：

1. **``RiskConfig``** —— msgspec 友好的 dataclass，可以放进 NT ``StrategyConfig``
   字段里被序列化。每个 check 一对 ``enable_*`` + 参数；
2. **``build_risk_manager(config)``** —— 工厂：根据 config 构造 ``RiskManager``
   及一个 handle 字典（暴露内部 check 实例供策略喂数据）；
3. **``apply_risk_manager(strategy, manager, intent)``** —— 在策略下单点调用，
   统一处理 APPROVE / SCALE / REJECT 三种 action，写日志，返回最终 size 或 None。

策略接入只需：

- ``__init__``: ``self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)``
- 下单前: ``size = apply_risk_manager(self, self._risk_manager, intent)``
- ``on_bar`` 末尾: 用 handles 喂 returns / equity 数据
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .base import RiskAction, RiskCheck, RiskIntent, RiskManager
from .correlation import CorrelationCheck
from .drawdown import AccountDrawdownCheck, AccountDrawdownTracker, DrawdownCheck, DrawdownTracker
from .kelly import KellyCheck
from .regime import RegimeDetectorProtocol, RegimeGateCheck, StrategyKind
from .trade_rate import TradeRateCheck
from .vol_target import VolTargetCheck

if TYPE_CHECKING:
    from nautilus_trader.trading.strategy import Strategy


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """风控参数集合，可放进 NT ``StrategyConfig`` 的字段里。

    用 ``frozen=True`` 是为了和 NT msgspec Struct 兼容（不可变 + 可哈希）。
    每个 check 是 opt-in 的——默认全关，策略 config 显式 ``enable_*=True`` 才生效。
    """

    # vol_target
    enable_vol_target: bool = False
    vol_target_annualized: float = 0.15
    vol_window: int = 20
    vol_max_scale: float = 2.0
    vol_min_scale: float = 0.1
    vol_periods_per_year: int = 365

    # kelly
    enable_kelly: bool = False
    kelly_win_rate: float = 0.5
    kelly_avg_r: float = 1.0
    kelly_fraction: float = 0.25
    kelly_max_fraction: float = 0.5

    # drawdown
    enable_drawdown: bool = False
    drawdown_daily_pct: float = 0.03
    drawdown_weekly_pct: float = 0.08

    # correlation
    enable_correlation: bool = False
    correlation_window: int = 30
    correlation_threshold: float = 0.7
    correlation_high_corr_scale: float = 0.5

    # regime gate (M5.X)
    enable_regime_gate: bool = False
    # 必须声明策略类型让 gate 知道往哪缩——momentum / reversal / neutral
    regime_strategy_kind: StrategyKind = "neutral"

    # trade-rate circuit breaker (churn guard, 2026-05-31)
    # 滚动窗口内放行的 entry 数超 max_trades → REJECT,防 fee-churn blowup
    # (2026-05-25 stat_arb 单日 5501 单 -550 fee 未触发 drawdown)。
    enable_trade_rate: bool = False
    trade_rate_max_trades: int = 60
    trade_rate_window_sec: int = 3600


@dataclass(frozen=True, slots=True)
class RiskHandles:
    """暴露给策略的喂数据 handle。

    策略通过 ``handles.vol_target.update_return(...)`` 等方法更新各 check 的内部
    状态。任何 handle 可能为 None（对应的 check 没启用）。
    """

    vol_target: VolTargetCheck | None = None
    kelly: KellyCheck | None = None
    drawdown_tracker: DrawdownTracker | None = None
    correlation: CorrelationCheck | None = None
    # regime detector 由调用方传入（``live_node`` 单例 + 注入），所有启用
    # regime gate 的策略共享同一引用
    regime_detector: RegimeDetectorProtocol | None = None
    # account-level DD tracker 单例 (共享):由 ``LiveMonitor`` 持有,通过
    # ``build_risk_manager`` 的 ``account_drawdown_tracker`` 参数注入。
    account_drawdown_tracker: AccountDrawdownTracker | None = None


def build_risk_manager(
    config: RiskConfig | None,
    *,
    regime_detector: RegimeDetectorProtocol | None = None,
    account_drawdown_tracker: AccountDrawdownTracker | None = None,
) -> tuple[RiskManager | None, RiskHandles]:
    """根据 config 构造 ``RiskManager`` + handles。

    Args:
        config: ``RiskConfig`` 或 None；后者完全跳过风控（返回 ``(None, RiskHandles())``）。
        regime_detector: 可选 ``RegimeDetector`` 实例。当 ``config.enable_regime_gate``
            为 True 且 detector 非空时，追加 ``RegimeGateCheck`` 到 pipeline。多个
            策略应共享同一 detector 实例（``live_node`` 启动时构造）。
        account_drawdown_tracker: 可选共享 ``AccountDrawdownTracker`` 单例。非 None
            时,前置 ``AccountDrawdownCheck`` 到 pipeline (优先级最高);该 tracker
            的状态由 ``LiveMonitor`` 统一推送 OKX totalEq 更新,任一策略命中后
            所有策略都被 kill-switch。即使 ``config is None`` 也会注入。

    Returns:
        ``(manager, handles)``。``manager`` 在所有 check 都关闭时也是 None
        （省一次 pipeline 调用）。
    """
    if config is None:
        if account_drawdown_tracker is not None:
            handles = RiskHandles(
                regime_detector=regime_detector,
                account_drawdown_tracker=account_drawdown_tracker,
            )
            return (
                RiskManager(checks=[AccountDrawdownCheck(account_drawdown_tracker)]),
                handles,
            )
        return None, RiskHandles(regime_detector=regime_detector)

    checks: list[RiskCheck] = []
    handles_kwargs: dict[str, Any] = {}

    # Account-level kill-switch goes first so it short-circuits the rest.
    if account_drawdown_tracker is not None:
        checks.append(AccountDrawdownCheck(account_drawdown_tracker))
        handles_kwargs["account_drawdown_tracker"] = account_drawdown_tracker

    if config.enable_vol_target:
        vt = VolTargetCheck(
            target_vol_annualized=config.vol_target_annualized,
            window=config.vol_window,
            max_scale=config.vol_max_scale,
            min_scale=config.vol_min_scale,
            periods_per_year=config.vol_periods_per_year,
        )
        checks.append(vt)
        handles_kwargs["vol_target"] = vt

    if config.enable_kelly:
        k = KellyCheck(
            win_rate=config.kelly_win_rate,
            avg_r=config.kelly_avg_r,
            fraction=config.kelly_fraction,
            max_fraction=config.kelly_max_fraction,
        )
        checks.append(k)
        handles_kwargs["kelly"] = k

    if config.enable_drawdown:
        tracker = DrawdownTracker(
            daily_threshold_pct=config.drawdown_daily_pct,
            weekly_threshold_pct=config.drawdown_weekly_pct,
        )
        checks.append(DrawdownCheck(tracker))
        handles_kwargs["drawdown_tracker"] = tracker

    if config.enable_correlation:
        c = CorrelationCheck(
            window=config.correlation_window,
            threshold=config.correlation_threshold,
            high_corr_scale=config.correlation_high_corr_scale,
        )
        checks.append(c)
        handles_kwargs["correlation"] = c

    if config.enable_regime_gate and regime_detector is not None:
        checks.append(RegimeGateCheck(
            detector=regime_detector,
            strategy_kind=config.regime_strategy_kind,
        ))
    handles_kwargs["regime_detector"] = regime_detector

    # 频率熔断放最后:只统计"通过其它所有 check"的 entry(早退的 REJECT 不计)。
    if config.enable_trade_rate:
        checks.append(TradeRateCheck(
            max_trades=config.trade_rate_max_trades,
            window_sec=config.trade_rate_window_sec,
        ))

    handles = RiskHandles(**handles_kwargs)
    if not checks:
        return None, handles
    return RiskManager(checks=checks), handles


def apply_risk_manager(
    strategy: Strategy | None,
    manager: RiskManager | None,
    intent: RiskIntent,
) -> float | None:
    """运行 risk pipeline 并返回最终下单 size。

    Args:
        strategy: NT Strategy 实例（仅用 ``self.log`` 写日志；测试时可传 None
            或一个有 ``log`` 属性的 stub）。
        manager: ``RiskManager`` 或 None（None 表示无风控，原仓位通过）。
        intent: 策略提交的 ``RiskIntent``。

    Returns:
        - 浮点 size：APPROVE 走 ``intent.size``，SCALE 走 ``result.scaled_size``；
        - ``None``：被 REJECT，策略应跳过下单。

    日志：
        - REJECT → ``strategy.log.warning``
        - SCALE  → ``strategy.log.info``
        - APPROVE → 无（避免噪声）
    """
    if manager is None:
        return intent.size
    result = manager.check_all(intent)
    if result.action == RiskAction.REJECT:
        if strategy is not None:
            strategy.log.warning(
                f"risk REJECT [{result.check_name}] intent={intent.size:.4f}: {result.reason}"
            )
        return None
    if result.action == RiskAction.SCALE:
        if strategy is not None:
            strategy.log.info(
                f"risk SCALE [{result.check_name}] {intent.size:.4f} → "
                f"{result.scaled_size:.4f}: {result.reason}"
            )
        return result.scaled_size
    # APPROVE
    return intent.size


__all__ = [
    "RiskConfig",
    "RiskHandles",
    "apply_risk_manager",
    "build_risk_manager",
]
