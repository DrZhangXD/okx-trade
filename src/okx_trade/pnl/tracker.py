"""PnL 持久化（SQLite 后端）。

设计
----
两张表：

- ``trades``：每笔已实现 PnL 的成交（``strategy_id``, ``instrument_id``,
  ``closed_ts_ms``, ``pnl_usdt``, ``r_multiple``）；用于算 Kelly 的 win_rate / avg_R。
- ``equities``：每个策略的净值快照（``strategy_id``, ``ts_ms``, ``equity_usdt``）；
  用于算 daily returns 喂 Correlation。

PnL tracker 设计成**线程安全**（``threading.RLock``），因为 NT live engine 的
``on_position_closed`` 回调可能跨线程调度，``LiveMonitor`` 也会从异步线程读。
SQLite ``check_same_thread=False`` + 写时持锁覆盖大部分场景。

为什么用 SQLite 而不是 JSON / parquet？
------------------------------------
- 跨进程重启无需手动重放：直接 ``SELECT`` 历史数据；
- 索引 + ``since_ms`` 过滤性能好；
- stdlib，零依赖；
- 文件单一，方便备份 / 同步。
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """一笔已平仓成交的 PnL 记录。

    Attributes:
        strategy_id: 策略实例 id（``RiskIntent.strategy_id`` 同源）。
        instrument_id: 标的 id（NT 形式，如 ``BTC-USDT-SWAP.OKX``）。
        closed_ts_ms: 平仓时刻（unix ms）。
        pnl_usdt: 已实现 PnL（USDT，正负皆可）。
        r_multiple: PnL / 入场风险（=止损距离 × 仓位价值）。用于 Kelly 的
            ``avg_r`` 估计。如果策略没有止损（如 funding_carry 的双腿对冲），
            可填 0.0，stats 计算时会按 win/loss 比近似。
    """

    strategy_id: str
    instrument_id: str
    closed_ts_ms: int
    pnl_usdt: Decimal
    r_multiple: float


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    """某策略在某时刻的净值快照（用于算 daily returns）。"""

    strategy_id: str
    ts_ms: int
    equity_usdt: Decimal


_SCHEMA_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    strategy_id    TEXT NOT NULL,
    instrument_id  TEXT NOT NULL,
    closed_ts_ms   INTEGER NOT NULL,
    pnl_usdt       REAL NOT NULL,
    r_multiple     REAL NOT NULL
)
"""
_SCHEMA_EQUITIES = """
CREATE TABLE IF NOT EXISTS equities (
    strategy_id    TEXT NOT NULL,
    ts_ms          INTEGER NOT NULL,
    equity_usdt    REAL NOT NULL
)
"""
_INDEX_TRADES = "CREATE INDEX IF NOT EXISTS idx_trades_sid_ts ON trades(strategy_id, closed_ts_ms)"
_INDEX_EQUITIES = "CREATE INDEX IF NOT EXISTS idx_equities_sid_ts ON equities(strategy_id, ts_ms)"


