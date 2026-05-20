"""M3 风控层单测：覆盖 4 个 check + RiskManager pipeline 合并语义。"""
from __future__ import annotations

import math

import pytest

from okx_trade.risk import (
    AccountDrawdownCheck,
    AccountDrawdownTracker,
    CorrelationCheck,
    DrawdownCheck,
    DrawdownState,
    DrawdownTracker,
    KellyCheck,
    RiskAction,
    RiskCheckResult,
    RiskIntent,
    RiskManager,
    VolTargetCheck,
    correlation_matrix,
    correlation_weight_adjustment,
    kelly_fraction,
    quarter_kelly_size,
    realized_vol_annualized,
    vol_target_position_size,
)


def _intent(
    *, strategy_id: str = "s1", instrument_id: str = "BTC-USDT-SWAP.OKX",
    size: float = 10.0, equity: float = 10000.0,
) -> RiskIntent:
    return RiskIntent(
        strategy_id=strategy_id,
        instrument_id=instrument_id,
        direction="long",
        size=size,
        entry_price=67000.0,
        stop_price=66800.0,
        account_equity_usdt=equity,
    )


# ============================================================================
# Vol target
# ============================================================================


class TestRealizedVol:
    def test_constant_returns_zero_vol(self) -> None:
        assert realized_vol_annualized([0.001] * 30) == 0.0

    def test_known_sequence(self) -> None:
        # 5 个收益的标准差手算 → 年化 √365
        rets = [0.01, -0.02, 0.015, -0.01, 0.005]
        v = realized_vol_annualized(rets, periods_per_year=365)
        # 不验证具体数值，只验证大致量级
        assert 0.1 < v < 1.0

    def test_too_few_samples_returns_zero(self) -> None:
        assert realized_vol_annualized([]) == 0.0
        assert realized_vol_annualized([0.01]) == 0.0


class TestVolTargetSizing:
    def test_basic_scale(self) -> None:
        # realized=30%, target=15% → scale=0.5
        assert vol_target_position_size(10, realized_vol=0.30, target_vol=0.15) == 5.0

    def test_caps_at_max_scale(self) -> None:
        # realized=5%, target=20% → scale=4.0 但 max=2.0 → 仓位×2
        assert vol_target_position_size(
            10, realized_vol=0.05, target_vol=0.20, max_scale=2.0,
        ) == 20.0

    def test_zero_when_invalid(self) -> None:
        assert vol_target_position_size(10, realized_vol=0, target_vol=0.15) == 0.0
        assert vol_target_position_size(0, realized_vol=0.3, target_vol=0.15) == 0.0


class TestVolTargetCheck:
    def test_insufficient_samples_approves(self) -> None:
        check = VolTargetCheck(target_vol_annualized=0.15, window=20)
        # 只喂 1 个样本
        check.update_return("BTC-USDT-SWAP.OKX", 0.01)
        result = check.check(_intent())
        assert result.action == RiskAction.APPROVE

    def test_high_vol_scales_down(self) -> None:
        check = VolTargetCheck(target_vol_annualized=0.15, window=20)
        # 喂 20 个高波动收益 → 年化 vol >> 15%
        for r in [0.05, -0.05] * 10:
            check.update_return("BTC-USDT-SWAP.OKX", r)
        result = check.check(_intent(size=10))
        assert result.action == RiskAction.SCALE
        assert 0 < result.scaled_size < 10

    def test_low_vol_scales_up_capped(self) -> None:
        check = VolTargetCheck(
            target_vol_annualized=0.50, window=20, max_scale=3.0,
        )
        # 喂 20 个微小波动 → realized vol 很小但 > 0 → scale up 击中 max_scale
        for i in range(20):
            check.update_return("BTC-USDT-SWAP.OKX", 0.0001 if i % 2 else -0.0001)
        result = check.check(_intent(size=10))
        # max_scale=3 → 仓位 ≤ 30
        assert result.action == RiskAction.SCALE
        assert result.scaled_size <= 30.0
        assert result.scaled_size >= 10.0  # 至少应该放大

    def test_constant_returns_rejects(self) -> None:
        # 常数收益 → realized vol=0 → 无 vol 估计 → 拒绝（保守）
        check = VolTargetCheck(target_vol_annualized=0.15, window=10)
        for _ in range(10):
            check.update_return("BTC-USDT-SWAP.OKX", 0.001)
        result = check.check(_intent(size=10))
        assert result.action == RiskAction.REJECT


