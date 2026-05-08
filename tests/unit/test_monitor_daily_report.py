"""M5 monitor.daily_report 单测。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from okx_trade.monitor import DailyReporter
from okx_trade.pnl import EquitySnapshot, PnLTracker, TradeRecord


def _ts(date_str: str, hour: int = 12) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hour, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def test_daily_report_empty_strategies(tmp_path: Path) -> None:
    tracker = PnLTracker(":memory:")
    reporter = DailyReporter(tracker, output_dir=tmp_path)
    p = reporter.write_for_date("2026-05-08")
    assert p.exists()
    j = json.loads(p.read_text(encoding="utf-8"))
    assert j["date"] == "2026-05-08"
    assert j["per_strategy"] == []
    assert j["totals_trade_count"] == 0
    tracker.close()


def test_daily_report_single_trade(tmp_path: Path) -> None:
    tracker = PnLTracker(":memory:")
    tracker.record_trade(TradeRecord(
        strategy_id="s1", instrument_id="x",
        closed_ts_ms=_ts("2026-05-08"), pnl_usdt=Decimal("42"), r_multiple=2.0,
    ))
    tracker.record_equity(EquitySnapshot(
        strategy_id="s1", ts_ms=_ts("2026-05-08", hour=23),
        equity_usdt=Decimal("10042"),
    ))
    reporter = DailyReporter(tracker, output_dir=tmp_path)
    p = reporter.write_for_date("2026-05-08")
    j = json.loads(p.read_text(encoding="utf-8"))
    assert len(j["per_strategy"]) == 1
    s = j["per_strategy"][0]
    assert s["strategy_id"] == "s1"
    assert s["trade_count"] == 1
    assert s["pnl_usdt"] == 42.0
    assert s["win_rate"] == 1.0
    assert s["ending_equity_usdt"] == 10042.0
    assert j["totals_pnl_usdt"] == 42.0
    tracker.close()


def test_daily_report_filters_by_date(tmp_path: Path) -> None:
    tracker = PnLTracker(":memory:")
    # 一笔在目标日，一笔在前一日
    tracker.record_trade(TradeRecord(
        strategy_id="s1", instrument_id="x",
        closed_ts_ms=_ts("2026-05-07"), pnl_usdt=Decimal("100"), r_multiple=1.0,
    ))
    tracker.record_trade(TradeRecord(
        strategy_id="s1", instrument_id="x",
        closed_ts_ms=_ts("2026-05-08"), pnl_usdt=Decimal("50"), r_multiple=1.0,
    ))
    reporter = DailyReporter(tracker, output_dir=tmp_path)
    j = json.loads(reporter.write_for_date("2026-05-08").read_text(encoding="utf-8"))
    assert j["per_strategy"][0]["trade_count"] == 1
    assert j["per_strategy"][0]["pnl_usdt"] == 50.0
    tracker.close()


def test_daily_report_multiple_strategies_aggregate(tmp_path: Path) -> None:
    tracker = PnLTracker(":memory:")
    for sid, pnl in [("s1", "10"), ("s2", "-3"), ("s1", "5")]:
        tracker.record_trade(TradeRecord(
            strategy_id=sid, instrument_id="x",
            closed_ts_ms=_ts("2026-05-08"),
            pnl_usdt=Decimal(pnl), r_multiple=1.0,
        ))
    reporter = DailyReporter(tracker, output_dir=tmp_path)
    j = json.loads(reporter.write_for_date("2026-05-08").read_text(encoding="utf-8"))
    assert j["totals_trade_count"] == 3
    assert j["totals_pnl_usdt"] == 12.0  # 10 - 3 + 5
    tracker.close()


def test_output_dir_auto_created(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "reports"
    tracker = PnLTracker(":memory:")
    DailyReporter(tracker, output_dir=nested).write_for_date("2026-05-08")
    assert (nested / "2026-05-08.json").exists()
    tracker.close()
