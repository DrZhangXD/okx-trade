"""M5 runtime/live_node 单测：build_live_context（不构 NT 节点的 dry-run 模式）。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from okx_trade.pnl import PnLTracker
from okx_trade.portfolio import EqualWeightAllocator, RiskBudgetingAllocator
from okx_trade.runtime import build_live_context


@pytest.fixture
def minimal_live_cfg(tmp_path: Path) -> dict:
    """两个 enabled 策略的最小可用 live_cfg。"""
    rb_cfg = tmp_path / "rb.yaml"
    rb_cfg.write_text(yaml.safe_dump({
        "instrument_id": "BTC-USDT-SWAP.OKX",
        "range_bar_type": "BTC-USDT-SWAP.OKX-1-DAY-LAST-EXTERNAL",
        "signal_bar_type": "BTC-USDT-SWAP.OKX-1-HOUR-LAST-EXTERNAL",
        "risk_pct": 0.005,
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
            "range_breakout": {"enabled": True, "config": str(rb_cfg)},
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
    assert set(ctx.strategies.keys()) == {"range_breakout", "funding_carry"}
    assert isinstance(ctx.allocator, EqualWeightAllocator)


def test_build_live_context_uses_risk_budget_when_configured(
    minimal_live_cfg: dict,
) -> None:
    minimal_live_cfg["portfolio_optimizer"] = {"mode": "risk_budget", "min_history_days": 30}
    ctx = build_live_context(minimal_live_cfg, build_node=False)
    assert isinstance(ctx.allocator, RiskBudgetingAllocator)


def test_build_live_context_allocates_equity_evenly(minimal_live_cfg: dict) -> None:
    ctx = build_live_context(minimal_live_cfg, build_node=False)
    eq_a = ctx.strategies["range_breakout"]["allocated_equity_usdt"]
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
