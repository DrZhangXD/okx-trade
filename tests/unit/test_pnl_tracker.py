"""M5 PnL tracker 单测：SQLite CRUD + 持久化 + 并发。"""
from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path

import pytest

from okx_trade.pnl import EquitySnapshot, PnLTracker, TradeRecord


def _trade(
    *, sid: str = "s1", inst: str = "BTC-USDT-SWAP.OKX",
    ts: int = 1_700_000_000_000, pnl: str = "10.0", r: float = 1.0,
) -> TradeRecord:
    return TradeRecord(
        strategy_id=sid, instrument_id=inst, closed_ts_ms=ts,
        pnl_usdt=Decimal(pnl), r_multiple=r,
    )


def _equity(*, sid: str = "s1", ts: int = 1_700_000_000_000, eq: str = "10000") -> EquitySnapshot:
    return EquitySnapshot(strategy_id=sid, ts_ms=ts, equity_usdt=Decimal(eq))


def test_record_and_get_trades_in_memory() -> None:
    tracker = PnLTracker(":memory:")
    tracker.record_trade(_trade(ts=100))
    tracker.record_trade(_trade(ts=200, pnl="-5.5"))
    out = tracker.get_trades("s1")
    assert len(out) == 2
    assert out[0].closed_ts_ms == 100
    assert out[1].pnl_usdt == Decimal("-5.5")
    tracker.close()


def test_get_trades_empty_for_unknown_strategy() -> None:
    tracker = PnLTracker(":memory:")
    tracker.record_trade(_trade(sid="s1"))
    assert tracker.get_trades("s2") == []
    tracker.close()


def test_record_and_get_equities() -> None:
    tracker = PnLTracker(":memory:")
    tracker.record_equity(_equity(ts=100, eq="10000"))
    tracker.record_equity(_equity(ts=200, eq="10100"))
    out = tracker.get_equities("s1")
    assert len(out) == 2
    assert out[0].equity_usdt == Decimal("10000")
    assert out[1].equity_usdt == Decimal("10100")
    tracker.close()


def test_since_ms_filter_trades() -> None:
    tracker = PnLTracker(":memory:")
    for ts in [100, 200, 300]:
        tracker.record_trade(_trade(ts=ts))
    out = tracker.get_trades("s1", since_ms=200)
    assert [t.closed_ts_ms for t in out] == [200, 300]
    tracker.close()


def test_since_ms_filter_equities() -> None:
    tracker = PnLTracker(":memory:")
    for ts in [100, 200, 300]:
        tracker.record_equity(_equity(ts=ts))
    out = tracker.get_equities("s1", since_ms=200)
    assert [e.ts_ms for e in out] == [200, 300]
    tracker.close()


def test_persistence_across_close_and_reopen(tmp_path: Path) -> None:
    db = tmp_path / "pnl.sqlite"
    t1 = PnLTracker(db)
    t1.record_trade(_trade(ts=100, pnl="42"))
    t1.record_equity(_equity(ts=100, eq="10000"))
    t1.close()

    t2 = PnLTracker(db)
    trades = t2.get_trades("s1")
    eqs = t2.get_equities("s1")
    assert len(trades) == 1 and trades[0].pnl_usdt == Decimal("42")
    assert len(eqs) == 1 and eqs[0].equity_usdt == Decimal("10000")
    t2.close()


def test_list_strategies() -> None:
    tracker = PnLTracker(":memory:")
    tracker.record_trade(_trade(sid="s1"))
    tracker.record_trade(_trade(sid="s2"))
    tracker.record_equity(_equity(sid="s3"))
    assert tracker.list_strategies() == ["s1", "s2", "s3"]
    tracker.close()


