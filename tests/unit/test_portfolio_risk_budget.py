"""M5 RiskBudgetingAllocator 单测。"""
from __future__ import annotations

from decimal import Decimal

from okx_trade.pnl import EquitySnapshot, PnLTracker
from okx_trade.portfolio import (
    RiskBudgetingAllocator,
    correlation_penalty,
    inverse_vol_weights,
    risk_budget_weights,
)


def _populate_equities(
    tracker: PnLTracker, sid: str, daily_rets: list[float],
    *, base_eq: float = 10000.0, base_ts: int = 1_700_000_000_000,
) -> None:
    """根据指定的日收益序列写 equity 快照。"""
    eq = base_eq
    tracker.record_equity(EquitySnapshot(
        strategy_id=sid, ts_ms=base_ts, equity_usdt=Decimal(str(eq)),
    ))
    for i, r in enumerate(daily_rets):
        eq = eq * (1 + r)
        tracker.record_equity(EquitySnapshot(
            strategy_id=sid, ts_ms=base_ts + (i + 1) * 86_400_000,
            equity_usdt=Decimal(str(eq)),
        ))


# ---------------------------------------------------------------------------
# inverse_vol_weights
# ---------------------------------------------------------------------------
def test_inverse_vol_balanced_two_strategies() -> None:
    # 两个策略 std 相同 → 1/2 each
    rets = {
        "s1": [0.01, -0.01, 0.01, -0.01, 0.01],
        "s2": [0.01, -0.01, 0.01, -0.01, 0.01],
    }
    out = inverse_vol_weights(rets)
    assert abs(out["s1"] - 0.5) < 1e-9
    assert abs(out["s2"] - 0.5) < 1e-9


def test_inverse_vol_high_vol_gets_lower_weight() -> None:
    rets = {
        "low": [0.001, -0.001, 0.001, -0.001, 0.001],
        "high": [0.05, -0.05, 0.05, -0.05, 0.05],
    }
    out = inverse_vol_weights(rets)
    assert out["low"] > out["high"]


def test_inverse_vol_zero_variance_returns_all_zero() -> None:
    rets = {"s1": [0.0, 0.0, 0.0], "s2": [0.0, 0.0, 0.0]}
    out = inverse_vol_weights(rets)
    assert all(v == 0.0 for v in out.values())


def test_inverse_vol_short_series_zero() -> None:
    # 单点不能算 std
    rets = {"s1": [0.01]}
    out = inverse_vol_weights(rets)
    assert out["s1"] == 0.0


# ---------------------------------------------------------------------------
# correlation_penalty
# ---------------------------------------------------------------------------
def test_correlation_penalty_uncorrelated_returns_full_weight() -> None:
    rets = {
        "s1": [0.01, -0.01, 0.01, -0.01, 0.01],
        "s2": [-0.01, 0.01, -0.01, 0.01, -0.01],   # 反相关
    }
    out = correlation_penalty(rets)
    # |ρ|=1 → penalty=0；说明高相关被严重压制
    assert out["s1"] < 0.1


def test_correlation_penalty_uncorrelated_full_pass() -> None:
    # 用伪随机生成真正弱相关的两组序列
    import random
    rng = random.Random(7)
    rets = {
        "s1": [rng.gauss(0, 0.01) for _ in range(50)],
        "s2": [rng.gauss(0, 0.01) for _ in range(50)],
    }
    out = correlation_penalty(rets)
    # 弱相关 → penalty 应该 > 0.7
    assert out["s1"] > 0.7


def test_correlation_penalty_single_strategy_returns_one() -> None:
    out = correlation_penalty({"s1": [0.01, 0.02]})
    assert out == {"s1": 1.0}


# ---------------------------------------------------------------------------
# risk_budget_weights
# ---------------------------------------------------------------------------
def test_risk_budget_weights_normalized() -> None:
    rets = {
        "s1": [0.01, -0.01, 0.005, 0.01, -0.005],
        "s2": [0.005, 0.005, -0.01, 0.005, 0.01],
    }
    out = risk_budget_weights(rets)
    assert out is not None
    assert abs(sum(out.values()) - 1.0) < 1e-9


def test_risk_budget_weights_zero_vol_returns_none() -> None:
    rets = {"s1": [0.0, 0.0], "s2": [0.0, 0.0]}
    out = risk_budget_weights(rets)
    assert out is None


# ---------------------------------------------------------------------------
# RiskBudgetingAllocator (full pipeline)
# ---------------------------------------------------------------------------
def test_risk_budget_allocator_falls_back_to_equal_when_history_short() -> None:
    tracker = PnLTracker(":memory:")
    _populate_equities(tracker, "s1", [0.01] * 5)
    _populate_equities(tracker, "s2", [0.01] * 5)
    alloc = RiskBudgetingAllocator(min_history_days=30)
    out = alloc.allocate(
        ["s1", "s2"], tracker, total_equity_usdt=Decimal("10000"),
    )
    # 历史不足 → equal weight
    assert out["s1"] == Decimal("5000")
    assert out["s2"] == Decimal("5000")
    tracker.close()


def test_risk_budget_allocator_uses_risk_budget_when_history_sufficient() -> None:
    import random
    rng = random.Random(42)
    tracker = PnLTracker(":memory:")
    # s1 高波动，s2 低波动，60 日数据
    s1_rets = [rng.gauss(0.0005, 0.02) for _ in range(60)]
    s2_rets = [rng.gauss(0.0005, 0.005) for _ in range(60)]
    _populate_equities(tracker, "s1", s1_rets)
    _populate_equities(tracker, "s2", s2_rets)

    alloc = RiskBudgetingAllocator(min_history_days=30)
    out = alloc.allocate(
        ["s1", "s2"], tracker, total_equity_usdt=Decimal("10000"),
    )
    # 高波动 s1 应该拿到较少
    assert out["s1"] < out["s2"]
    assert sum(out.values()) == Decimal("10000")
    tracker.close()


def test_risk_budget_allocator_single_strategy_gets_all() -> None:
    tracker = PnLTracker(":memory:")
    alloc = RiskBudgetingAllocator(min_history_days=30)
    out = alloc.allocate(
        ["s1"], tracker, total_equity_usdt=Decimal("10000"),
    )
    assert out == {"s1": Decimal("10000")}
    tracker.close()


def test_risk_budget_allocator_zero_vol_falls_back_to_equal() -> None:
    tracker = PnLTracker(":memory:")
    # 每日完全相同 → daily_returns 全零 → std=0 → 回退
    _populate_equities(tracker, "s1", [0.0] * 60)
    _populate_equities(tracker, "s2", [0.0] * 60)
    alloc = RiskBudgetingAllocator(min_history_days=30)
    out = alloc.allocate(
        ["s1", "s2"], tracker, total_equity_usdt=Decimal("10000"),
    )
    assert out["s1"] == Decimal("5000")
    assert out["s2"] == Decimal("5000")
    tracker.close()
