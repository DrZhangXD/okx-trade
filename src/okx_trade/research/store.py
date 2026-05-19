"""SQLite-backed factor zoo: metadata + grade history + approval state."""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, slots=True)
class GradeRecord:
    factor_id: str
    panel_start_ms: int
    panel_end_ms: int
    horizon_bars: int
    ic_mean: float
    ic_std: float
    ir: float
    ic_t_stat: float
    ic_positive_rate: float
    turnover_avg: float
    autocorr_1: float
    long_short_spread: float
    net_ls_spread_after_fees: float
    n_periods: int
    n_instruments: int
    verdict: str  # "pass" | "fail"
    graded_at_ms: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS factors (
  id TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  direction TEXT NOT NULL,
  description TEXT,
  approved INTEGER NOT NULL DEFAULT 0,
  approved_weight REAL,
  approved_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS grade_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  factor_id TEXT NOT NULL REFERENCES factors(id),
  panel_start_ms INTEGER NOT NULL,
  panel_end_ms INTEGER NOT NULL,
  horizon_bars INTEGER NOT NULL,
  ic_mean REAL, ic_std REAL, ir REAL, ic_t_stat REAL, ic_positive_rate REAL,
  turnover_avg REAL, autocorr_1 REAL,
  long_short_spread REAL, net_ls_spread_after_fees REAL,
  n_periods INTEGER, n_instruments INTEGER,
  verdict TEXT NOT NULL,
  graded_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grade_runs_factor
  ON grade_runs(factor_id, graded_at_ms DESC);
"""


class FactorStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def upsert_factor(
        self, *, id: str, category: str, direction: str, description: str,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO factors(id, category, direction, description) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "category=excluded.category, direction=excluded.direction, "
                "description=excluded.description",
                (id, category, direction, description),
            )

    def list_factors(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, category, direction, description, approved, "
                "approved_weight, approved_at_ms FROM factors ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_approved(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, category, direction, approved_weight, approved_at_ms "
                "FROM factors WHERE approved=1 ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def save_grade(self, rec: GradeRecord) -> None:
        d = asdict(rec)
        cols = ", ".join(d.keys())
        placeholders = ", ".join("?" for _ in d)
        with self._conn() as c:
            c.execute(
                f"INSERT INTO grade_runs({cols}) VALUES({placeholders})",
                tuple(d.values()),
            )

    def grade_history(self, factor_id: str, *, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM grade_runs WHERE factor_id=? "
                "ORDER BY graded_at_ms DESC LIMIT ?",
                (factor_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_grade(self, factor_id: str) -> dict | None:
        rows = self.grade_history(factor_id, limit=1)
        return rows[0] if rows else None

    def approve(self, factor_id: str, *, weight: float, ts_ms: int) -> None:
        with self._conn() as c:
            updated = c.execute(
                "UPDATE factors SET approved=1, approved_weight=?, approved_at_ms=? "
                "WHERE id=?",
                (weight, ts_ms, factor_id),
            ).rowcount
        if updated == 0:
            raise KeyError(f"factor {factor_id!r} not in store; upsert first")

    def reject(self, factor_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE factors SET approved=0, approved_weight=NULL, approved_at_ms=NULL "
                "WHERE id=?",
                (factor_id,),
            )


__all__ = ["FactorStore", "GradeRecord"]
