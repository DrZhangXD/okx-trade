"""regime detector + RegimeGateCheck 单测。"""
from __future__ import annotations

import math
import random

import pytest

from okx_trade.risk.base import RiskAction, RiskIntent
from okx_trade.risk.integration import RiskConfig, build_risk_manager
from okx_trade.risk.regime import (
    DEFAULT_SCALE_TABLE,
    RegimeGateCheck,
    RuleBasedRegimeDetector,
    RuleBasedRegimeParams,
)


# ---------------------------------------------------------------------------
# RuleBasedRegimeDetector
# ---------------------------------------------------------------------------


def _feed_synthetic(detector: RuleBasedRegimeDetector, closes: list[float], start_ts: int = 1_700_000_000_000) -> None:
    for i, c in enumerate(closes):
        detector.update(ts_ms=start_ts + i * 86_400_000, close=c)


class TestRuleBasedRegimeDetector:
    def test_neutral_with_insufficient_history(self) -> None:
        det = RuleBasedRegimeDetector()
        _feed_synthetic(det, [60000.0 + i for i in range(30)])  # 30 < min_history=60
        assert det.current_state() == "neutral"

    def test_trending_when_above_sma_and_low_vol(self) -> None:
        # 稳定上涨 + 低波动率 → trending
        det = RuleBasedRegimeDetector(RuleBasedRegimeParams(
            sma_window=50, rv_window=10, rv_history_days=50,
            trending_rv_pct=0.6, mean_reverting_rv_pct=0.7, min_history=60,
        ))
        # 250 个稳步上涨的点，低噪音
        rng = random.Random(42)
        closes = []
        price = 60000.0
        for _ in range(250):
            price *= 1 + (0.0005 + rng.gauss(0, 0.001))  # ~0.05% 日均，1‰ 噪声
            closes.append(price)
        _feed_synthetic(det, closes)
        assert det.current_state() == "trending"

    def test_mean_reverting_when_below_sma(self) -> None:
        # 持续下跌 → mean_reverting (because close < sma)
        det = RuleBasedRegimeDetector(RuleBasedRegimeParams(
            sma_window=50, rv_window=10, rv_history_days=50, min_history=60,
        ))
        closes = []
        price = 60000.0
        rng = random.Random(7)
        for _ in range(250):
            price *= 1 + (-0.0008 + rng.gauss(0, 0.001))
            closes.append(price)
        _feed_synthetic(det, closes)
        assert det.current_state() == "mean_reverting"

    def test_mean_reverting_on_high_vol_spike(self) -> None:
        # 价格在 SMA 之上但波动率高于历史 70 分位 → mean_reverting
        det = RuleBasedRegimeDetector(RuleBasedRegimeParams(
            sma_window=50, rv_window=10, rv_history_days=50, min_history=60,
        ))
        rng = random.Random(11)
        closes = []
        price = 60000.0
        # 前 200 个低波动率
        for _ in range(200):
            price *= 1 + rng.gauss(0.0001, 0.005)
            closes.append(price)
        # 后 50 个高波动率（>70 分位）
        for _ in range(50):
            price *= 1 + rng.gauss(0.0005, 0.05)  # 5% 日波动是极高的
            closes.append(price)
        _feed_synthetic(det, closes)
        assert det.current_state() == "mean_reverting"

    def test_update_rejects_non_monotonic_ts(self) -> None:
        det = RuleBasedRegimeDetector()
        det.update(ts_ms=2000, close=60000.0)
        det.update(ts_ms=1000, close=70000.0)  # 倒退被拒
        assert det.current_state() == "neutral"

    def test_update_ignores_non_positive_close(self) -> None:
        det = RuleBasedRegimeDetector()
        det.update(ts_ms=1000, close=0.0)
        det.update(ts_ms=2000, close=-1.0)
        assert det.current_state() == "neutral"


# ---------------------------------------------------------------------------
# RegimeGateCheck
# ---------------------------------------------------------------------------


class _StubDetector:
    def __init__(self, state: str) -> None:
        self.state = state

    def update(self, ts_ms: int, close: float) -> None:
        pass

    def current_state(self) -> str:
        return self.state


def _make_intent(size: float = 10.0) -> RiskIntent:
    return RiskIntent(
        strategy_id="strat-1", instrument_id="BTC-USDT-SWAP.OKX",
        direction="long", size=size,
        entry_price=60000.0, stop_price=59700.0,
        account_equity_usdt=10000.0,
    )


