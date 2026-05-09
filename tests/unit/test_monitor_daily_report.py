"""M5 monitor.daily_report 单测。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# run_loop 调度逻辑（修红灯 3a）
# ---------------------------------------------------------------------------


class TestNextRunAt:
    """``_next_run_at`` 时间计算 —— 调度的核心，下一次写报告时刻。"""

    def test_before_today_001_returns_today_001(self) -> None:
        # 当前 UTC 23:00 < 第二天的 0:01 —— 但还在"今天"内；
        # next_run 应当是"明天 0:01"（因为 today_001 是今天 0:01，已过）
        cur = datetime(2026, 5, 10, 23, 0, 0, tzinfo=timezone.utc)
        nxt = DailyReporter._next_run_at(cur)
        assert nxt == datetime(2026, 5, 11, 0, 1, 0, tzinfo=timezone.utc)

    def test_at_002_returns_tomorrow_001(self) -> None:
        # 当前 UTC 0:02（刚过 0:01），等到明天 0:01
        cur = datetime(2026, 5, 10, 0, 2, 0, tzinfo=timezone.utc)
        nxt = DailyReporter._next_run_at(cur)
        assert nxt == datetime(2026, 5, 11, 0, 1, 0, tzinfo=timezone.utc)

    def test_before_001_today_returns_today_001(self) -> None:
        # 当前 UTC 0:00:30（00:01 之前），等到今天 0:01
        cur = datetime(2026, 5, 10, 0, 0, 30, tzinfo=timezone.utc)
        nxt = DailyReporter._next_run_at(cur)
        assert nxt == datetime(2026, 5, 10, 0, 1, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_run_loop_writes_yesterday_at_first_tick(tmp_path: Path) -> None:
    """run_loop 第一次唤醒（UTC 0:01）时写"昨天"的报告。"""
    tracker = PnLTracker(":memory:")
    reporter = DailyReporter(tracker, output_dir=tmp_path)

    # 模拟时间：当前 UTC 23:55，等 6 分钟后到第二天 0:01
    times = iter([
        datetime(2026, 5, 10, 23, 55, 0, tzinfo=timezone.utc),  # entry
        datetime(2026, 5, 11, 0, 1, 0, tzinfo=timezone.utc),    # after sleep, write
        datetime(2026, 5, 11, 0, 1, 0, tzinfo=timezone.utc),    # next iter entry
    ])

    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)
        # 第二次循环时让 task 取消，避免无限跑
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError

    written: list[Path] = []

    with pytest.raises(asyncio.CancelledError):
        await reporter.run_loop(
            sleep_for=fake_sleep,
            now=lambda: next(times),
            on_write=written.append,
        )

    # 第一次 sleep 应该是 6 分钟（360s）
    assert sleep_calls[0] == 360.0
    # 写了一份"昨天"的报告 = 2026-05-10（now=2026-05-11 0:01 - 1h = 2026-05-10 23:01）
    assert len(written) == 1
    assert written[0].name == "2026-05-10.json"
    assert written[0].exists()
    tracker.close()


@pytest.mark.asyncio
async def test_run_loop_swallows_write_exception(tmp_path: Path) -> None:
    """写报告失败不能让 task 挂掉 —— on_error 回调，循环继续。"""
    tracker = PnLTracker(":memory:")
    reporter = DailyReporter(tracker, output_dir=tmp_path)

    # 让 write_for_date 抛
    def boom(_date_str: str) -> Path:
        raise RuntimeError("disk full")

    reporter.write_for_date = boom  # type: ignore[method-assign]

    times = iter([
        datetime(2026, 5, 10, 23, 55, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 11, 0, 1, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 11, 0, 1, 0, tzinfo=timezone.utc),
    ])

    sleep_calls: list[float] = []
    errors: list[BaseException] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await reporter.run_loop(
            sleep_for=fake_sleep,
            now=lambda: next(times),
            on_error=errors.append,
        )

    # write 抛了，但 loop 没崩，进入第二次 sleep
    assert len(sleep_calls) == 2
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "disk full" in str(errors[0])
    tracker.close()