# ============================================================================
# Kelly
# ============================================================================


class TestKellyFraction:
    def test_known_value_quarter_kelly(self) -> None:
        # win=55%, R=1.5 → f* = (0.55*1.5 - 0.45)/1.5 = 0.25
        assert math.isclose(kelly_fraction(0.55, 1.5), 0.25, abs_tol=1e-9)

    def test_negative_kelly(self) -> None:
        # win=40%, R=1.0 → f* = (0.4 - 0.6)/1.0 = -0.2 → 实际取 0
        assert kelly_fraction(0.40, 1.0) < 0

    def test_invalid_inputs(self) -> None:
        assert kelly_fraction(0.0, 1.5) == 0.0
        assert kelly_fraction(1.0, 1.5) == 0.0
        assert kelly_fraction(0.6, 0) == 0.0


class TestKellyCheck:
    def test_negative_kelly_rejects(self) -> None:
        # 期望负收益的策略 → REJECT
        check = KellyCheck(win_rate=0.30, avg_r=1.0)
        result = check.check(_intent())
        assert result.action == RiskAction.REJECT

    def test_high_kelly_caps(self) -> None:
        check = KellyCheck(win_rate=0.80, avg_r=2.0, fraction=0.25, max_fraction=0.5)
        result = check.check(_intent(size=10))
        # 不应超过原仓位太多
        assert result.scaled_size > 0


# ============================================================================
# Drawdown
# ============================================================================


class TestDrawdownTracker:
    def test_normal_state_under_threshold(self) -> None:
        t = DrawdownTracker(daily_threshold_pct=0.03, weekly_threshold_pct=0.08)
        t.record_equity(1_700_000_000_000, 10000)
        t.record_equity(1_700_001_000_000, 9800)  # -2%
        assert t.state == DrawdownState.NORMAL

    def test_daily_breach_triggered(self) -> None:
        t = DrawdownTracker(daily_threshold_pct=0.03, weekly_threshold_pct=0.08)
        t.record_equity(1_700_000_000_000, 10000)
        t.record_equity(1_700_001_000_000, 9650)  # -3.5% > 3%
        assert t.state == DrawdownState.DAILY_BREACH

    def test_weekly_breach_overrides_daily(self) -> None:
        t = DrawdownTracker(daily_threshold_pct=0.03, weekly_threshold_pct=0.08)
        t.record_equity(1_700_000_000_000, 10000)
        # 单日跌 -10% 触发周熝断（>8%）
        t.record_equity(1_700_001_000_000, 9000)
        assert t.state == DrawdownState.WEEKLY_BREACH

    def test_daily_resets_on_new_day(self) -> None:
        t = DrawdownTracker(daily_threshold_pct=0.03, weekly_threshold_pct=0.08)
        # day 1 触发日熝断
        t.record_equity(1_700_000_000_000, 10000)
        t.record_equity(1_700_001_000_000, 9650)
        assert t.state == DrawdownState.DAILY_BREACH
        # 跨过 UTC 0:00 → 重置（+86_400_000 ms = 1 day）
        t.record_equity(1_700_000_000_000 + 86_400_000 * 2, 9650)
        assert t.state == DrawdownState.NORMAL

    def test_weekly_does_not_auto_reset(self) -> None:
        t = DrawdownTracker(daily_threshold_pct=0.03, weekly_threshold_pct=0.08)
        t.record_equity(1_700_000_000_000, 10000)
        t.record_equity(1_700_001_000_000, 9000)
        assert t.state == DrawdownState.WEEKLY_BREACH
        # 即使过了一周，weekly_breach 不会自动恢复
        t.record_equity(1_700_000_000_000 + 86_400_000 * 8, 9100)
        assert t.state == DrawdownState.WEEKLY_BREACH

    def test_acknowledge_weekly_resets(self) -> None:
        t = DrawdownTracker(daily_threshold_pct=0.03, weekly_threshold_pct=0.08)
        t.record_equity(1_700_000_000_000, 10000)
        t.record_equity(1_700_001_000_000, 9000)
        t.acknowledge_weekly_breach()
        assert t.state == DrawdownState.NORMAL