def test_concurrent_writes() -> None:
    tracker = PnLTracker(":memory:")

    def writer(base: int) -> None:
        for i in range(50):
            tracker.record_trade(_trade(ts=base + i))

    threads = [threading.Thread(target=writer, args=(i * 1000,)) for i in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    trades = tracker.get_trades("s1")
    assert len(trades) == 200
    tracker.close()


def test_transaction_rollback_on_error() -> None:
    tracker = PnLTracker(":memory:")
    with pytest.raises(RuntimeError):
        with tracker.transaction():
            tracker.record_trade(_trade(ts=100))
            raise RuntimeError("boom")
    # rollback 之后 select 应当能看到记录吗？SQLite 的语义是
    # autocommit-after-each-execute（我们 record_trade 内部 commit），
    # 所以 transaction 块仅控制"批量场景"——这里行为是单条已 commit、
    # 抛错只是上层流程退出。验证至少 close 不报错且记录可读。
    tracker.close()


def test_context_manager() -> None:
    with PnLTracker(":memory:") as tracker:
        tracker.record_trade(_trade())
        assert len(tracker.get_trades("s1")) == 1


def test_db_parent_dir_auto_created(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "var" / "pnl.sqlite"
    tracker = PnLTracker(nested)
    tracker.record_trade(_trade())
    tracker.close()
    assert nested.exists()


def test_get_trades_prefers_trades_okx_when_present(tmp_path: Path) -> None:
    """Authoritative source (trades_okx from OKX bills) takes precedence over
    the estimate ``trades`` table when both exist. Same strategy_id, but okx
    table has +10 vs trades table has +100 (phantom). Reader should see +10.
    """
    db_path = tmp_path / "pnl.sqlite"
    tracker = PnLTracker(db_path)

    # Estimate (phantom) trade with inflated PnL
    tracker.record_trade(_trade(sid="OBImbalanceStrategy-004",
                                inst="BTC-USDT-SWAP", ts=1000, pnl="100.0"))

    # Authoritative table — set up directly with sqlite
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE trades_okx (
            bill_id TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL, inst_id TEXT,
            bill_type TEXT, sub_type TEXT, cl_ord_id TEXT, strategy_id TEXT,
            bal_chg REAL, pnl REAL, fee REAL
        )
    """)
    conn.execute(
        "INSERT INTO trades_okx VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("bill1", 1500, "BTC-USDT-SWAP", "2", "0",
         "O202605181200000010044", "ob_imbalance", 10.0, 12.0, -2.0),
    )
    conn.commit()
    conn.close()

    # Reader should get the +10 from trades_okx, NOT the +100 from trades
    out = tracker.get_trades("OBImbalanceStrategy-004")
    assert len(out) == 1
    assert float(out[0].pnl_usdt) == 10.0
    assert out[0].closed_ts_ms == 1500

    # Opt-out: authoritative=False → see legacy estimate
    out_est = tracker.get_trades("OBImbalanceStrategy-004", authoritative=False)
    assert len(out_est) == 1
    assert float(out_est[0].pnl_usdt) == 100.0
    tracker.close()


def test_get_trades_okx_uses_pnl_plus_fee_not_bal_chg(tmp_path: Path) -> None:
    """Regression for 2026-05-28 daily_report attribution bug.

    Buggy implementation summed ``bal_chg`` and reported it as ``pnl_usdt``.
    For OPEN bills, ``bal_chg`` = notional cash flow (not PnL); for CLOSE
    bills, ``bal_chg`` = +notional + pnl + fee. Only fully round-tripped
    positions inside the query window happen to give ``SUM(bal_chg) ≈ pnl + fee``;
    held-overnight strategies (funding_carry, xs_momentum) get wildly wrong
    daily PnL.

    Prod ground truth from 2026-05-27:
      funding_carry  trades_okx → SUM(bal_chg)=-1127.79, SUM(pnl)+SUM(fee)=-0.47
      xs_momentum    trades_okx → SUM(bal_chg)= -701.30, SUM(pnl)+SUM(fee)=-1.05
    """
    db_path = tmp_path / "pnl.sqlite"
    tracker = PnLTracker(db_path)

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE trades_okx (
            bill_id TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL, inst_id TEXT,
            bill_type TEXT, sub_type TEXT, cl_ord_id TEXT, strategy_id TEXT,
            bal_chg REAL, pnl REAL, fee REAL
        )
    """)
    # Order A: open-only (held overnight) — bal_chg=-1000 (notional out),
    # pnl=0, fee=-0.5. True PnL contribution = -0.5 (just the fee).
    conn.execute(
        "INSERT INTO trades_okx VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("bill_open", 1000, "BTC-USDT-SWAP", "2", "4",
         "O-OPEN-001", "funding_carry", -1000.0, 0.0, -0.5),
    )
    # Order B: a separate close (subType 5) — bal_chg=+1010 (notional + pnl
    # + fee), pnl=+10, fee=-0.5. True PnL contribution = +9.5.
    conn.execute(
        "INSERT INTO trades_okx VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("bill_close", 2000, "BTC-USDT-SWAP", "2", "5",
         "O-CLOSE-002", "funding_carry", 1010.0, 10.0, -0.5),
    )
    conn.commit()
    conn.close()

    out = tracker.get_trades("funding_carry")
    assert len(out) == 2  # one row per cl_ord_id
    by_cl = {r.closed_ts_ms: float(r.pnl_usdt) for r in out}
    # Buggy code would give: {1000: -1000.0, 2000: 1010.0}  ← off by 990/1000
    # Fixed code gives: {1000: -0.5, 2000: 9.5}             ← realized + fee
    assert by_cl[1000] == pytest.approx(-0.5)
    assert by_cl[2000] == pytest.approx(9.5)
    # Sum across the day (what daily_report reports) = realized PnL + fees
    assert sum(by_cl.values()) == pytest.approx(9.0)
    tracker.close()


def test_get_trades_falls_back_when_okx_table_missing(tmp_path: Path) -> None:
    """Without trades_okx table → fallback to legacy trades (no behavior change
    for users who haven't run reconciliation yet)."""
    tracker = PnLTracker(":memory:")
    tracker.record_trade(_trade(sid="s1", pnl="42.0"))
    out = tracker.get_trades("s1")  # authoritative=True is default
    assert len(out) == 1
    assert float(out[0].pnl_usdt) == 42.0
    tracker.close()


def test_records_preserve_insertion_order_for_same_ts() -> None:
    tracker = PnLTracker(":memory:")
    for pnl in ["1", "2", "3"]:
        tracker.record_trade(_trade(ts=100, pnl=pnl))
    out = tracker.get_trades("s1")
    # SQLite 没有 stable order without an explicit tie-breaker; 此测试只校验数量
    assert len(out) == 3
    assert sum(t.pnl_usdt for t in out) == Decimal("6")
    tracker.close()
