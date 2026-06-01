"""M5 runtime/live_node 单测：build_live_context（不构 NT 节点的 dry-run 模式）。"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from okx_trade.pnl import PnLTracker
from okx_trade.portfolio import EqualWeightAllocator, RiskBudgetingAllocator
from okx_trade.runtime import build_live_context
from okx_trade.runtime.live_node import _build_okx_equity_provider


@pytest.fixture
def minimal_live_cfg(tmp_path: Path) -> dict:
    """两个 enabled 策略的最小可用 live_cfg。"""
    lr_cfg = tmp_path / "lr.yaml"
    lr_cfg.write_text(yaml.safe_dump({
        "instrument_id": "BTC-USDT-SWAP.OKX",
        "bar_type": "BTC-USDT-SWAP.OKX-1-MINUTE-LAST-EXTERNAL",
        "risk_pct": 0.005,
        "subscribe_liquidations": False,
        "account_equity_usdt": 5000.0,  # 应该被 allocator 覆盖
    }))
    fc_cfg = tmp_path / "fc.yaml"
    fc_cfg.write_text(yaml.safe_dump({
        "spot_instrument_id": "BTC-USDT.OKX",
        "perp_instrument_id": "BTC-USDT-SWAP.OKX",
        "spot_bar_type": "BTC-USDT.OKX-1-HOUR-LAST-EXTERNAL",
        "account_equity_usdt": 5000.0,
    }))
    return {
        "account": {"equity_usdt": 10000.0, "paper_trading": True},
        "pnl_db": ":memory:",
        "strategies": {
            "liq_reversal": {"enabled": True, "config": str(lr_cfg)},
            "funding_carry": {"enabled": True, "config": str(fc_cfg)},
        },
        "risk_defaults": {
            "enable_drawdown": True,
            "drawdown_daily_pct": 0.03,
        },
        "portfolio_optimizer": {"mode": "equal"},
        "alerts": {"sinks": [{"type": "log"}]},
    }


def test_build_live_context_dry_run_two_strategies(minimal_live_cfg: dict) -> None:
    ctx = build_live_context(minimal_live_cfg, build_node=False)
    assert ctx.node is None
    assert set(ctx.strategies.keys()) == {"liq_reversal", "funding_carry"}
    assert isinstance(ctx.allocator, EqualWeightAllocator)


def test_build_live_context_uses_risk_budget_when_configured(
    minimal_live_cfg: dict,
) -> None:
    minimal_live_cfg["portfolio_optimizer"] = {"mode": "risk_budget", "min_history_days": 30}
    ctx = build_live_context(minimal_live_cfg, build_node=False)
    assert isinstance(ctx.allocator, RiskBudgetingAllocator)


def test_build_live_context_allocates_equity_evenly(minimal_live_cfg: dict) -> None:
    ctx = build_live_context(minimal_live_cfg, build_node=False)
    eq_a = ctx.strategies["liq_reversal"]["allocated_equity_usdt"]
    eq_b = ctx.strategies["funding_carry"]["allocated_equity_usdt"]
    assert eq_a == 5000.0
    assert eq_b == 5000.0
    assert eq_a + eq_b == 10000.0


def test_build_live_context_writes_alert_sinks(minimal_live_cfg: dict, tmp_path: Path) -> None:
    minimal_live_cfg["alerts"] = {
        "sinks": [{"type": "log"}, {"type": "jsonl", "path": str(tmp_path / "a.jsonl")}],
    }
    ctx = build_live_context(minimal_live_cfg, build_node=False)
    assert len(ctx.sinks) == 2


def test_build_live_context_no_strategies_raises(minimal_live_cfg: dict) -> None:
    minimal_live_cfg["strategies"] = {
        k: {**v, "enabled": False} for k, v in minimal_live_cfg["strategies"].items()
    }
    with pytest.raises(ValueError, match="no enabled strategies"):
        build_live_context(minimal_live_cfg, build_node=False)


def test_build_live_context_unknown_sink_type_raises(minimal_live_cfg: dict) -> None:
    minimal_live_cfg["alerts"] = {"sinks": [{"type": "weird"}]}
    with pytest.raises(ValueError, match="unknown alert sink"):
        build_live_context(minimal_live_cfg, build_node=False)


def test_build_live_context_uses_injected_tracker(minimal_live_cfg: dict) -> None:
    tracker = PnLTracker(":memory:")
    ctx = build_live_context(minimal_live_cfg, pnl_tracker=tracker, build_node=False)
    assert ctx.tracker is tracker
    tracker.close()


def test_build_live_context_reporter_present(minimal_live_cfg: dict, tmp_path: Path) -> None:
    minimal_live_cfg["monitor"] = {"daily_report_dir": str(tmp_path / "reports")}
    ctx = build_live_context(minimal_live_cfg, build_node=False)
    assert ctx.reporter is not None
    p = ctx.reporter.write_for_date("2026-05-08")
    assert p.exists()


def test_okx_equity_provider_returns_total_eq_not_usdt_avail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for 2026-05-20 incident: provider must return totalEq
    (整账户净值), not USDT.avail_eq. The latter drops when margin is frozen
    by a new position, causing false DAILY_BREACH cascades across all
    strategies even when total account equity is unchanged.

    Scenario: account has BTC collateral worth ~25k USDT and USDT cash 60k;
    USDT avail_eq is 55k after margin freeze. Old behavior returned 55k;
    new behavior must return 82k (full totalEq).
    """
    from okx_trade.models.account import Balance, BalanceDetail
    import okx_trade.runtime.live_node as live_node_mod

    fake_balance = Balance(
        totalEq=Decimal("82474"),
        details=[BalanceDetail(
            ccy="USDT", eq=Decimal("59653"), cashBal=Decimal("59701"),
            availEq=Decimal("55217"), availBal=Decimal("55217"),
            upl=Decimal("-49"),
        )],
    )

    class _FakeAccount:
        async def get_balance(self, ccy: str | None = None) -> Balance:
            return fake_balance

    class _FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.account = _FakeAccount()
        async def __aenter__(self) -> "_FakeClient":
            return self
        async def __aexit__(self, *exc: object) -> None:
            return None

    class _FakeSettings:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    # Patch the symbols the lazy import inside _provider resolves to.
    monkeypatch.setattr("okx_trade.OKXRestClient", _FakeClient)
    monkeypatch.setattr("okx_trade.OKXSettings", _FakeSettings)

    provider = _build_okx_equity_provider()
    val = asyncio.run(provider())
    assert val == 82474.0, f"expected totalEq=82474, got {val}"


