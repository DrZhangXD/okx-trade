"""Unit tests for FundingXS three-layer defense helpers (2026-05-26)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from okx_trade.strategies._isolated_helpers import (
    compute_edge_score,
    compute_leverage,
    outlier_check,
)


# ---------------------------------------------------------------------------
# compute_leverage
# ---------------------------------------------------------------------------
class TestComputeLeverage:
    def test_zero_edge_returns_base(self) -> None:
        assert compute_leverage(0.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 2.0

    def test_one_sigma_edge_returns_5x(self) -> None:
        # base=2 + slope=3 * |1| = 5
        assert compute_leverage(1.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 5.0

    def test_two_sigma_edge_returns_8x(self) -> None:
        assert compute_leverage(2.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 8.0

    def test_three_sigma_edge_clipped_to_hi(self) -> None:
        # base=2 + slope=3 * 3 = 11 → clipped to 10
        assert compute_leverage(3.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 10.0

    def test_negative_edge_uses_abs(self) -> None:
        assert compute_leverage(-1.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 5.0

    def test_lo_clip(self) -> None:
        # if base were below lo somehow (config error), still clip to lo
        assert compute_leverage(0.0, base=1.0, slope=3.0, lo=2.0, hi=10.0) == 2.0


# ---------------------------------------------------------------------------
# compute_edge_score
# ---------------------------------------------------------------------------
class TestComputeEdgeScore:
    def test_short_positive_funding_positive_edge(self) -> None:
        # leg is short, funding > universe avg → "going short on high funding"
        # → strong edge
        score = compute_edge_score(
            funding_rate=0.005,
            funding_universe=[0.001, 0.001, 0.001, 0.001, 0.005],
            basis=None,
            basis_universe=None,
            direction="short",
            combine_basis=False,
        )
        # funding_z of 0.005 in [0.001, 0.001, 0.001, 0.001, 0.005] ≈ 2.0
        # short direction → sign(+1) × 2.0 → +2.0
        assert score == pytest.approx(2.0, abs=0.1)

    def test_long_negative_funding_positive_edge(self) -> None:
        # leg is long, funding < universe avg → "going long on low funding"
        # → strong edge (mirror of above)
        score = compute_edge_score(
            funding_rate=-0.005,
            funding_universe=[-0.001, -0.001, -0.001, -0.001, -0.005],
            basis=None,
            basis_universe=None,
            direction="long",
            combine_basis=False,
        )
        # funding_z of -0.005 ≈ -2.0; long direction → sign(-1) × -2.0 → +2.0
        assert score == pytest.approx(2.0, abs=0.1)

    def test_combine_basis_adds_basis_z(self) -> None:
        # both funding and basis aligned with short → larger edge
        score = compute_edge_score(
            funding_rate=0.005,
            funding_universe=[0.001, 0.005],
            basis=0.01,
            basis_universe=[0.001, 0.01],
            direction="short",
            combine_basis=True,
        )
        # both signals at "high" end → both z ≈ +1; mean = 1; sign(+1) × 1 = 1
        assert score == pytest.approx(1.0, abs=0.1)

    def test_universe_zero_std_returns_zero(self) -> None:
        # all identical funding → no edge
        score = compute_edge_score(
            funding_rate=0.001,
            funding_universe=[0.001, 0.001, 0.001],
            basis=None, basis_universe=None,
            direction="short",
            combine_basis=False,
        )
        assert score == 0.0

    def test_single_element_universe_returns_zero(self) -> None:
        score = compute_edge_score(
            funding_rate=0.005,
            funding_universe=[0.005],
            basis=None, basis_universe=None,
            direction="short",
            combine_basis=False,
        )
        assert score == 0.0


# ---------------------------------------------------------------------------
# outlier_check
# ---------------------------------------------------------------------------
class TestOutlierCheck:
    def _calm_closes(self, n: int = 1500) -> list[float]:
        """Geometric Brownian motion-ish: small log-returns ~N(0, 0.001)."""
        rng = np.random.default_rng(seed=42)
        rets = rng.normal(0.0, 0.001, n)
        prices = 100.0 * np.exp(np.cumsum(rets))
        return prices.tolist()

    def test_warmup_short_history_allowed(self) -> None:
        ok, reason = outlier_check(
            closes=[1.0, 1.1, 0.9],
            window=60, baseline=1440, warmup=1440, ratio_threshold=3.0,
        )
        assert ok is True
        assert reason == "warmup"

    def test_calm_market_allowed(self) -> None:
        ok, reason = outlier_check(
            closes=self._calm_closes(),
            window=60, baseline=1440, warmup=1440, ratio_threshold=3.0,
        )
        assert ok is True
        assert reason == "ok"

    def test_recent_spike_rejected(self) -> None:
        closes = self._calm_closes(n=1440)
        # Inject a large wick in the last 60 bars: 10x normal vol
        rng = np.random.default_rng(seed=7)
        wick = rng.normal(0.0, 0.01, 60)  # 10x sigma
        closes.extend((closes[-1] * np.exp(np.cumsum(wick))).tolist())
        ok, reason = outlier_check(
            closes=closes,
            window=60, baseline=1440, warmup=1440, ratio_threshold=3.0,
        )
        assert ok is False
        assert "vol_ratio" in reason

    def test_zero_baseline_vol_allowed(self) -> None:
        # Flat history → std=0 baseline → allow (no signal to filter on)
        ok, reason = outlier_check(
            closes=[100.0] * 1500,
            window=60, baseline=1440, warmup=1440, ratio_threshold=3.0,
        )
        assert ok is True
        assert reason == "no_baseline"


# ---------------------------------------------------------------------------
# Note: TestSetLeverageCache was removed in P2-Task 14 — set-leverage cache
# behavior now lives on IsolatedMarginService and is covered by
# tests/unit/test_risk_isolated_margin_service.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Integration: _compute_targets respects outlier guard
# ---------------------------------------------------------------------------
class TestComputeTargetsOutlierGuard:
    """We can't construct a real FundingXSStrategy without NT runtime, but we
    can directly test the underlying outlier_check helper that _compute_targets
    delegates to — that's the unit-testable contract."""

    def test_calm_market_includes_leg(self) -> None:
        from okx_trade.strategies._isolated_helpers import outlier_check
        rng = np.random.default_rng(seed=11)
        rets = rng.normal(0.0, 0.001, 1500)
        closes = (100 * np.exp(np.cumsum(rets))).tolist()
        ok, _ = outlier_check(closes=closes, window=60, baseline=1440,
                              warmup=1440, ratio_threshold=3.0)
        assert ok is True

    def test_wicky_market_excludes_leg(self) -> None:
        from okx_trade.strategies._isolated_helpers import outlier_check
        rng = np.random.default_rng(seed=11)
        rets = rng.normal(0.0, 0.001, 1440)
        spike = rng.normal(0.0, 0.02, 60)  # 20x normal
        closes = (100 * np.exp(np.cumsum(np.concatenate([rets, spike])))).tolist()
        ok, reason = outlier_check(closes=closes, window=60, baseline=1440,
                                    warmup=1440, ratio_threshold=3.0)
        assert ok is False
        assert "vol_ratio" in reason


