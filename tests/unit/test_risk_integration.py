"""M3.6 集成层单测：``RiskConfig`` / ``build_risk_manager`` / ``apply_risk_manager``。

专注 helper 行为，不跑 NT live runtime（策略接入的端到端验证留 backtest 冲烟）。
"""
from __future__ import annotations

from okx_trade.risk import (
    AccountDrawdownTracker,
    DrawdownState,
    RiskAction,
    RiskCheckResult,
    RiskConfig,
    RiskHandles,
    RiskIntent,
    RiskManager,
    apply_risk_manager,
    build_risk_manager,
)

# ---------------------------------------------------------------------------
# 共用
# ---------------------------------------------------------------------------


def _intent(size: float = 10.0) -> RiskIntent:
    return RiskIntent(
        strategy_id="s1",
        instrument_id="BTC-USDT-SWAP.OKX",
        direction="long",
        size=size,
        entry_price=67000.0,
        stop_price=66800.0,
        account_equity_usdt=10000.0,
    )


class _DummyLog:
    """记录所有 log 调用，便于断言。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def info(self, msg: str) -> None:
        self.infos.append(msg)


class _DummyStrategy:
    def __init__(self) -> None:
        self.log = _DummyLog()


# ---------------------------------------------------------------------------
# build_risk_manager
# ---------------------------------------------------------------------------


class TestBuildRiskManager:
    def test_none_config_returns_none_manager(self) -> None:
        mgr, handles = build_risk_manager(None)
        assert mgr is None
        assert isinstance(handles, RiskHandles)
        assert handles.vol_target is None
        assert handles.drawdown_tracker is None

    def test_all_disabled_returns_none(self) -> None:
        # 所有 enable_* 默认 False
        mgr, handles = build_risk_manager(RiskConfig())
        assert mgr is None
        assert handles.vol_target is None
        assert handles.drawdown_tracker is None

    def test_only_drawdown(self) -> None:
        mgr, handles = build_risk_manager(RiskConfig(
            enable_drawdown=True, drawdown_daily_pct=0.05,
        ))
        assert mgr is not None
        assert len(mgr.checks) == 1
        assert handles.drawdown_tracker is not None
        assert handles.drawdown_tracker.daily_threshold_pct == 0.05
        # 其他 handle 仍为 None
        assert handles.vol_target is None
        assert handles.kelly is None
        assert handles.correlation is None

    def test_all_enabled(self) -> None:
        mgr, handles = build_risk_manager(RiskConfig(
            enable_vol_target=True, enable_kelly=True,
            enable_drawdown=True, enable_correlation=True,
        ))
        assert mgr is not None
        assert len(mgr.checks) == 4
        assert all([
            handles.vol_target,
            handles.kelly,
            handles.drawdown_tracker,
            handles.correlation,
        ])

    def test_kelly_param_passthrough(self) -> None:
        _mgr, handles = build_risk_manager(RiskConfig(
            enable_kelly=True,
            kelly_win_rate=0.6, kelly_avg_r=2.0, kelly_fraction=0.5,
        ))
        assert handles.kelly is not None
        assert handles.kelly.win_rate == 0.6
        assert handles.kelly.avg_r == 2.0
        assert handles.kelly.fraction == 0.5

    def test_account_drawdown_tracker_injected_with_none_config(self) -> None:
        """No per-strategy RiskConfig but account tracker still wired ⇒
        a single-check pipeline whose only gate is the kill-switch."""
        tracker = AccountDrawdownTracker()
        mgr, handles = build_risk_manager(None, account_drawdown_tracker=tracker)
        assert mgr is not None
        assert len(mgr.checks) == 1
        assert mgr.checks[0].name == "account_drawdown"
        assert handles.account_drawdown_tracker is tracker

    def test_account_drawdown_tracker_prepends_check(self) -> None:
        """When per-strategy checks AND account tracker are set, account
        check goes first so a breach short-circuits before any kelly/vol
        compute that might log noise."""
        tracker = AccountDrawdownTracker()
        mgr, handles = build_risk_manager(
            RiskConfig(enable_kelly=True, enable_drawdown=True),
            account_drawdown_tracker=tracker,
        )
        assert mgr is not None
        assert mgr.checks[0].name == "account_drawdown"
        assert handles.account_drawdown_tracker is tracker
        assert handles.kelly is not None
        assert handles.drawdown_tracker is not None

    def test_account_drawdown_kill_switch_rejects(self) -> None:
        tracker = AccountDrawdownTracker(daily_threshold_pct=0.03)
        tracker.record_equity(1_700_000_000_000, 100_000)
        tracker.record_equity(1_700_001_000_000, 95_000)  # -5%
        mgr, _ = build_risk_manager(None, account_drawdown_tracker=tracker)
        assert mgr is not None
        result = mgr.check_all(_intent(size=10.0))
        assert result.action == RiskAction.REJECT
        assert result.check_name == "account_drawdown"


# ---------------------------------------------------------------------------
# apply_risk_manager
# ---------------------------------------------------------------------------


class TestApplyRiskManager:
    def test_none_manager_returns_intent_size(self) -> None:
        size = apply_risk_manager(None, None, _intent(size=10))
        assert size == 10.0

    def test_none_manager_with_strategy_no_log(self) -> None:
        # 即使传了 strategy，None manager 也不写日志
        strat = _DummyStrategy()
        size = apply_risk_manager(strat, None, _intent(size=7))
        assert size == 7.0
        assert strat.log.warnings == []
        assert strat.log.infos == []

    def test_approve_no_log(self) -> None:
        class AlwaysApprove:
            name = "ok"
            def check(self, intent: RiskIntent) -> RiskCheckResult:
                return RiskCheckResult.approve(intent.size, check_name="ok")

        strat = _DummyStrategy()
        mgr = RiskManager(checks=[AlwaysApprove()])
        size = apply_risk_manager(strat, mgr, _intent(size=10))
        assert size == 10.0
        # APPROVE 不写日志（避免噪声）
        assert strat.log.infos == []
        assert strat.log.warnings == []

    def test_scale_logs_info(self) -> None:
        class AlwaysScale:
            name = "scale_half"
            def check(self, intent: RiskIntent) -> RiskCheckResult:
                return RiskCheckResult.scale(
                    intent.size * 0.5, reason="halved", check_name="scale_half",
                )

        strat = _DummyStrategy()
        mgr = RiskManager(checks=[AlwaysScale()])
        size = apply_risk_manager(strat, mgr, _intent(size=10))
        assert size == 5.0
        assert len(strat.log.infos) == 1
        assert "SCALE" in strat.log.infos[0]
        assert "scale_half" in strat.log.infos[0]
        assert strat.log.warnings == []

    def test_reject_returns_none_logs_warning(self) -> None:
        class AlwaysReject:
            name = "reject_test"
            def check(self, intent: RiskIntent) -> RiskCheckResult:
                return RiskCheckResult.reject(reason="nope", check_name="reject_test")

        strat = _DummyStrategy()
        mgr = RiskManager(checks=[AlwaysReject()])
        size = apply_risk_manager(strat, mgr, _intent(size=10))
        assert size is None
        assert len(strat.log.warnings) == 1
        assert "REJECT" in strat.log.warnings[0]
        assert "reject_test" in strat.log.warnings[0]

    def test_no_strategy_no_log_on_action(self) -> None:
        # strategy=None 时不应抛 AttributeError
        class AlwaysReject:
            name = "x"
            def check(self, intent: RiskIntent) -> RiskCheckResult:
                return RiskCheckResult.reject(reason="r", check_name="x")
        mgr = RiskManager(checks=[AlwaysReject()])
        size = apply_risk_manager(None, mgr, _intent())
        assert size is None  # 无 log，但语义正确


# ---------------------------------------------------------------------------
# 端到端：build_risk_manager + apply_risk_manager 联动
# ---------------------------------------------------------------------------


class TestBuildAndApply:
    def test_drawdown_breach_via_handles(self) -> None:
        """完整流程：通过 handles 喂净值 → drawdown 触发 → apply 返回 None。"""
        mgr, handles = build_risk_manager(RiskConfig(
            enable_drawdown=True, drawdown_daily_pct=0.03,
        ))
        assert mgr is not None and handles.drawdown_tracker is not None

        # 喂初始净值
        handles.drawdown_tracker.record_equity(1_700_000_000_000, 10000)
        # 同日内跌 5% → 触发
        handles.drawdown_tracker.record_equity(1_700_001_000_000, 9500)
        assert handles.drawdown_tracker.state == DrawdownState.DAILY_BREACH

        strat = _DummyStrategy()
        size = apply_risk_manager(strat, mgr, _intent())
        assert size is None
        assert "drawdown" in strat.log.warnings[0]

    def test_vol_target_scales_via_handles(self) -> None:
        """喂高波动收益 → vol_target 触发 SCALE。"""
        mgr, handles = build_risk_manager(RiskConfig(
            enable_vol_target=True, vol_target_annualized=0.15, vol_window=20,
        ))
        assert handles.vol_target is not None
        # 喂 20 个高波动收益 → realized vol >> 15%
        for r in [0.05, -0.05] * 10:
            handles.vol_target.update_return("BTC-USDT-SWAP.OKX", r)

        strat = _DummyStrategy()
        size = apply_risk_manager(strat, mgr, _intent(size=10))
        assert size is not None
        assert size < 10
        assert "SCALE" in strat.log.infos[0]

    def test_pipeline_first_reject_short_circuits(self) -> None:
        """drawdown trip → REJECT → vol_target 即使 SCALE 也无效。"""
        mgr, handles = build_risk_manager(RiskConfig(
            enable_vol_target=True,
            enable_drawdown=True, drawdown_daily_pct=0.01,
        ))
        # vol_target 喂高波动想 SCALE
        for r in [0.05, -0.05] * 10:
            handles.vol_target.update_return("BTC-USDT-SWAP.OKX", r)
        # drawdown 喂触发熝断
        handles.drawdown_tracker.record_equity(1_700_000_000_000, 10000)
        handles.drawdown_tracker.record_equity(1_700_001_000_000, 9800)  # -2% > 1%

        size = apply_risk_manager(_DummyStrategy(), mgr, _intent())
        assert size is None  # drawdown 早退


# ---------------------------------------------------------------------------
# RiskConfig msgspec 友好性
# ---------------------------------------------------------------------------


class TestRiskConfigSerialization:
    def test_frozen_config_is_hashable(self) -> None:
        # frozen=True + slots=True → 可哈希，可放进 set / dict key
        cfg1 = RiskConfig(enable_drawdown=True)
        cfg2 = RiskConfig(enable_drawdown=True)
        assert hash(cfg1) == hash(cfg2)

    def test_default_values(self) -> None:
        cfg = RiskConfig()
        # 默认全关，是安全的（不破坏现有策略行为）
        assert cfg.enable_vol_target is False
        assert cfg.enable_kelly is False
        assert cfg.enable_drawdown is False
        assert cfg.enable_correlation is False