class TestRegimeGateCheckMapping:
    """验证默认映射表的全部 9 种 (state, kind) 组合。"""

    @pytest.mark.parametrize("state,kind,expected_scale", [
        ("trending", "momentum", 1.0),
        ("trending", "reversal", 0.4),
        ("trending", "neutral", 1.0),
        ("mean_reverting", "momentum", 0.4),
        ("mean_reverting", "reversal", 1.0),
        ("mean_reverting", "neutral", 1.0),
        ("neutral", "momentum", 0.7),
        ("neutral", "reversal", 0.7),
        ("neutral", "neutral", 1.0),
    ])
    def test_scale_factor(self, state: str, kind: str, expected_scale: float) -> None:
        det = _StubDetector(state)
        check = RegimeGateCheck(detector=det, strategy_kind=kind)  # type: ignore[arg-type]
        result = check.check(_make_intent(size=10.0))
        if expected_scale >= 1.0:
            assert result.action == RiskAction.APPROVE
            assert result.scaled_size == 10.0
        else:
            assert result.action == RiskAction.SCALE
            assert result.scaled_size == pytest.approx(10.0 * expected_scale)


# ---------------------------------------------------------------------------
# Integration with build_risk_manager
# ---------------------------------------------------------------------------


class TestBuildRiskManagerWithRegime:
    def test_no_regime_when_disabled(self) -> None:
        det = RuleBasedRegimeDetector()
        cfg = RiskConfig(enable_drawdown=True, enable_regime_gate=False)
        mgr, handles = build_risk_manager(cfg, regime_detector=det)
        check_names = [type(c).__name__ for c in mgr.checks]
        assert "RegimeGateCheck" not in check_names
        # handle 仍然透传（其他策略可读 state）
        assert handles.regime_detector is det

    def test_no_regime_when_detector_missing(self) -> None:
        # 即使 enable_regime_gate=True，没注入 detector 时跳过追加 check
        cfg = RiskConfig(enable_drawdown=True, enable_regime_gate=True,
                         regime_strategy_kind="momentum")
        mgr, _ = build_risk_manager(cfg, regime_detector=None)
        check_names = [type(c).__name__ for c in mgr.checks]
        assert "RegimeGateCheck" not in check_names

    def test_regime_appended_when_enabled(self) -> None:
        det = _StubDetector("trending")
        cfg = RiskConfig(enable_drawdown=True, enable_regime_gate=True,
                         regime_strategy_kind="reversal")
        mgr, handles = build_risk_manager(cfg, regime_detector=det)  # type: ignore[arg-type]
        check_names = [type(c).__name__ for c in mgr.checks]
        assert "RegimeGateCheck" in check_names

    def test_regime_only_no_other_checks(self) -> None:
        # 只开 regime gate，其他全关
        det = _StubDetector("trending")
        cfg = RiskConfig(enable_regime_gate=True, regime_strategy_kind="momentum")
        mgr, _ = build_risk_manager(cfg, regime_detector=det)  # type: ignore[arg-type]
        assert mgr is not None
        check_names = [type(c).__name__ for c in mgr.checks]
        assert check_names == ["RegimeGateCheck"]


# ---------------------------------------------------------------------------
# HMM detector (skip if hmmlearn missing)
# ---------------------------------------------------------------------------


def _has_hmmlearn() -> bool:
    try:
        import hmmlearn  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_hmmlearn(), reason="hmmlearn optional dep not installed")
class TestHMMRegimeDetector:
    def test_neutral_with_no_history(self) -> None:
        from okx_trade.risk.regime import HMMRegimeDetector
        det = HMMRegimeDetector()
        assert det.current_state() == "neutral"

    def test_trains_and_returns_a_state(self) -> None:
        from okx_trade.risk.regime import HMMRegimeDetector, HMMRegimeParams
        det = HMMRegimeDetector(HMMRegimeParams(min_history=60, retrain_interval_days=1))
        rng = random.Random(123)
        # 200 个混合 regime 的合成数据
        price = 60000.0
        ts = 1_700_000_000_000
        for i in range(200):
            if i < 100:
                price *= 1 + rng.gauss(0.0008, 0.005)  # 牛
            else:
                price *= 1 + rng.gauss(-0.0008, 0.02)  # 熊 / 高波动
            ts += 86_400_000
            det.update(ts_ms=ts, close=price)
        state = det.current_state()
        # 不强求具体 state（HMM 在合成数据上的 label 可能不稳），但必须是有效 enum 值
        assert state in ("trending", "mean_reverting", "neutral")
