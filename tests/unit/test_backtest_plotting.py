"""Unit tests for okx_trade.backtest.plotting."""
from __future__ import annotations

import pandas as pd
import pytest

plotly = pytest.importorskip("plotly")  # noqa: F841 — skip suite if not installed

from okx_trade.backtest.plotting import plot_equity_curve_html


def _make_equity_df() -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=24, freq="1h", tz="UTC")
    # 模拟一段上涨 → 回撤 → 反弹的净值
    totals = [10000.0]
    deltas = [+50, +80, +120, +90, -200, -150, -300, -120, +60, +90, +110,
              +130, +60, -90, -50, +40, +120, +180, +90, +60, +40, +20, +10, +5]
    for d in deltas[:-1]:
        totals.append(totals[-1] + d)
    return pd.DataFrame(
        {"total": totals, "free": totals, "locked": [0.0] * len(totals)},
        index=idx,
    )


def test_plot_equity_curve_html_writes_file(tmp_path):
    df = _make_equity_df()
    out = plot_equity_curve_html(
        df,
        output_path=tmp_path / "equity.html",
        title="test",
        starting_balance=10000.0,
        subtitle="PnL +0.5%",
    )
    assert out.exists()
    content = out.read_text()
    assert out.stat().st_size > 1000  # plotly HTML 至少几 KB
    assert "plotly" in content.lower()
    assert "Equity" in content
    assert "Drawdown" in content


def test_plot_equity_curve_html_empty_raises(tmp_path):
    with pytest.raises(ValueError):
        plot_equity_curve_html(
            pd.DataFrame(), output_path=tmp_path / "empty.html",
        )


def test_plot_equity_curve_html_missing_total_raises(tmp_path):
    bad = pd.DataFrame({"free": [1.0, 2.0]})
    with pytest.raises(ValueError):
        plot_equity_curve_html(bad, output_path=tmp_path / "bad.html")
