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
# _set_leverage_cached behavior (mocked)
# ---------------------------------------------------------------------------
class TestSetLeverageCache:
    """Test the cache logic via a minimal mock — we don't spin up a real
    strategy because that requires NT TradingNode. We bind the unbound
    method to a mock object that has the required state shape."""

    @pytest.fixture
    def fake_strategy(self):
        class _Mock:
            def __init__(self):
                self._set_lever_cache: dict = {}
                self.calls: list = []
                self.log = type("L", (), {
                    "info": lambda *_, **__: None,
                    "warning": lambda *_, **__: None,
                })()
                outer = self

                class _Acct:
                    async def set_leverage(self_inner, *, inst_id, leverage, mgn_mode, pos_side):
                        outer.calls.append((inst_id, leverage, pos_side))

                class _Rest:
                    account = _Acct()
                self._rest = _Rest()

        from okx_trade.strategies.funding_cross_section import FundingXSStrategy
        m = _Mock()
        m._set_leverage_cached = FundingXSStrategy._set_leverage_cached.__get__(m)
        return m

    @pytest.mark.asyncio
    async def test_first_call_invokes_rest_with_pos_side(self, fake_strategy) -> None:
        from okx_trade.enums import PosSide
        ok = await fake_strategy._set_leverage_cached("DOT-USDT-SWAP", 5.0, PosSide.LONG)
        assert ok is True
        assert len(fake_strategy.calls) == 1
        assert fake_strategy.calls[0][0] == "DOT-USDT-SWAP"
        assert fake_strategy.calls[0][1] == 5
        assert fake_strategy.calls[0][2] == PosSide.LONG

    @pytest.mark.asyncio
    async def test_same_lever_same_side_skips_rest(self, fake_strategy) -> None:
        from okx_trade.enums import PosSide
        await fake_strategy._set_leverage_cached("DOT-USDT-SWAP", 5.0, PosSide.LONG)
        await fake_strategy._set_leverage_cached("DOT-USDT-SWAP", 5.0, PosSide.LONG)
        assert len(fake_strategy.calls) == 1  # second call hit cache

    @pytest.mark.asyncio
    async def test_same_lever_different_side_invokes_again(self, fake_strategy) -> None:
        from okx_trade.enums import PosSide
        await fake_strategy._set_leverage_cached("DOT-USDT-SWAP", 5.0, PosSide.LONG)
        await fake_strategy._set_leverage_cached("DOT-USDT-SWAP", 5.0, PosSide.SHORT)
        # Different posSide → separate cache entries
        assert len(fake_strategy.calls) == 2
        sides = {c[2] for c in fake_strategy.calls}
        assert PosSide.LONG in sides and PosSide.SHORT in sides

    @pytest.mark.asyncio
    async def test_changed_lever_re_invokes(self, fake_strategy) -> None:
        from okx_trade.enums import PosSide
        await fake_strategy._set_leverage_cached("DOT-USDT-SWAP", 5.0, PosSide.LONG)
        await fake_strategy._set_leverage_cached("DOT-USDT-SWAP", 8.0, PosSide.LONG)
        assert len(fake_strategy.calls) == 2
        assert fake_strategy.calls[0][1] == 5
        assert fake_strategy.calls[1][1] == 8