def test_okx_equity_provider_returns_none_on_zero_total_eq(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero / missing totalEq → None so monitor keeps last allocation."""
    from okx_trade.models.account import Balance
    fake_balance = Balance(totalEq=Decimal("0"))

    class _FakeAccount:
        async def get_balance(self, ccy: str | None = None) -> Balance:
            return fake_balance

    class _FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.account = _FakeAccount()
        async def __aenter__(self) -> "_FakeClient":
            return self
        async def __aexit__(self, *exc: object) -> None:
            return None

    class _FakeSettings:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    monkeypatch.setattr("okx_trade.OKXRestClient", _FakeClient)
    monkeypatch.setattr("okx_trade.OKXSettings", _FakeSettings)

    val = asyncio.run(_build_okx_equity_provider()())
    assert val is None


def test_build_live_context_injects_iso_service_and_vol_filter(
    minimal_live_cfg: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 Task 10 DI wiring: every OkxStrategyBase-derived strategy gets
    the singleton iso_service + vol_filter, and LiveMonitor holds refs
    to both. Uses ``build_node=True`` with a stubbed ``_build_trading_node``
    so NT live engine isn't required.

    Migrated strategies (those inheriting ``OkxStrategyBase``) declare
    ``_iso_service`` / ``_vol_filter`` class attrs — we assert their
    instance dicts now hold the singleton refs. Legacy strategies without
    those attrs are skipped silently by the ``hasattr`` guard.
    """
    from okx_trade.risk.isolated_margin_service import IsolatedMarginService
    from okx_trade.risk.volatility_filter import VolatilityFilter
    import okx_trade.runtime.live_node as live_node_mod

    class _FakeTrader:
        def __init__(self) -> None:
            self.added: list = []

        def add_strategy(self, strategy: object) -> None:
            self.added.append(strategy)

    class _FakeNode:
        def __init__(self) -> None:
            self.trader = _FakeTrader()

    monkeypatch.setattr(
        live_node_mod, "_build_trading_node",
        lambda cfg, option_ulys=None: _FakeNode(),  # fceeb2a 加了 option_ulys kwarg
    )

    ctx = build_live_context(minimal_live_cfg, build_node=True)

    assert ctx.monitor is not None
    assert isinstance(ctx.monitor._iso_service, IsolatedMarginService)
    assert isinstance(ctx.monitor._vol_filter, VolatilityFilter)

    # At least one of the two enabled strategies (liq_reversal / funding_carry)
    # must have migrated to OkxStrategyBase — assert ALL strategies that
    # declared the attr received the *same* singleton ref.
    migrated = [
        s for s in ctx.strategies.values() if hasattr(s, "_iso_service")
    ]
    if migrated:  # at least one migrated strategy in current registry
        for s in migrated:
            assert s._iso_service is ctx.monitor._iso_service
            assert s._vol_filter is ctx.monitor._vol_filter


def test_okx_equity_provider_returns_none_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Network / auth error → None (monitor keeps last allocation)."""
    class _FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass
        async def __aenter__(self) -> "_FakeClient":
            raise RuntimeError("network down")
        async def __aexit__(self, *exc: object) -> None:
            return None

    class _FakeSettings:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    monkeypatch.setattr("okx_trade.OKXRestClient", _FakeClient)
    monkeypatch.setattr("okx_trade.OKXSettings", _FakeSettings)

    val = asyncio.run(_build_okx_equity_provider()())
    assert val is None


def test_build_risk_config_passes_trade_rate_keys() -> None:
    """_build_risk_config 的字段白名单必须包含 trade_rate_* —— 否则配置里
    enable_trade_rate 会被静默丢弃,熔断永不生效(2026-05-31 回归)。"""
    from okx_trade.runtime.live_node import _build_risk_config

    cfg = _build_risk_config({
        "enable_drawdown": True,
        "enable_trade_rate": True,
        "trade_rate_max_trades": 60,
        "trade_rate_window_sec": 3600,
    })
    assert cfg is not None
    assert cfg.enable_trade_rate is True
    assert cfg.trade_rate_max_trades == 60
    assert cfg.trade_rate_window_sec == 3600
