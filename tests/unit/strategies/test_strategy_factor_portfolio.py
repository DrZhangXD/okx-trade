"""Tests for FactorPortfolioStrategy pure synthesis."""
from __future__ import annotations

import importlib

import numpy as np
import pytest

from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, register_factor
from okx_trade.strategies.factor_portfolio import (
    FactorWeight,
    cross_section_zscore,
    select_top_bot,
    synthesize_score,
)


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    import okx_trade.research.factors.momentum as m
    importlib.reload(m)  # bring back built-ins for these tests
    yield
    clear_registry()


def _panel(close: np.ndarray) -> FactorPanel:
    T, N = close.shape
    return FactorPanel(
        inst_ids=tuple(f"I{i}" for i in range(N)),
        timestamps_ms=tuple(range(T)),
        close=close, volume_usdt=np.ones((T, N)),
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_cross_section_zscore_zero_mean_unit_var() -> None:
    vals = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    z = cross_section_zscore(vals)
    assert z.mean() == pytest.approx(0.0)
    assert z.std(ddof=0) == pytest.approx(1.0)


def test_cross_section_zscore_returns_nan_when_no_variance() -> None:
    vals = np.array([3.0, 3.0, 3.0])
    z = cross_section_zscore(vals)
    assert np.all(np.isnan(z))


def test_cross_section_zscore_all_nan_input_silent() -> None:
    """All-NaN input must return all-NaN WITHOUT emitting numpy
    RuntimeWarning. Regression for log spam at funding hours when
    history-hungry factors (funding_z_30d, basis_z_30d) return all-NaN
    rows before the 30-day rolling window is warm.
    """
    import warnings
    vals = np.array([np.nan, np.nan, np.nan])
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # promote RuntimeWarning → exception
        z = cross_section_zscore(vals)
    assert np.all(np.isnan(z))


def test_cross_section_zscore_empty_input_silent() -> None:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        z = cross_section_zscore(np.array([], dtype=float))
    assert z.shape == (0,)


def test_synthesize_score_combines_factors_by_weight() -> None:
    # Create close prices with 25 bars, 3 instruments, different momentum profiles
    closes = np.zeros((25, 3))
    closes[:, 0] = np.linspace(100, 124, 25)  # uptrend
    closes[:, 1] = np.linspace(100, 110, 25)  # slower uptrend
    closes[:, 2] = np.linspace(100, 95, 25)   # downtrend
    panel = _panel(closes)
    weights = [
        FactorWeight(id="momentum_1d", weight=1.0),
    ]
    score, missing = synthesize_score(panel, weights)
    assert score.shape == (panel.n,)
    assert missing == []
    # Verify that instruments with better momentum get higher scores
    assert score[0] > score[2]  # uptrend > downtrend


def test_synthesize_score_skips_unregistered_with_warning() -> None:
    panel = _panel(np.ones((30, 3)) * 100.0)
    weights = [FactorWeight(id="nonexistent", weight=1.0)]
    score, missing = synthesize_score(panel, weights)
    assert "nonexistent" in missing
    # Score is all-NaN when all weights are missing
    assert np.all(np.isnan(score))


def test_select_top_bot_returns_indices_by_score() -> None:
    score = np.array([0.5, -1.2, 0.8, -0.3, 1.5])
    longs, shorts = select_top_bot(score, top_k_long=2, top_k_short=2)
    assert longs == [4, 2]   # 1.5, 0.8 — descending
    assert shorts == [1, 3]  # -1.2, -0.3 — ascending


def test_select_top_bot_skips_nan_scores() -> None:
    score = np.array([0.5, np.nan, 0.8, -0.3, 1.5])
    longs, shorts = select_top_bot(score, top_k_long=3, top_k_short=3)
    assert 1 not in longs and 1 not in shorts


nt = pytest.importorskip("nautilus_trader")


def test_factor_portfolio_config_loads_from_yaml() -> None:
    """Verify the dataclass-mode StrategyConfig accepts our yaml shape."""
    from okx_trade.strategies.factor_portfolio import FactorPortfolioConfig
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4,
        top_k_long=2, top_k_short=2,
        risk_pct=0.002,
        account_equity_usdt=10_000.0,
        factor_weights=[("momentum_7d", 0.5), ("funding_z_30d", 0.5)],
    )
    assert cfg.rebalance_hours == 4
    assert len(cfg.factor_weights) == 2


def test_factor_portfolio_strategy_initializes_without_error() -> None:
    from okx_trade.strategies.factor_portfolio import (
        FactorPortfolioConfig, FactorPortfolioStrategy,
    )
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4,
        top_k_long=1, top_k_short=1,
        risk_pct=0.002, account_equity_usdt=10_000.0,
        factor_weights=[("momentum_7d", 1.0)],
    )
    strategy = FactorPortfolioStrategy(cfg)
    assert strategy.config.rebalance_hours == 4