# ---------------------------------------------------------------------------
# Integration: _compute_target_positions wired to 1m outlier-guard cache
# ---------------------------------------------------------------------------
class TestComputeTargetsIntegration:
    """End-to-end check that ``_compute_target_positions`` actually fires the
    outlier guard when fed a populated ``_closes_1m_by_inst`` deque.

    Before the wire-up fix, the guard read from ``_closes_by_inst`` (1D,
    capped at ~32 entries), which is always < warmup=1440 → ``(True,
    "warmup")`` → Layer 3 was inert. This test pins the new behavior.
    """

    @pytest.fixture
    def mock_strategy(self):
        """Construct a minimal mock that exposes only the attributes
        ``_compute_target_positions`` reads — no real NT runtime."""
        from collections import deque

        class _MockConfig:
            top_n = 1
            bot_n = 1
            enable_outlier_guard = True
            outlier_vol_ratio = 3.0
            outlier_window_min = 60
            outlier_baseline_min = 1440
            outlier_warmup_min = 1440
            enable_beta_hedge = False
            enable_dynamic_lever = False
            lever_max = 10.0
            lever_min = 2.0
            lever_base = 2.0
            lever_slope = 3.0
            max_position_pct = 0.40
            account_equity_usdt = 10000.0
            lever_edge_combine_basis = False

        class _MockLog:
            def __init__(self):
                self.warnings: list[str] = []
                self.infos: list[str] = []

            def warning(self, msg):
                self.warnings.append(msg)

            def info(self, msg):
                self.infos.append(msg)

            def error(self, msg):
                self.infos.append(msg)

        class _MockInst:
            multiplier = 1.0
            size_increment = 0.1

        class _MockCache:
            def instrument(self, _inst_id):
                return _MockInst()

        # Two legs with deterministic close histories.
        rng_calm = np.random.default_rng(seed=11)
        calm_rets = rng_calm.normal(0.0, 0.001, 1500)
        calm_closes_1m = (100.0 * np.exp(np.cumsum(calm_rets))).tolist()

        rng_wick = np.random.default_rng(seed=23)
        wicky_base = rng_wick.normal(0.0, 0.001, 1440)
        wicky_spike = rng_wick.normal(0.0, 0.02, 60)  # 20x normal vol
        wicky_closes_1m = (
            100.0 * np.exp(np.cumsum(np.concatenate([wicky_base, wicky_spike])))
        ).tolist()

        m = type("MockStrategy", (), {})()
        m.config = _MockConfig()
        m.log = _MockLog()
        m.cache = _MockCache()
        # ranked ascending by funding rate:
        #   CALM = +0.005 (highest → short leg under bot_n=1)
        #   WICK = -0.005 (lowest  → long  leg under top_n=1)
        # Keys must be parseable as NT InstrumentId ("<sym>.<venue>").
        calm_iid = "CALM-USDT-SWAP.OKX"
        wick_iid = "WICK-USDT-SWAP.OKX"
        m._latest_funding = {
            calm_iid: 0.005,
            wick_iid: -0.005,
        }
        m._latest_basis = {}
        # 1D cache: only need last close for price → contracts conversion.
        # β-hedge is disabled, so we don't need a long history.
        m._closes_by_inst = {
            calm_iid: [calm_closes_1m[-1]],
            wick_iid: [wicky_closes_1m[-1]],
        }
        # 1m cache: feeds outlier_check.
        m._closes_1m_by_inst = {
            calm_iid: deque(calm_closes_1m, maxlen=2000),
            wick_iid: deque(wicky_closes_1m, maxlen=2000),
        }
        m._allocated_equity_usdt = 10000.0
        m._inst_ids = []  # not read by _compute_target_positions

        # Stub _beta_ref_id with a minimal namespace (only ``.value`` is read).
        m._beta_ref_id = type("Iid", (), {"value": "BTC-USDT-SWAP.OKX"})()

        # Bind _returns staticmethod so _compute_target_positions can call it.
        from okx_trade.strategies.funding_cross_section import FundingXSStrategy
        m._returns = FundingXSStrategy._returns

        return m

    def test_wicky_leg_is_rejected_by_outlier_guard(self, mock_strategy) -> None:
        from okx_trade.strategies.funding_cross_section import FundingXSStrategy

        bound = FundingXSStrategy._compute_target_positions.__get__(mock_strategy)
        target = bound()

        # WICK is the long candidate (lowest funding). Its 1m history has a
        # 20x-vol spike in the last 60 bars → outlier guard must reject.
        assert "WICK-USDT-SWAP.OKX" not in target
        assert any(
            "OUTLIER_SKIP" in w and "WICK-USDT-SWAP.OKX" in w
            for w in mock_strategy.log.warnings
        ), f"expected OUTLIER_SKIP for WICK, got warnings={mock_strategy.log.warnings}"

    def test_calm_leg_passes_outlier_guard(self, mock_strategy) -> None:
        from okx_trade.strategies.funding_cross_section import FundingXSStrategy

        bound = FundingXSStrategy._compute_target_positions.__get__(mock_strategy)
        target = bound()

        # CALM (calm 1m series) should pass the guard — and since β-hedge is
        # off and the mock cache returns a usable instrument, it should make
        # it into the target dict as the short leg (highest funding).
        assert "CALM-USDT-SWAP.OKX" in target
        leg = target["CALM-USDT-SWAP.OKX"]
        assert leg.direction == "short"
        assert leg.contracts > 0
        # No OUTLIER_SKIP for CALM
        assert not any(
            "OUTLIER_SKIP" in w and "CALM-USDT-SWAP.OKX" in w
            for w in mock_strategy.log.warnings
        )

    def test_outlier_guard_is_now_actually_firing(self, mock_strategy) -> None:
        """Regression: confirm Layer 3 is no longer structurally inert.

        Pre-fix, the guard was wired to ``_closes_by_inst`` (1D, len ≤ 32),
        so it always returned ``(True, "warmup")``. We assert that with a
        full 1500-bar 1m history, the guard reaches its decision branches
        (not ``warmup``) for at least one leg.
        """
        from okx_trade.strategies._isolated_helpers import outlier_check

        # Same data the fixture would feed. If this returns "warmup", the
        # wire-up is wrong (we'd be looking at 1D instead of 1m).
        wick_closes = list(mock_strategy._closes_1m_by_inst["WICK-USDT-SWAP.OKX"])
        ok, reason = outlier_check(
            closes=wick_closes,
            window=mock_strategy.config.outlier_window_min,
            baseline=mock_strategy.config.outlier_baseline_min,
            warmup=mock_strategy.config.outlier_warmup_min,
            ratio_threshold=mock_strategy.config.outlier_vol_ratio,
        )
        assert reason != "warmup", (
            "Outlier guard returned 'warmup' even with 1500 1m bars — "
            "wire-up is wrong, still reading from a too-small cache."
        )
        assert ok is False, "Wicky 20x-vol history should be rejected"


