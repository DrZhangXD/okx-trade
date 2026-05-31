"""TradeRateCheck 单测：每策略下单频率熔断(churn guard)。

动机：2026-05-25 stat_arb_pairs 单日刷 5501 单,-550 USDT 全是手续费,但 -1.7%
未触发 3% daily drawdown。drawdown 看净值跌幅,挡不住"高频小亏 churn"。本 check
在下单频率层面兜底。
"""
from __future__ import annotations

import pytest

from okx_trade.risk import RiskConfig, build_risk_manager
from okx_trade.risk.base import RiskAction, RiskIntent
from okx_trade.risk.trade_rate import TradeRateCheck


def _intent(size: float = 10.0) -> RiskIntent:
    return RiskIntent(
        strategy_id="stat_arb_pairs",
        instrument_id="BTC-USDT-SWAP.OKX",
        direction="long",
        size=size,
        entry_price=70000.0,
        stop_price=69650.0,
        account_equity_usdt=3000.0,
    )


class _Clock:
    """可控时钟,返回毫秒。"""

    def __init__(self, t_ms: int = 0) -> None:
        self.t = t_ms

    def now(self) -> int:
        return self.t


def test_under_cap_approves_unchanged() -> None:
    clk = _Clock()
    chk = TradeRateCheck(max_trades=5, window_sec=60, now_ms=clk.now)
    for i in range(5):
        clk.t = i * 1000
        res = chk.check(_intent(size=10.0))
        assert res.action == RiskAction.APPROVE
        assert res.scaled_size == 10.0


def test_at_cap_rejects() -> None:
    clk = _Clock()
    chk = TradeRateCheck(max_trades=2, window_sec=60, now_ms=clk.now)
    assert chk.check(_intent()).action == RiskAction.APPROVE   # 1
    clk.t = 1000
    assert chk.check(_intent()).action == RiskAction.APPROVE   # 2
    clk.t = 2000
    res = chk.check(_intent())                                 # 3 → over cap
    assert res.action == RiskAction.REJECT
    assert res.check_name == "trade_rate"
    assert "2/2" in res.reason


def test_window_slides_allows_again() -> None:
    clk = _Clock()
    chk = TradeRateCheck(max_trades=2, window_sec=10, now_ms=clk.now)
    chk.check(_intent())          # t=0
    clk.t = 1000
    chk.check(_intent())          # t=1s
    clk.t = 2000
    assert chk.check(_intent()).action == RiskAction.REJECT    # full
    # 推进到窗口外:t=0 的那条(< 11000-10000=1000)被剪掉 → 只剩 t=1000 一条
    clk.t = 11000
    assert chk.check(_intent()).action == RiskAction.APPROVE


def test_invalid_params_raise() -> None:
    with pytest.raises(ValueError):
        TradeRateCheck(max_trades=0, window_sec=60)
    with pytest.raises(ValueError):
        TradeRateCheck(max_trades=5, window_sec=0)


def test_build_risk_manager_wires_trade_rate() -> None:
    cfg = RiskConfig(
        enable_trade_rate=True,
        trade_rate_max_trades=2,
        trade_rate_window_sec=3600,
    )
    manager, _handles = build_risk_manager(cfg)
    assert manager is not None
    assert any(c.name == "trade_rate" for c in manager.checks)
    # 第 3 笔被熔断
    assert manager.check_all(_intent()).action == RiskAction.APPROVE
    assert manager.check_all(_intent()).action == RiskAction.APPROVE
    assert manager.check_all(_intent()).action == RiskAction.REJECT