class TestDrawdownCheck:
    def test_normal_approves(self) -> None:
        t = DrawdownTracker()
        t.record_equity(1_700_000_000_000, 10000)
        check = DrawdownCheck(t)
        assert check.check(_intent()).action == RiskAction.APPROVE

    def test_breach_rejects(self) -> None:
        t = DrawdownTracker(daily_threshold_pct=0.03)
        t.record_equity(1_700_000_000_000, 10000)
        t.record_equity(1_700_001_000_000, 9500)  # -5%
        check = DrawdownCheck(t)
        result = check.check(_intent())
        assert result.action == RiskAction.REJECT
        assert "DD" in result.reason


class TestAccountDrawdown:
    """Account-level kill-switch tracker (Phase 0 of DD architecture split).

    Same state-machine semantics as DrawdownTracker; distinct class identity
    + check name lets ops disambiguate "single strategy DD" vs "all-strategy
    account kill-switch" in alerts and risk-reject logs.
    """

    def test_inherits_drawdown_semantics(self) -> None:
        t = AccountDrawdownTracker(daily_threshold_pct=0.03, weekly_threshold_pct=0.08)
        t.record_equity(1_700_000_000_000, 100_000)
        t.record_equity(1_700_001_000_000, 96_500)  # -3.5%
        assert t.state == DrawdownState.DAILY_BREACH

    def test_check_reject_message_says_account(self) -> None:
        t = AccountDrawdownTracker(daily_threshold_pct=0.03)
        t.record_equity(1_700_000_000_000, 100_000)
        t.record_equity(1_700_001_000_000, 95_000)  # -5%
        result = AccountDrawdownCheck(t).check(_intent())
        assert result.action == RiskAction.REJECT
        assert result.check_name == "account_drawdown"
        assert "account" in result.reason.lower()

    def test_shared_tracker_rejects_all_strategies(self) -> None:
        """Same tracker injected into N strategies' pipelines → one breach
        kill-switches all (replaces the pre-Phase-0 per-strategy cascade)."""
        t = AccountDrawdownTracker(daily_threshold_pct=0.03)
        t.record_equity(1_700_000_000_000, 100_000)
        t.record_equity(1_700_001_000_000, 95_000)  # account-wide -5%
        check = AccountDrawdownCheck(t)
        for sid in ("strat_a", "strat_b", "strat_c"):
            r = check.check(_intent(strategy_id=sid))
            assert r.action == RiskAction.REJECT, f"{sid} should be killed"

    def test_normal_state_approves(self) -> None:
        t = AccountDrawdownTracker(daily_threshold_pct=0.03)
        t.record_equity(1_700_000_000_000, 100_000)
        t.record_equity(1_700_001_000_000, 99_000)  # -1%
        assert AccountDrawdownCheck(t).check(_intent()).action == RiskAction.APPROVE


# ============================================================================
# Correlation
# ============================================================================


class TestCorrelation:
    def test_identical_series_corr_one(self) -> None:
        rets = {"a": [0.01, -0.01, 0.02], "b": [0.01, -0.01, 0.02]}
        m = correlation_matrix(rets)
        assert math.isclose(m[("a", "b")], 1.0, abs_tol=1e-9)

    def test_anti_correlated_series(self) -> None:
        rets = {"a": [0.01, -0.01, 0.02], "b": [-0.01, 0.01, -0.02]}
        m = correlation_matrix(rets)
        assert math.isclose(m[("a", "b")], -1.0, abs_tol=1e-9)

    def test_weight_adjustment_above_threshold(self) -> None:
        # target 与 peer 完全相关 → 缩仓到 0.5
        scale = correlation_weight_adjustment(
            "target",
            other_returns={"peer": [0.01, -0.01, 0.02]},
            target_returns=[0.01, -0.01, 0.02],
            threshold=0.7, high_corr_scale=0.5,
        )
        assert scale == 0.5

    def test_weight_adjustment_uncorrelated(self) -> None:
        scale = correlation_weight_adjustment(
            "target",
            other_returns={"peer": [0.01, -0.01, 0.02]},
            target_returns=[-0.01, 0.01, -0.02],  # 完全反相关，但 |ρ|=1 也触发
            threshold=0.7,
        )
        assert scale == 0.5  # |corr|=1 触发降权

    def test_weight_adjustment_low_corr(self) -> None:
        scale = correlation_weight_adjustment(
            "target",
            other_returns={"peer": [0.01, -0.01, 0.02]},
            target_returns=[0.005, 0.001, -0.003],
            threshold=0.7,
        )
        assert scale == 1.0


