"""Tests for markdown report rendering."""
from __future__ import annotations

from dataclasses import replace

from okx_trade.research.grade import FactorGrade
from okx_trade.research.report import render_grade_report


def _fake_grade() -> FactorGrade:
    return FactorGrade(
        factor_id="momentum_7d",
        panel_start_ms=1_700_000_000_000,
        panel_end_ms=1_715_000_000_000,
        horizon_bars=24,
        ic_mean=0.045, ic_std=0.082, ir=0.549, ic_t_stat=4.31, ic_positive_rate=0.62,
        ic_decay=[0.05, 0.04, 0.03, 0.02, 0.01, 0.005],
        turnover_avg=0.22, autocorr_1=0.45,
        long_short_spread=0.0018, net_ls_spread_after_fees=0.0011,
        n_periods=4320, n_instruments=30,
        verdict="pass", graded_at_ms=1_716_000_000_000,
    )


def test_render_grade_report_includes_factor_id_and_verdict() -> None:
    md = render_grade_report(_fake_grade())
    assert "# Factor Grade: momentum_7d" in md
    assert "PASS" in md
    assert "ic_mean" in md and "0.045" in md
    # Decay table has 6 horizons
    assert "32" in md  # last decay bucket header


def test_render_grade_report_marks_fail() -> None:
    g = _fake_grade()
    g_fail = replace(g, verdict="fail")
    md = render_grade_report(g_fail)
    assert "FAIL" in md