def test_derive_spot_inst_id_handles_variants() -> None:
    from okx_trade.strategies.factor_portfolio import _derive_spot_inst_id
    assert _derive_spot_inst_id("BTC-USDT-SWAP.OKX") == "BTC-USDT.OKX"
    assert _derive_spot_inst_id("ETH-USDT-SWAP") == "ETH-USDT"
    # Already spot — None
    assert _derive_spot_inst_id("BTC-USDT") is None
    # Delivery futures — None
    assert _derive_spot_inst_id("BTC-USD-260925") is None


def test_factor_portfolio_strategy_subscribes_spot_pairs_by_default() -> None:
    from okx_trade.strategies.factor_portfolio import (
        FactorPortfolioConfig, FactorPortfolioStrategy,
    )
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4,
        top_k_long=1, top_k_short=1,
        risk_pct=0.002, account_equity_usdt=10_000.0,
        factor_weights=[("basis_z_30d", 1.0)],
    )
    s = FactorPortfolioStrategy(cfg)
    # spot routing is set up on init
    assert s._spot_to_perp == {
        "BTC-USDT.OKX": "BTC-USDT-SWAP.OKX",
        "ETH-USDT.OKX": "ETH-USDT-SWAP.OKX",
    }
    assert len(s._spot_bar_types) == 2


def test_ffill_basis_columns_bridges_gaps() -> None:
    """Bounded column-wise ffill: small gaps filled, gaps >max_lookback stay NaN."""
    from okx_trade.strategies.factor_portfolio import _ffill_basis_columns

    arr = np.array([
        [0.01, 0.02],   # row 0: both valid
        [np.nan, 0.03], # row 1: col0 gap (1 row), col1 valid
        [np.nan, np.nan],  # row 2: col0 gap (2), col1 gap (1)
        [np.nan, np.nan],  # row 3: col0 gap (3), col1 gap (2)
        [0.04, np.nan], # row 4: col0 recovered, col1 gap (3) — still in window
    ])
    _ffill_basis_columns(arr, max_lookback=3)
    # col 0: row 1/2/3 filled from row 0 (gap 1/2/3 ≤ 3 = window)
    assert arr[1, 0] == pytest.approx(0.01)
    assert arr[2, 0] == pytest.approx(0.01)
    assert arr[3, 0] == pytest.approx(0.01)
    assert arr[4, 0] == pytest.approx(0.04)
    # col 1: row 2/3/4 filled from row 1 (gap 1/2/3 ≤ 3)
    assert arr[2, 1] == pytest.approx(0.03)
    assert arr[3, 1] == pytest.approx(0.03)
    assert arr[4, 1] == pytest.approx(0.03)


def test_ffill_basis_columns_respects_max_lookback() -> None:
    from okx_trade.strategies.factor_portfolio import _ffill_basis_columns

    arr = np.array([
        [0.01],
        [np.nan],
        [np.nan],
        [np.nan],
        [np.nan],  # row 4: gap 4 > window 3 → stays NaN
    ])
    _ffill_basis_columns(arr, max_lookback=3)
    assert arr[1, 0] == pytest.approx(0.01)
    assert arr[2, 0] == pytest.approx(0.01)
    assert arr[3, 0] == pytest.approx(0.01)  # gap 3 = window → still fills
    assert np.isnan(arr[4, 0])               # gap 4 > window → stale, leave NaN


def test_ffill_basis_columns_empty_array_is_noop() -> None:
    from okx_trade.strategies.factor_portfolio import _ffill_basis_columns
    arr = np.empty((0, 0), dtype=float)
    _ffill_basis_columns(arr, max_lookback=24)  # must not raise
    assert arr.shape == (0, 0)


