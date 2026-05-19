"""Tests for the factor sqlite store."""
from __future__ import annotations

from pathlib import Path

import pytest

from okx_trade.research.store import FactorStore, GradeRecord


@pytest.fixture
def store(tmp_path: Path) -> FactorStore:
    return FactorStore(tmp_path / "zoo.db")


def test_store_creates_schema_on_first_use(store: FactorStore) -> None:
    store.init_schema()
    # Should be idempotent
    store.init_schema()
    assert store.list_approved() == []


def test_upsert_factor_then_list(store: FactorStore) -> None:
    store.init_schema()
    store.upsert_factor(
        id="momentum_7d", category="momentum",
        direction="long_high", description="7d momentum",
    )
    rows = store.list_factors()
    assert len(rows) == 1
    assert rows[0]["id"] == "momentum_7d"
    assert rows[0]["approved"] == 0


def test_save_grade_appends_row(store: FactorStore) -> None:
    store.init_schema()
    store.upsert_factor(id="f", category="t", direction="long_high", description="")
    rec = GradeRecord(
        factor_id="f", panel_start_ms=1, panel_end_ms=2, horizon_bars=24,
        ic_mean=0.04, ic_std=0.08, ir=0.5, ic_t_stat=3.2, ic_positive_rate=0.6,
        turnover_avg=0.2, autocorr_1=0.4,
        long_short_spread=0.001, net_ls_spread_after_fees=0.0005,
        n_periods=100, n_instruments=10, verdict="pass",
        graded_at_ms=1_700_000_000_000,
    )
    store.save_grade(rec)
    history = store.grade_history("f")
    assert len(history) == 1
    assert history[0]["verdict"] == "pass"


def test_approve_writes_weight_and_timestamp(store: FactorStore) -> None:
    store.init_schema()
    store.upsert_factor(id="f", category="t", direction="long_high", description="")
    store.approve("f", weight=0.25, ts_ms=1_700_000_000_000)
    approved = store.list_approved()
    assert len(approved) == 1
    assert approved[0]["id"] == "f"
    assert approved[0]["approved_weight"] == pytest.approx(0.25)


def test_reject_clears_approval(store: FactorStore) -> None:
    store.init_schema()
    store.upsert_factor(id="f", category="t", direction="long_high", description="")
    store.approve("f", weight=0.25, ts_ms=1)
    store.reject("f")
    assert store.list_approved() == []
