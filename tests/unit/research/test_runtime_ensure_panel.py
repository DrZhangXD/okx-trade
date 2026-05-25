"""Tests for ensure_panel_cached: cache-hit short-circuits REST, cache-miss invokes fetch_panel."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_ensure_panel_cached_skips_fetch_when_cache_present(tmp_path, monkeypatch):
    from okx_trade.research.data import _cache_key
    from okx_trade.research.runtime import ensure_panel_cached
    import okx_trade.research.runtime as runtime_mod

    inst_ids = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    bar = "1H"
    start_ms, end_ms = 1_700_000_000_000, 1_700_500_000_000
    include = ("close", "volume_usdt", "funding_rate")
    key = _cache_key(inst_ids, bar, start_ms, end_ms, include)
    cache_path = tmp_path / f"panel_{key}.parquet"
    cache_path.write_bytes(b"\x00" * 16)

    sentinel = AsyncMock(side_effect=AssertionError("fetch_panel must not be called on cache hit"))
    monkeypatch.setattr(runtime_mod, "fetch_panel", sentinel)

    fake_rest = AsyncMock()
    res = await ensure_panel_cached(
        rest_client=fake_rest, inst_ids=inst_ids, bar=bar,
        start_ms=start_ms, end_ms=end_ms, include=include,
        cache_dir=tmp_path,
    )
    assert res == cache_path
    sentinel.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_panel_cached_fetches_when_missing(tmp_path, monkeypatch):
    from okx_trade.research.data import _cache_key
    from okx_trade.research.runtime import ensure_panel_cached
    import okx_trade.research.runtime as runtime_mod

    inst_ids = ["X"]
    bar = "1H"
    start_ms, end_ms = 1, 2
    include = ("close",)
    expected_key = _cache_key(inst_ids, bar, start_ms, end_ms, include)
    expected_path = tmp_path / f"panel_{expected_key}.parquet"

    async def fake_fetch_panel(*, rest_client, inst_ids, start_ms, end_ms, bar, include, cache_dir):
        # Simulate the side effect: write the parquet at the deterministic location.
        key = _cache_key(inst_ids, bar, start_ms, end_ms, tuple(include))
        (cache_dir / f"panel_{key}.parquet").write_bytes(b"\x00")
        return None

    monkeypatch.setattr(runtime_mod, "fetch_panel", fake_fetch_panel)

    fake_rest = AsyncMock()
    res = await ensure_panel_cached(
        rest_client=fake_rest, inst_ids=inst_ids, bar=bar,
        start_ms=start_ms, end_ms=end_ms, include=include, cache_dir=tmp_path,
    )
    assert res == expected_path
    assert res.exists()


@pytest.mark.asyncio
async def test_ensure_panel_cached_raises_if_fetch_does_not_produce_file(tmp_path, monkeypatch):
    from okx_trade.research.runtime import ensure_panel_cached
    import okx_trade.research.runtime as runtime_mod

    async def fake_fetch_panel_no_write(**kw):
        return None  # writes nothing

    monkeypatch.setattr(runtime_mod, "fetch_panel", fake_fetch_panel_no_write)

    with pytest.raises(RuntimeError, match="fetch_panel did not produce expected cache"):
        await ensure_panel_cached(
            rest_client=AsyncMock(), inst_ids=["X"], bar="1H",
            start_ms=0, end_ms=1, include=("close",), cache_dir=tmp_path,
        )