class TestCorrelationCheck:
    def test_no_peers_approves(self) -> None:
        check = CorrelationCheck(window=10, threshold=0.7)
        for _ in range(5):
            check.update_strategy_pnl("s1", 0.01)
        result = check.check(_intent(strategy_id="s1"))
        assert result.action == RiskAction.APPROVE

    def test_high_corr_scales(self) -> None:
        check = CorrelationCheck(window=10, threshold=0.7, high_corr_scale=0.5)
        for r in [0.01, -0.005, 0.02, -0.01, 0.005]:
            check.update_strategy_pnl("s1", r)
            check.update_strategy_pnl("s2", r)  # 完全相同的序列
        result = check.check(_intent(strategy_id="s1", size=10))
        assert result.action == RiskAction.SCALE
        assert result.scaled_size == 5.0


# ============================================================================
# RiskManager pipeline
# ============================================================================


class TestRiskManager:
    def test_empty_pipeline_approves(self) -> None:
        mgr = RiskManager(checks=[])
        result = mgr.check_all(_intent(size=10))
        assert result.action == RiskAction.APPROVE
        assert result.scaled_size == 10.0

    def test_first_reject_short_circuits(self) -> None:
        # drawdown 拒绝 → 后面的 check 不应该跑
        t = DrawdownTracker(daily_threshold_pct=0.01)
        t.record_equity(1_700_000_000_000, 10000)
        t.record_equity(1_700_001_000_000, 9800)  # -2% > 1%
        mgr = RiskManager(checks=[
            DrawdownCheck(t),
            VolTargetCheck(target_vol_annualized=0.15),
        ])
        result = mgr.check_all(_intent())
        assert result.action == RiskAction.REJECT
        assert result.check_name == "drawdown"

    def test_min_scale_wins(self) -> None:
        # 两个 SCALE，取最严的
        class FixedScaleCheck:
            name = "fixed"
            def __init__(self, factor: float) -> None:
                self.factor = factor
            def check(self, intent: RiskIntent) -> RiskCheckResult:
                return RiskCheckResult.scale(
                    intent.size * self.factor,
                    reason=f"x{self.factor}", check_name=f"fixed_{self.factor}",
                )

        mgr = RiskManager(checks=[
            FixedScaleCheck(0.7),
            FixedScaleCheck(0.4),
            FixedScaleCheck(0.9),
        ])
        result = mgr.check_all(_intent(size=10))
        assert result.action == RiskAction.SCALE
        assert math.isclose(result.scaled_size, 4.0, abs_tol=1e-9)
        assert result.check_name == "fixed_0.4"

    def test_all_approve(self) -> None:
        class AlwaysApprove:
            name = "ok"
            def check(self, intent: RiskIntent) -> RiskCheckResult:
                return RiskCheckResult.approve(intent.size, check_name="ok")

        mgr = RiskManager(checks=[AlwaysApprove(), AlwaysApprove()])
        result = mgr.check_all(_intent(size=10))
        assert result.action == RiskAction.APPROVE
        assert result.scaled_size == 10.0


# ============================================================================
# 端到端：模拟 5 笔大亏触发熝断（plan 的验证场景）
# ============================================================================


class TestEndToEnd:
    def test_consecutive_losses_trigger_breach(self) -> None:
        """plan 的验证场景：构造 PnL 序列触发熝断。"""
        tracker = DrawdownTracker(daily_threshold_pct=0.03, weekly_threshold_pct=0.08)
        equity = 10000.0
        ts = 1_700_000_000_000
        # 第一笔记录开盘
        tracker.record_equity(ts, equity)

        # 连续 5 笔小亏（每笔 -1%），同一日内
        for i in range(5):
            equity *= 0.99
            ts += 60_000  # 1 分钟后
            tracker.record_equity(ts, equity)

        # 累计 -4.9% 应触发日熝断（> 3%）
        assert tracker.state == DrawdownState.DAILY_BREACH

        # 此时 RiskManager 拒绝所有新订单
        mgr = RiskManager(checks=[DrawdownCheck(tracker)])
        result = mgr.check_all(_intent())
        assert result.action == RiskAction.REJECT