def test_missing_factor_log_event_dedupes_steady_state() -> None:
    """Pure transition decider. Same skipped set across rebalances →
    after the first call, returns None (silent). Regression for the
    4-hourly "skipped factors ['basis_apr','basis_z_30d']" log spam.
    """
    from okx_trade.strategies.factor_portfolio import _missing_factor_log_event

    basis_skip = frozenset({"basis_apr", "basis_z_30d"})

    # First occurrence (None → any set) → emit at warning level
    ev = _missing_factor_log_event(basis_skip, last_logged=None)
    assert ev is not None and ev[0] == "warning"
    assert "basis_apr" in ev[1] and "basis_z_30d" in ev[1]

    # Steady state (same set re-evaluated) → silent
    assert _missing_factor_log_event(basis_skip, last_logged=basis_skip) is None

    # Skip-set narrows (one factor recovered, one still skipped) → emit warn
    ev = _missing_factor_log_event(frozenset({"basis_z_30d"}), last_logged=basis_skip)
    assert ev is not None and ev[0] == "warning"
    assert "basis_z_30d" in ev[1]

    # Recovery (set → empty) → emit info with "previously skipped" context
    ev = _missing_factor_log_event(frozenset(), last_logged=basis_skip)
    assert ev is not None and ev[0] == "info"
    assert "all factors active" in ev[1] and "basis_apr" in ev[1]

    # Steady all-clear (None or empty → empty) → silent
    assert _missing_factor_log_event(frozenset(), last_logged=frozenset()) is None
    assert _missing_factor_log_event(frozenset(), last_logged=None) is None


def test_factor_portfolio_strategy_disable_spot_subscription() -> None:
    from okx_trade.strategies.factor_portfolio import (
        FactorPortfolioConfig, FactorPortfolioStrategy,
    )
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4,
        top_k_long=1, top_k_short=1,
        risk_pct=0.002, account_equity_usdt=10_000.0,
        factor_weights=[("momentum_1d_reversal", 1.0)],
        subscribe_spot_for_basis=False,
    )
    s = FactorPortfolioStrategy(cfg)
    assert s._spot_to_perp == {}
    assert s._spot_bar_types == {}


def test_ffill_to_axis_basic() -> None:
    """_ffill_to_axis aligns sparse (ts, val) deques onto a regular ts axis."""
    from collections import deque

    from okx_trade.strategies.factor_portfolio import _ffill_to_axis

    by_inst = {
        "A": deque([(100, 1.0), (300, 2.0)]),
        "B": deque(),  # empty → all-NaN column
    }
    ts_axis = (100, 200, 300, 400)
    inst_ids = ("A", "B")
    out = _ffill_to_axis(by_inst, ts_axis, inst_ids)
    assert out.shape == (4, 2)
    # A: 1.0 at ts=100, ffill to ts=200, then 2.0 at ts=300, ffill to ts=400
    assert out[0, 0] == 1.0
    assert out[1, 0] == 1.0  # ffilled
    assert out[2, 0] == 2.0
    assert out[3, 0] == 2.0  # ffilled
    # B: empty → all NaN
    import numpy as np
    assert np.all(np.isnan(out[:, 1]))


def test_ffill_to_axis_before_first_obs_is_nan() -> None:
    from collections import deque
    import numpy as np

    from okx_trade.strategies.factor_portfolio import _ffill_to_axis
    by_inst = {"A": deque([(500, 9.0)])}
    out = _ffill_to_axis(by_inst, (100, 300, 500, 700), ("A",))
    assert np.isnan(out[0, 0])
    assert np.isnan(out[1, 0])
    assert out[2, 0] == 9.0
    assert out[3, 0] == 9.0  # ffilled


def test_factor_portfolio_polling_disabled_by_default_in_backtest_config() -> None:
    """Constructing a strategy with enable_rest_polling=False must not spawn task."""
    from okx_trade.strategies.factor_portfolio import (
        FactorPortfolioConfig, FactorPortfolioStrategy,
    )
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4, top_k_long=1, top_k_short=1,
        risk_pct=0.002, account_equity_usdt=10_000.0,
        factor_weights=[("momentum_1d_reversal", 1.0)],
        subscribe_spot_for_basis=False,
        enable_rest_polling=False,
    )
    s = FactorPortfolioStrategy(cfg)
    # Polling task is created by on_start, not __init__; verify init state.
    assert s._rest_poll_task is None
    assert s._funding_rates == {"BTC-USDT-SWAP.OKX": s._funding_rates["BTC-USDT-SWAP.OKX"]}
    assert len(s._funding_rates["BTC-USDT-SWAP.OKX"]) == 0
    assert len(s._open_interests["BTC-USDT-SWAP.OKX"]) == 0


def test_factor_portfolio_polling_config_has_safe_defaults() -> None:
    """Defaults: enable_rest_polling=True, rest_poll_interval_sec=300."""
    from okx_trade.strategies.factor_portfolio import FactorPortfolioConfig
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
    )
    assert cfg.enable_rest_polling is True
    assert cfg.rest_poll_interval_sec == 300
    # Live REST warmup defaults to 30 days so basis_z_30d / funding_z_30d are
    # active at full weight from restart instead of waiting ~30 days.
    assert cfg.warmup_via_rest_days == 30


