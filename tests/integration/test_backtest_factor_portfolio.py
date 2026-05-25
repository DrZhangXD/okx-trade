"""Integration tests for factor_portfolio backtest pipeline.

Per the integration-test policy (Plans 1-3 lesson): exercises the realistic
disk → parquet → runtime preflight chain WITHOUT spinning up the NT BacktestNode
(which would require fixture catalog bars). The full engine path is verified
by the CLI smoke in scripts/backtest.py and the unit tests for cmd_backtest_portfolio.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import numpy as np
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_ensure_panel_cached_hits_preseeded_cache(tmp_path, monkeypatch):
    """Pre-seed a synthetic FactorPanel parquet at the deterministic cache key;
    ensure_panel_cached must return the pre-seeded path without invoking REST."""
    from okx_trade.research.data import _cache_key, _save_cache
    from okx_trade.research.panel import FactorPanel
    from okx_trade.research.runtime import ensure_panel_cached
    import okx_trade.research.runtime as runtime_mod

    inst_ids = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")
    bar = "1H"
    start_ms, end_ms = 1_700_000_000_000, 1_700_500_000_000
    include = ("close", "volume_usdt", "funding_rate", "open_interest")
    T = 100
    timestamps = tuple(start_ms + i * 3_600_000 for i in range(T))
    rng = np.random.default_rng(42)
    panel = FactorPanel(
        inst_ids=inst_ids,
        timestamps_ms=timestamps,
        close=rng.uniform(20_000, 70_000, size=(T, len(inst_ids))),
        volume_usdt=rng.uniform(1e7, 1e9, size=(T, len(inst_ids))),
        funding_rate=rng.uniform(-1e-3, 1e-3, size=(T, len(inst_ids))),
        open_interest=rng.uniform(1e6, 1e8, size=(T, len(inst_ids))),
        basis_apr=None,
    )

    key = _cache_key(list(inst_ids), bar, start_ms, end_ms, include)
    cache_path = tmp_path / f"panel_{key}.parquet"
    _save_cache(panel, cache_path)
    assert cache_path.exists() and cache_path.stat().st_size > 0

    # Sentinel: fetch_panel must not be called on cache hit.
    sentinel = AsyncMock(side_effect=AssertionError("REST fetch_panel called on cache hit"))
    monkeypatch.setattr(runtime_mod, "fetch_panel", sentinel)

    fake_rest = AsyncMock()
    res = await ensure_panel_cached(
        rest_client=fake_rest, inst_ids=list(inst_ids), bar=bar,
        start_ms=start_ms, end_ms=end_ms, include=include,
        cache_dir=tmp_path,
    )
    assert res == cache_path
    sentinel.assert_not_called()


def _run_backtest_cli(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "backtest.py"), *args],
        capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT),
    )


def test_run_factor_portfolio_errors_on_missing_yaml():
    res = _run_backtest_cli([
        "--strategy", "factor_portfolio",
        "--factor-yaml", "/nonexistent/factor.yaml",
    ])
    assert res.returncode != 0
    combined = (res.stdout or "") + (res.stderr or "")
    assert "factor_portfolio yaml not found" in combined, combined


def test_run_factor_portfolio_errors_on_empty_factor_weights(tmp_path):
    yaml_path = tmp_path / "empty_factor.yaml"
    yaml_path.write_text(
        "instrument_ids:\n  - BTC-USDT-SWAP.OKX\nfactor_weights: []\n"
    )
    res = _run_backtest_cli([
        "--strategy", "factor_portfolio",
        "--factor-yaml", str(yaml_path),
    ])
    assert res.returncode != 0
    combined = (res.stdout or "") + (res.stderr or "")
    assert "empty factor_weights" in combined, combined