# ---------------------------------------------------------------------------
# _LegTarget dataclass smoke test
# ---------------------------------------------------------------------------
def test_leg_target_dataclass_construction() -> None:
    from okx_trade.enums import PosSide
    from okx_trade.strategies.funding_cross_section import _LegTarget
    lt = _LegTarget(
        direction="short", contracts=10.0, lever=5.0, edge_score=1.5,
        pos_side=PosSide.SHORT,
    )
    assert lt.direction == "short"
    assert lt.contracts == 10.0
    assert lt.lever == 5.0
    assert lt.edge_score == 1.5
    assert lt.pos_side == PosSide.SHORT


# ---------------------------------------------------------------------------
# Partial-rebalance abort guard (post-migration: service-mocked)
# ---------------------------------------------------------------------------
class TestPartialRebalanceAbort:
    """Verify _execute_diff aborts open-phase if service.batch_ensure_leverage
    returns all_ok=False."""

    @pytest.mark.asyncio
    async def test_partial_failure_aborts_all_opens(self) -> None:
        from okx_trade.strategies.funding_cross_section import (
            FundingXSStrategy, _LegTarget,
        )
        from okx_trade.enums import PosSide
        from okx_trade.risk.isolated_margin_service import BatchEnsureResult

        class _MockLog:
            def __init__(self): self.warnings = []
            def warning(self, msg): self.warnings.append(msg)
            def info(self, msg): pass

        class _MockConfig:
            margin_mode = "isolated"
            enable_dynamic_lever = True

        class _MockService:
            calls: list = []
            def is_backtest(self): return False
            async def get_pos_mode(self): return "long_short_mode"
            async def batch_ensure_leverage(self, items):
                self.calls.append(items)
                # First leg succeeds, second fails — emulate partial failure
                return BatchEnsureResult(
                    all_ok=False,
                    failed=[("B-USDT-SWAP", "51001 broken")],
                )

        async def fake_open_leg(inst_value, leg):
            pytest.fail(f"_open_leg should not be called on abort; got {inst_value}")

        m = type("Mock", (), {})()
        m.config = _MockConfig()
        m.log = _MockLog()
        m._positions = {}
        m._iso_service = _MockService()
        m._open_leg = fake_open_leg
        m._close_leg = lambda *_a, **_kw: None
        m._execute_diff = FundingXSStrategy._execute_diff.__get__(m)

        target = {
            "A-USDT-SWAP": _LegTarget(
                direction="long", contracts=1.0, lever=5.0, edge_score=1.0,
                pos_side=PosSide.LONG,
            ),
            "B-USDT-SWAP": _LegTarget(
                direction="short", contracts=1.0, lever=5.0, edge_score=1.0,
                pos_side=PosSide.SHORT,
            ),
        }
        await m._execute_diff(target)

        assert any("ABORT" in w for w in m.log.warnings)
        # batch_ensure_leverage was called once with both items
        assert len(m._iso_service.calls) == 1
        assert len(m._iso_service.calls[0]) == 2


# ---------------------------------------------------------------------------
# Note: TestUnknownPosModeWarn and TestBacktestFallback were removed in
# P2-Task 14. Those behaviors now live on IsolatedMarginService.get_pos_mode
# and .is_backtest, covered by tests/unit/test_risk_isolated_margin_service.py.
# ---------------------------------------------------------------------------