def test_apply_warmup_panel_works_with_in_memory_panel() -> None:
    """REST warmup path: build a panel in memory (no parquet roundtrip) and
    feed it through _apply_warmup_panel directly. This is the code path used
    by _warmup_via_rest in live mode.
    """
    import numpy as np
    from okx_trade.research.panel import FactorPanel
    from okx_trade.strategies.factor_portfolio import (
        FactorPortfolioConfig, FactorPortfolioStrategy,
    )

    T = 50
    ts_axis = tuple(1_700_000_000_000 + i * 3_600_000 for i in range(T))
    panel = FactorPanel(
        inst_ids=("BTC-USDT-SWAP",),
        timestamps_ms=ts_axis,
        close=np.linspace(100.0, 110.0, T).reshape(T, 1),
        volume_usdt=np.ones((T, 1)) * 2e6,
        funding_rate=(np.ones((T, 1)) * 0.0002),
        open_interest=None,
        basis_apr=(np.ones((T, 1)) * 0.005),
    )

    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4, top_k_long=1, top_k_short=1,
        risk_pct=0.002, account_equity_usdt=10_000.0,
        factor_weights=[("basis_apr", 1.0)],
        subscribe_spot_for_basis=False,
        enable_rest_polling=False,
        warmup_via_rest_days=0,  # avoid spawning REST task in unit test
    )
    s = FactorPortfolioStrategy(cfg)
    s._apply_warmup_panel(panel)

    inst = "BTC-USDT-SWAP.OKX"
    assert len(s._closes[inst]) == T
    assert len(s._spot_closes[inst]) == T  # derived from basis_apr
    assert len(s._funding_rates[inst]) == T


def test_load_warmup_panel_populates_all_buffers(tmp_path) -> None:
    """End-to-end: build a panel, save via _save_cache, load via warmup."""
    import numpy as np
    from okx_trade.research.data import _save_cache
    from okx_trade.research.panel import FactorPanel
    from okx_trade.strategies.factor_portfolio import (
        FactorPortfolioConfig, FactorPortfolioStrategy,
    )

    # Synthetic panel with all 5 fields populated for one inst
    T = 100
    ts_axis = tuple(1_700_000_000_000 + i * 3_600_000 for i in range(T))
    panel = FactorPanel(
        inst_ids=("BTC-USDT-SWAP",),
        timestamps_ms=ts_axis,
        close=np.linspace(100.0, 120.0, T).reshape(T, 1),
        volume_usdt=np.ones((T, 1)) * 1e6,
        funding_rate=(np.ones((T, 1)) * 0.0001),
        open_interest=(np.ones((T, 1)) * 5e6),
        basis_apr=(np.ones((T, 1)) * 0.01),  # 1% premium → spot = perp / 1.01
    )
    cache_path = tmp_path / "warmup.parquet"
    _save_cache(panel, cache_path)
    assert cache_path.exists()

    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX"],  # NT requires venue; loader handles bare panel ids
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4, top_k_long=1, top_k_short=1,
        risk_pct=0.002, account_equity_usdt=10_000.0,
        factor_weights=[("basis_z_30d", 1.0)],
        subscribe_spot_for_basis=False,  # no NT subscribe in unit test
        enable_rest_polling=False,
        warmup_panel_cache_path=str(cache_path),
    )
    s = FactorPortfolioStrategy(cfg)
    # Call the loader directly (since on_start needs NT framework)
    s._load_warmup_panel(str(cache_path))

    inst = "BTC-USDT-SWAP.OKX"
    assert len(s._closes[inst]) == T
    assert len(s._volumes[inst]) == T
    assert len(s._funding_rates[inst]) == T
    assert len(s._open_interests[inst]) == T
    assert len(s._spot_closes[inst]) == T

    # Verify reconstruction: spot = perp / (1 + basis); for basis=0.01, ratio is fixed
    perp_first = s._closes[inst][0][1]
    spot_first = s._spot_closes[inst][0][1]
    assert spot_first == pytest.approx(perp_first / 1.01)


def test_load_warmup_panel_missing_file_warns_does_not_crash(tmp_path) -> None:
    """Bad path shouldn't kill the strategy."""
    from okx_trade.strategies.factor_portfolio import (
        FactorPortfolioConfig, FactorPortfolioStrategy,
    )
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        subscribe_spot_for_basis=False,
        enable_rest_polling=False,
        warmup_panel_cache_path=str(tmp_path / "nope.parquet"),
    )
    s = FactorPortfolioStrategy(cfg)
    s._load_warmup_panel(cfg.warmup_panel_cache_path)
    # All buffers stay empty
    assert len(s._closes["BTC-USDT-SWAP.OKX"]) == 0
