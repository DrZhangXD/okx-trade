"""把 ``configs/live.yaml`` + PnL tracker + Allocator 组装成 NT ``TradingNode``。

调用流程
--------
1. 解析 ``live.yaml``：账户 equity、enabled 策略 + 各自 yaml 路径、风控默认值；
2. ``allocator.allocate(strategy_ids, tracker, total_equity)`` → 每策略 USDT 配额；
3. 把配额注入每个策略的 ``account_equity_usdt`` 字段；
4. 构造 ``TradingNodeConfig``（data_clients + exec_clients = OKX）；
5. ``TradingNode(config).build()``；
6. 为每个策略实例化 strategy + ``_pnl_tracker = tracker`` → ``trader.add_strategy``；
7. 构造 ``LiveMonitor`` —— 收集所有 strategy 的 ``_risk_handles``；
8. 返回 ``LiveContext``（持有 node / tracker / monitor / strategies），调用方
   决定 ``await monitor.run()`` + ``node.run()`` 的 lifecycle。

关键解耦
--------
- 这个 module 只 import NT 的入口（``TradingNode`` / ``StrategyConfig``）；
- 策略类通过 yaml 中的 ``type`` 字段查表（``_STRATEGY_REGISTRY``），用户日后加策略不需要改这里 —— 在 registry 加一行即可。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..monitor import (
    AlertSink,
    DailyReporter,
    JsonlSink,
    LiveMonitor,
    LogSink,
    MonitorThresholds,
)
from ..pnl import PnLTracker
from ..portfolio import Allocator, EqualWeightAllocator, RiskBudgetingAllocator
from ..risk.integration import RiskConfig, RiskHandles

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# 策略注册表（type 字段 → (Config 类, Strategy 类)）
# 在 NT 不可用环境（``[strategy]`` extra 未装）下不导入这些类
# ---------------------------------------------------------------------------
def _strategy_registry() -> dict[str, tuple[Any, Any]]:
    from ..strategies.funding_carry import FundingCarryConfig, FundingCarryStrategy
    from ..strategies.liq_reversal import LiqReversalConfig, LiqReversalStrategy
    from ..strategies.range_breakout import (
        RangeBreakoutConfig,
        RangeBreakoutStrategy,
    )
    from ..strategies.xs_momentum import XSMomentumConfig, XSMomentumStrategy
    return {
        "range_breakout": (RangeBreakoutConfig, RangeBreakoutStrategy),
        "funding_carry": (FundingCarryConfig, FundingCarryStrategy),
        "xs_momentum": (XSMomentumConfig, XSMomentumStrategy),
        "liq_reversal": (LiqReversalConfig, LiqReversalStrategy),
    }


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------
@dataclass
class LiveContext:
    """``build_live_context`` 的返回值。

    Attributes:
        node: 构建好的 NT ``TradingNode``（已 ``build()``）。
        tracker: PnL 持久化。
        allocator: 当前 allocator 实例。
        strategies: ``{name: strategy_instance}``——便于测试时检查。
        monitor: ``LiveMonitor`` 实例（外部 spawn ``run()`` task）。
        reporter: ``DailyReporter``（外部按需 ``write_for_today()``）。
        sinks: alert sink 列表（外部可在运行时 ``append`` 新 sink）。
    """

    node: Any | None
    tracker: PnLTracker
    allocator: Allocator
    strategies: dict[str, Any] = field(default_factory=dict)
    monitor: LiveMonitor | None = None
    reporter: DailyReporter | None = None
    sinks: list[AlertSink] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _build_allocator(live_cfg: dict[str, Any]) -> Allocator:
    """根据 live_cfg 的 ``portfolio_optimizer.mode`` 选 allocator。"""
    cfg = live_cfg.get("portfolio_optimizer", {}) or {}
    mode = cfg.get("mode", "equal")
    if mode == "risk_budget":
        return RiskBudgetingAllocator(
            min_history_days=int(cfg.get("min_history_days", 30)),
        )
    return EqualWeightAllocator()


def _build_sinks(live_cfg: dict[str, Any]) -> list[AlertSink]:
    """构造 alert sinks 列表（按 yaml 配置；未配则只装 LogSink）。"""
    cfg = (live_cfg.get("alerts") or {}).get("sinks") or [{"type": "log"}]
    out: list[AlertSink] = []
    for s in cfg:
        t = s.get("type")
        if t == "log":
            out.append(LogSink(s.get("logger_name", "okx_trade.alerts")))
        elif t == "jsonl":
            out.append(JsonlSink(s.get("path", "var/alerts.jsonl")))
        elif t == "telegram":
            # 故意不实接：留 hook 给 M6
            from ..monitor.alerts import TelegramSink
            out.append(TelegramSink())
        else:
            raise ValueError(f"unknown alert sink type: {t!r}")
    return out


def _resolve_strategy_specs(
    live_cfg: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any]]]:
    """返回 ``[(name, type, raw_cfg), ...]``，仅 enabled 的策略。"""
    out: list[tuple[str, str, dict[str, Any]]] = []
    for name, entry in (live_cfg.get("strategies") or {}).items():
        if not entry.get("enabled", False):
            continue
        cfg_path = entry.get("config")
        if not cfg_path:
            raise ValueError(f"strategy {name!r}: missing config path")
        raw = _load_yaml(cfg_path)
        # 优先取 entry.type；否则按 name 默认
        s_type = entry.get("type", name)
        out.append((name, s_type, raw))
    return out


def _merge_risk_defaults(
    raw: dict[str, Any], risk_defaults: dict[str, Any] | None,
) -> dict[str, Any]:
    """策略 yaml 的 ``risk:`` 字段合并全局 ``risk_defaults`` —— 策略侧优先。"""
    if not risk_defaults:
        return raw
    raw_risk = (raw.get("risk") or {}).copy()
    merged = {**risk_defaults, **raw_risk}
    raw["risk"] = merged
    return raw


def _build_risk_config(d: dict[str, Any] | None) -> RiskConfig | None:
    if not d:
        return None
    fields = {
        f for f in (
            "enable_vol_target", "vol_target_annualized", "vol_window",
            "vol_max_scale", "vol_min_scale", "vol_periods_per_year",
            "enable_kelly", "kelly_win_rate", "kelly_avg_r",
            "kelly_fraction", "kelly_max_fraction",
            "enable_drawdown", "drawdown_daily_pct", "drawdown_weekly_pct",
            "enable_correlation", "correlation_window",
            "correlation_threshold", "correlation_high_corr_scale",
        )
    }
    kw = {k: v for k, v in d.items() if k in fields}
    return RiskConfig(**kw)


# ---------------------------------------------------------------------------
# build_live_context（主入口）
# ---------------------------------------------------------------------------
def build_live_context(
    live_cfg: dict[str, Any] | str | Path,
    *,
    pnl_tracker: PnLTracker | None = None,
    allocator: Allocator | None = None,
    build_node: bool = True,
) -> LiveContext:
    """主入口：从 ``live_cfg``（dict 或 yaml 路径）装配 ``LiveContext``。

    Args:
        live_cfg: ``dict`` 或 yaml 路径。
        pnl_tracker: 可注入；不传则按 yaml ``pnl_db`` 字段构造（默认 ``var/pnl.sqlite``）。
        allocator: 可注入；不传则按 yaml ``portfolio_optimizer.mode`` 构造。
        build_node: ``False`` 时不构造 NT ``TradingNode``（用于单测——避免依赖
            网络 / NT live engine 内部）；返回的 ``LiveContext.node`` 为 None。

    Returns:
        ``LiveContext``。``strategies`` 字典里每个策略的 ``_pnl_tracker`` 已设置。
    """
    if not isinstance(live_cfg, dict):
        live_cfg = _load_yaml(live_cfg)

    tracker = pnl_tracker or PnLTracker(live_cfg.get("pnl_db", "var/pnl.sqlite"))
    alloc = allocator or _build_allocator(live_cfg)

    account = live_cfg.get("account", {}) or {}
    total_equity = Decimal(str(account.get("equity_usdt", 10000.0)))

    risk_defaults = live_cfg.get("risk_defaults", {}) or {}
    specs = _resolve_strategy_specs(live_cfg)
    if not specs:
        raise ValueError("no enabled strategies in live_cfg")

    # 用策略 name 当 strategy_id 喂 allocator（NT live 模式下 self.id 才知道，
    # 这里先用 name 算 allocation；后续会覆盖到 config.account_equity_usdt）
    names = [name for name, _, _ in specs]
    allocations = alloc.allocate(names, tracker, total_equity_usdt=total_equity)

    strategies: dict[str, Any] = {}
    handles_map: dict[str, RiskHandles] = {}

    if build_node:
        node = _build_trading_node(live_cfg)
        registry = _strategy_registry()
        for name, s_type, raw in specs:
            raw = _merge_risk_defaults(raw, risk_defaults)
            cfg_cls, strat_cls = registry[s_type]
            # 注入 allocator 算出的 equity
            kwargs = {k: v for k, v in raw.items() if k != "risk"}
            kwargs["account_equity_usdt"] = float(allocations[name])
            kwargs["risk_config"] = _build_risk_config(raw.get("risk"))
            cfg = cfg_cls(**kwargs)
            strategy = strat_cls(cfg)
            strategy._pnl_tracker = tracker
            node.trader.add_strategy(strategy)
            strategies[name] = strategy
            handles_map[name] = strategy._risk_handles
    else:
        node = None
        # 在 build_node=False 下也要让测试能看到 allocations / specs，
        # 不实例化策略类（NT 重，且测试常常不需要）
        for name, _, raw in specs:
            strategies[name] = {
                "config": raw,
                "allocated_equity_usdt": float(allocations[name]),
            }

    sinks = _build_sinks(live_cfg)
    monitor_cfg = live_cfg.get("monitor", {}) or {}
    monitor = LiveMonitor(
        handles_map, sinks,
        poll_interval_s=int(monitor_cfg.get("poll_interval_s", 60)),
        thresholds=_build_thresholds(live_cfg.get("alerts", {}) or {}),
        heartbeat_path=monitor_cfg.get("heartbeat_path", "var/heartbeat.ts"),
    ) if handles_map else None

    reporter = DailyReporter(
        tracker,
        output_dir=monitor_cfg.get("daily_report_dir", "var/daily_reports"),
    )

    return LiveContext(
        node=node, tracker=tracker, allocator=alloc,
        strategies=strategies, monitor=monitor, reporter=reporter, sinks=sinks,
    )


def _build_thresholds(alerts_cfg: dict[str, Any]) -> MonitorThresholds:
    th = alerts_cfg.get("thresholds", {}) or {}
    return MonitorThresholds(
        kelly_jump=float(th.get("kelly_jump", 0.05)),
        correlation_max=float(th.get("correlation_max", 0.7)),
        reject_per_hour=int(th.get("reject_per_hour", 5)),
    )


def _build_trading_node(live_cfg: dict[str, Any]) -> Any:
    """构造 NT ``TradingNode``，注册 OKX 工厂，build。"""
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.live.config import LiveExecEngineConfig, TradingNodeConfig
    from nautilus_trader.live.node import TradingNode

    from ..adapter.config import OKXDataClientConfig, OKXExecClientConfig
    from ..adapter.factories import OKXLiveDataClientFactory, OKXLiveExecClientFactory

    account = live_cfg.get("account", {}) or {}
    is_paper = bool(account.get("paper_trading", True))
    creds = live_cfg.get("okx_credentials", {}) or {}

    # ``execution.reconciliation``（live.yaml）→ ``LiveExecEngineConfig``。
    # 默认 ``true``：M5 adapter 已实现 generate_*_reports，NT 启动时拉 OKX
    # 当前订单/持仓做对账。``lookback_mins`` 控制历史订单回查窗口，None=不限。
    exec_cfg = live_cfg.get("execution", {}) or {}
    exec_engine_kwargs: dict[str, Any] = {
        "reconciliation": bool(exec_cfg.get("reconciliation", True)),
    }
    lookback_mins = exec_cfg.get("reconciliation_lookback_mins")
    if lookback_mins is not None:
        exec_engine_kwargs["reconciliation_lookback_mins"] = int(lookback_mins)

    config = TradingNodeConfig(
        trader_id="OKX-TRADER-001",
        logging=LoggingConfig(log_level="INFO"),
        exec_engine=LiveExecEngineConfig(**exec_engine_kwargs),
        data_clients={
            "OKX": OKXDataClientConfig(
                api_key=creds.get("api_key"),
                api_secret=creds.get("api_secret"),
                passphrase=creds.get("passphrase"),
                is_demo=is_paper,
                http_proxy=creds.get("http_proxy"),
            ),
        },
        exec_clients={
            "OKX": OKXExecClientConfig(
                api_key=creds.get("api_key"),
                api_secret=creds.get("api_secret"),
                passphrase=creds.get("passphrase"),
                is_demo=is_paper,
                http_proxy=creds.get("http_proxy"),
                td_mode=exec_cfg.get("td_mode", "cross"),
                pos_side_mode=exec_cfg.get("pos_side_mode", "net"),
            ),
        },
    )
    node = TradingNode(config=config)
    node.add_data_client_factory("OKX", OKXLiveDataClientFactory)
    node.add_exec_client_factory("OKX", OKXLiveExecClientFactory)
    node.build()
    return node


__all__ = ["LiveContext", "build_live_context"]
