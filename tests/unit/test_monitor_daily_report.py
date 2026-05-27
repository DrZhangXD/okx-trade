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
# Attribution + duplication 回归（2026-05-26 prod bug：FundingCarry-000/-001
# 两条完全相同的行、StatArb 显示 0、ending_equity 来自多天前的死实例快照）
# ---------------------------------------------------------------------------


def _seed_trades_okx(tracker: PnLTracker, rows: list[tuple]) -> None:
    """直接往 trades_okx 表插 bill 行（reconcile_pnl_from_okx 路径的 fixture）。

    rows: ``[(bill_id, ts_ms, inst_id, cl_ord_id, strategy_id, bal_chg), ...]``
    """
    import sqlite3
    # tracker._conn 内部连接复用即可 —— 不另开 connection（避免 :memory: 各自孤岛）
    conn = tracker._conn  # noqa: SLF001 — 测试 fixture 直连
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trades_okx (
            bill_id TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL, inst_id TEXT,
            bill_type TEXT, sub_type TEXT, cl_ord_id TEXT, strategy_id TEXT,
            bal_chg REAL, pnl REAL, fee REAL
        )"""
    )
    for bill_id, ts_ms, inst_id, cl_ord_id, sid, bal_chg in rows:
        conn.execute(
            "INSERT INTO trades_okx VALUES (?,?,?,?,?,?,?,?,?,?)",
            (bill_id, ts_ms, inst_id, "2", "0", cl_ord_id, sid, bal_chg, 0.0, 0.0),
        )
    conn.commit()


def test_daily_report_dedupes_nt_instances_to_one_bucket(tmp_path: Path) -> None:
    """两条 NT 实例（FundingCarryStrategy-000 + -001）共享 OKX bills（reconcile
    写入 snake_case 'funding_carry'）。每个实例都模糊匹配到同一批 bills →
    旧实现会 emit 两行完全相同的 13/-pnl；修复后必须 emit 一行（per class 桶）。
    """
    tracker = PnLTracker(":memory:")
    # 两个活实例都写 equity
    for sid in ("FundingCarryStrategy-000", "FundingCarryStrategy-001"):
        tracker.record_equity(EquitySnapshot(
            strategy_id=sid, ts_ms=_ts("2026-05-26", hour=23),
            equity_usdt=Decimal("50000"),
        ))
    # OKX bills 用 snake_case bare name（reconcile 的实际写法）
    _seed_trades_okx(tracker, [
        ("b1", _ts("2026-05-26", 10), "BTC-USDT-SWAP", "cl1", "funding_carry", -10.0),
        ("b2", _ts("2026-05-26", 11), "BTC-USDT-SWAP", "cl2", "funding_carry", +3.0),
    ])
    reporter = DailyReporter(tracker, output_dir=tmp_path)
    j = json.loads(reporter.write_for_date("2026-05-26").read_text(encoding="utf-8"))

    # 关键不变量：只有 1 行 funding_carry，pnl 不被双倍记 N=2 次
    funding_rows = [r for r in j["per_strategy"] if "fund" in r["strategy_id"].lower()]
    assert len(funding_rows) == 1, f"expected 1 row per class, got {funding_rows!r}"
    assert funding_rows[0]["strategy_id"] == "funding_carry"
    assert funding_rows[0]["trade_count"] == 2
    assert funding_rows[0]["pnl_usdt"] == pytest.approx(-7.0)
    # totals 不应被 N 倍夸大
    assert j["totals_trade_count"] == 2
    assert j["totals_pnl_usdt"] == pytest.approx(-7.0)
    tracker.close()


def test_daily_report_ending_equity_ignores_stale_dead_instances(tmp_path: Path) -> None:
    """死实例（FundingCarryStrategy-001：trades 表残留但没 in-window equity）
    不能用它几天前的 equity 当 "当日 ending"。修复后必须只看 day window 内的
    快照；没新快照的桶 → ending=0（或被合并到活实例的 latest）。
    """
    tracker = PnLTracker(":memory:")
    # 活实例 -000 当日有 equity 50000
    tracker.record_equity(EquitySnapshot(
        strategy_id="OBImbalanceStrategy-004",
        ts_ms=_ts("2026-05-26", hour=20),
        equity_usdt=Decimal("50000"),
    ))
    # 死实例 -003 只在多天前有一笔 80935（不应当作 5-26 ending）
    tracker.record_equity(EquitySnapshot(
        strategy_id="OBImbalanceStrategy-003",
        ts_ms=_ts("2026-05-20", hour=12),
        equity_usdt=Decimal("80935"),
    ))
    reporter = DailyReporter(tracker, output_dir=tmp_path)
    j = json.loads(reporter.write_for_date("2026-05-26").read_text(encoding="utf-8"))

    ob_rows = [r for r in j["per_strategy"] if "ob" in r["strategy_id"].lower()]
    assert len(ob_rows) == 1, "OBImbalance 实例必须合并成 1 桶"
    # ending 来自当日活实例 -004 的 50000，绝不是 -003 几天前的 80935
    assert ob_rows[0]["ending_equity_usdt"] == 50000.0
    tracker.close()


def test_daily_report_uses_snake_case_canonical_strategy_id(tmp_path: Path) -> None:
    """per_strategy 行的 strategy_id 字段使用 snake_case canonical 名
    （与 trades_okx 一致），不是 NT 实例 id。"""
    tracker = PnLTracker(":memory:")
    tracker.record_equity(EquitySnapshot(
        strategy_id="StatArbStrategy-008", ts_ms=_ts("2026-05-26", hour=23),
        equity_usdt=Decimal("50000"),
    ))
    _seed_trades_okx(tracker, [
        ("b1", _ts("2026-05-26", 5), "ETH-USDT-SWAP", "cl1", "stat_arb_pairs", -5.0),
    ])
    reporter = DailyReporter(tracker, output_dir=tmp_path)
    j = json.loads(reporter.write_for_date("2026-05-26").read_text(encoding="utf-8"))

    ids = [r["strategy_id"] for r in j["per_strategy"]]
    assert "stat_arb_pairs" in ids
    assert all("Strategy-" not in i for i in ids), f"NT-style 后缀漏了: {ids}"
    tracker.close()


def test_daily_report_non_nt_strategy_id_passthrough(tmp_path: Path) -> None:
    """不带 'Strategy' 后缀的旧式 sid（s1/s2，多见于单测/回测）不应被改写。"""
    tracker = PnLTracker(":memory:")
    for sid, pnl in [("s1", "10"), ("s2", "-3"), ("s1", "5")]:
        tracker.record_trade(TradeRecord(
            strategy_id=sid, instrument_id="x",
            closed_ts_ms=_ts("2026-05-08"),
            pnl_usdt=Decimal(pnl), r_multiple=1.0,
        ))
    reporter = DailyReporter(tracker, output_dir=tmp_path)
    j = json.loads(reporter.write_for_date("2026-05-08").read_text(encoding="utf-8"))
    ids = {r["strategy_id"] for r in j["per_strategy"]}
    assert ids == {"s1", "s2"}
    tracker.close()


# ---------------------------------------------------------------------------
# run_loop 调度逻辑（修红灯 3a；2026-05-27 修 B3：从 0:01 → 0:10 让位 reconcile cron）
# ---------------------------------------------------------------------------


class TestNextRunAt:
    """``_next_run_at`` 时间计算 —— UTC 0:10 让位 reconcile_pnl_from_okx cron (0:05)。"""

    def test_before_today_010_returns_today_010(self) -> None:
        # 当前 UTC 23:00 < 第二天的 0:10；next_run = 明天 0:10
        cur = datetime(2026, 5, 10, 23, 0, 0, tzinfo=timezone.utc)
        nxt = DailyReporter._next_run_at(cur)
        assert nxt == datetime(2026, 5, 11, 0, 10, 0, tzinfo=timezone.utc)

    def test_at_011_returns_tomorrow_010(self) -> None:
        # 当前 UTC 0:11（刚过 0:10），等到明天 0:10
        cur = datetime(2026, 5, 10, 0, 11, 0, tzinfo=timezone.utc)
        nxt = DailyReporter._next_run_at(cur)
        assert nxt == datetime(2026, 5, 11, 0, 10, 0, tzinfo=timezone.utc)

    def test_before_010_today_returns_today_010(self) -> None:
        # 当前 UTC 0:00:30（00:10 之前），等到今天 0:10
        cur = datetime(2026, 5, 10, 0, 0, 30, tzinfo=timezone.utc)
        nxt = DailyReporter._next_run_at(cur)
        assert nxt == datetime(2026, 5, 10, 0, 10, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_run_loop_writes_yesterday_at_first_tick(tmp_path: Path) -> None:
    """run_loop 第一次唤醒（UTC 0:10）时写"昨天"的报告。"""
    tracker = PnLTracker(":memory:")
    reporter = DailyReporter(tracker, output_dir=tmp_path)

    # 模拟时间：当前 UTC 23:55，等 15 分钟后到第二天 0:10
    times = iter([
        datetime(2026, 5, 10, 23, 55, 0, tzinfo=timezone.utc),   # entry
        datetime(2026, 5, 11, 0, 10, 0, tzinfo=timezone.utc),    # after sleep, write
        datetime(2026, 5, 11, 0, 10, 0, tzinfo=timezone.utc),    # next iter entry
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

    # 第一次 sleep 应该是 15 分钟（900s）：23:55 → 次日 0:10
    assert sleep_calls[0] == 900.0
    # 写了一份"昨天"的报告 = 2026-05-10（now=2026-05-11 0:10 - 1h = 2026-05-10 23:10）
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
        datetime(2026, 5, 11, 0, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 11, 0, 10, 0, tzinfo=timezone.utc),
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