class PnLTracker:
    """SQLite-backed PnL / equity 记录器。

    Args:
        db_path: SQLite 文件路径。``":memory:"`` 表示内存库（测试用）。
            父目录不存在会自动创建。

    用法::

        tracker = PnLTracker("var/pnl.sqlite")
        tracker.record_trade(TradeRecord(...))
        tracker.record_equity(EquitySnapshot(...))
        trades = tracker.get_trades("strategy_id_xxx")
        tracker.close()
    """

    def __init__(self, db_path: str | Path = "var/pnl.sqlite") -> None:
        self.db_path = Path(db_path) if db_path != ":memory:" else db_path
        if isinstance(self.db_path, Path):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        path_arg = str(self.db_path) if isinstance(self.db_path, Path) else self.db_path
        # check_same_thread=False：允许跨线程使用，配合 _lock 保证写安全
        self._conn = sqlite3.connect(path_arg, check_same_thread=False)
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(_SCHEMA_TRADES)
            self._conn.execute(_SCHEMA_EQUITIES)
            self._conn.execute(_INDEX_TRADES)
            self._conn.execute(_INDEX_EQUITIES)
            self._conn.commit()

    def record_trade(self, record: TradeRecord) -> None:
        """追加一笔成交记录。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO trades(strategy_id, instrument_id, closed_ts_ms, "
                "pnl_usdt, r_multiple) VALUES (?, ?, ?, ?, ?)",
                (
                    record.strategy_id,
                    record.instrument_id,
                    int(record.closed_ts_ms),
                    float(record.pnl_usdt),
                    float(record.r_multiple),
                ),
            )
            self._conn.commit()

    def record_equity(self, snap: EquitySnapshot) -> None:
        """追加一笔净值快照。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO equities(strategy_id, ts_ms, equity_usdt) VALUES (?, ?, ?)",
                (snap.strategy_id, int(snap.ts_ms), float(snap.equity_usdt)),
            )
            self._conn.commit()

    def get_trades(
        self,
        strategy_id: str,
        *,
        since_ms: int | None = None,
        authoritative: bool = True,
    ) -> list[TradeRecord]:
        """按 strategy_id 拉成交，按时间升序。

        ``authoritative=True`` (default) → 优先读 ``trades_okx``（由
        ``scripts/reconcile_pnl_from_okx.py`` 从 OKX bills 写入的权威账本），
        无该表 / 该表对此 strategy_id 无数据时回退到 ``trades``（策略侧 bar-
        price 估算）。``authoritative=False`` 直接读 ``trades`` 表（保留旧
        行为给单测 / 回测）。

        为什么默认走 trades_okx：策略侧 ``record_strategy_trade`` 在
        ``submit_order`` 后立即写 record，不等 OrderFilled，且用 bar.close
        而非真实 fill 价。OBImbalance 5/18 写 485 条但 OKX 实际 31 笔，
        PnL 估算 +6340 vs 实际 -754，差 +7094 USDT。trades_okx 用 OKX bills
        作权威源避免 Kelly handoff 用脏数据。
        """
        with self._lock:
            # Phase 0: try authoritative trades_okx if it exists
            if authoritative:
                has_okx = self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='trades_okx'"
                ).fetchone()
                if has_okx:
                    okx_rows = self._fetch_trades_okx(strategy_id, since_ms)
                    if okx_rows:
                        return okx_rows
                    # No bill rows for this strategy → fall through to legacy
                    # (most likely a strategy that never had OKX activity)

            if since_ms is None:
                cur = self._conn.execute(
                    "SELECT strategy_id, instrument_id, closed_ts_ms, pnl_usdt, r_multiple "
                    "FROM trades WHERE strategy_id = ? ORDER BY closed_ts_ms ASC",
                    (strategy_id,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT strategy_id, instrument_id, closed_ts_ms, pnl_usdt, r_multiple "
                    "FROM trades WHERE strategy_id = ? AND closed_ts_ms >= ? "
                    "ORDER BY closed_ts_ms ASC",
                    (strategy_id, int(since_ms)),
                )
            rows = cur.fetchall()
        return [
            TradeRecord(
                strategy_id=r[0],
                instrument_id=r[1],
                closed_ts_ms=int(r[2]),
                pnl_usdt=Decimal(str(r[3])),
                r_multiple=float(r[4]),
            )
            for r in rows
        ]

    def _fetch_trades_okx(
        self, strategy_id: str, since_ms: int | None,
    ) -> list[TradeRecord]:
        """Aggregate ``trades_okx`` (raw bills) into TradeRecord rows.

        Group by ``cl_ord_id`` so partial fills of the same order become one
        record. r_multiple is left at 0 (we don't have risk_usdt here; Kelly
        only needs win_rate/avg_R which can be computed from pnl alone).

        Strategy match is by ``strategy_id`` column matching either the bare
        config name (e.g. "ob_imbalance") or the NT-assigned id (e.g.
        "OBImbalanceStrategy-004"); reconcile_pnl_from_okx.py writes the
        bare name when clOrdId parse succeeds.
        """
        bare_name = strategy_id.split("Strategy")[0].lower()
        # Strategy name from yaml: "ob_imbalance" etc. Try both forms.
        candidates: list[str] = [strategy_id]
        # snake_case mapping
        mapping = {
            "obimbalance": "ob_imbalance",
            "fundingcarry": "funding_carry",
            "xsmomentum": "xs_momentum",
            "liqreversal": "liq_reversal",
            "basisarb": "basis_arb",
            "factorportfolio": "factor_portfolio",
            "fundingxs": "funding_cross_section",
            "fundingskew": "funding_skew_momentum",
            "statarb": "stat_arb_pairs",
            "optionvol": "option_vol_selling",
            "mlfusion": "ml_fusion",
        }
        if bare_name in mapping:
            candidates.append(mapping[bare_name])
        placeholders = ",".join("?" * len(candidates))
        params: list = list(candidates)
        ts_filter = ""
        if since_ms is not None:
            ts_filter = " AND ts_ms >= ?"
            params.append(int(since_ms))
        cur = self._conn.execute(
            f"SELECT strategy_id, inst_id, MAX(ts_ms) AS ts, "
            f"  SUM(bal_chg) AS bal, cl_ord_id "
            f"FROM trades_okx WHERE strategy_id IN ({placeholders}){ts_filter} "
            f"GROUP BY cl_ord_id ORDER BY ts ASC",
            params,
        )
        rows = cur.fetchall()
        return [
            TradeRecord(
                strategy_id=r[0],
                instrument_id=r[1] or "",
                closed_ts_ms=int(r[2]),
                pnl_usdt=Decimal(str(r[3] or 0)),
                r_multiple=0.0,
            )
            for r in rows
        ]

    def get_equities(
        self,
        strategy_id: str,
        *,
        since_ms: int | None = None,
    ) -> list[EquitySnapshot]:
        """按 strategy_id 拉净值快照，按 ``ts_ms`` 升序。"""
        with self._lock:
            if since_ms is None:
                cur = self._conn.execute(
                    "SELECT strategy_id, ts_ms, equity_usdt FROM equities "
                    "WHERE strategy_id = ? ORDER BY ts_ms ASC",
                    (strategy_id,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT strategy_id, ts_ms, equity_usdt FROM equities "
                    "WHERE strategy_id = ? AND ts_ms >= ? ORDER BY ts_ms ASC",
                    (strategy_id, int(since_ms)),
                )
            rows = cur.fetchall()
        return [
            EquitySnapshot(
                strategy_id=r[0],
                ts_ms=int(r[1]),
                equity_usdt=Decimal(str(r[2])),
            )
            for r in rows
        ]

    def list_strategies(self) -> list[str]:
        """返回所有曾出现过的 strategy_id（trades 或 equities 任一表里）。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT DISTINCT strategy_id FROM trades "
                "UNION SELECT DISTINCT strategy_id FROM equities"
            )
            return sorted(r[0] for r in cur.fetchall())

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """批量写入用的事务块（性能：减少 commit 次数）。"""
        with self._lock:
            try:
                yield
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def __enter__(self) -> PnLTracker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "EquitySnapshot",
    "PnLTracker",
    "TradeRecord",
]
