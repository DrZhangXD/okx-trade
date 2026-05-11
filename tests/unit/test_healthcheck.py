"""scripts/healthcheck.py 单测：未来时间戳过滤回归。

回归 bug：XSMomentum 曾把 1D bar 边界（HKT close = UTC 16:00）当作 equity ts_ms
写入，导致 ``MAX(ts_ms)`` 永远在未来，``age = (now - max) / 60_000`` 恒负，
``--max-stale-mins 30`` 阈值永远 ``> -637``，PNL_STALE 检查实际从未触发。
修复后 SQL 加了 ``WHERE ts <= now_ms`` 过滤未来行；这里覆盖三种边界。
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEALTHCHECK_PATH = _REPO_ROOT / "scripts" / "healthcheck.py"


def _load_healthcheck() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hc_mod", _HEALTHCHECK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hc_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


hc = _load_healthcheck()


def _make_pnl_db(tmp_path: Path) -> Path:
    """建一个最小 pnl.sqlite，schema 与生产一致。"""
    db = tmp_path / "pnl.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE trades (
            strategy_id    TEXT NOT NULL,
            instrument_id  TEXT NOT NULL,
            closed_ts_ms   INTEGER NOT NULL,
            pnl_usdt       REAL NOT NULL,
            r_multiple     REAL NOT NULL
        );
        CREATE TABLE equities (
            strategy_id    TEXT NOT NULL,
            ts_ms          INTEGER NOT NULL,
            equity_usdt    REAL NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def test_pnl_age_ignores_future_equity(tmp_path: Path) -> None:
    """未来时间戳的 equity 行不应污染 MAX；真实 trade 决定 age。"""
    db = _make_pnl_db(tmp_path)
    now_ms = int(time.time() * 1000)
    five_min_ago = now_ms - 5 * 60_000
    ten_hours_in_future = now_ms + 10 * 3600_000

    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trades VALUES (?, ?, ?, ?, ?)",
        ("s1", "BTC-USDT-SWAP", five_min_ago, 1.5, 0.3),
    )
    conn.execute(
        "INSERT INTO equities VALUES (?, ?, ?)",
        ("s1", ten_hours_in_future, 5000.0),
    )
    conn.commit()
    conn.close()

    age = hc.pnl_age_minutes(db)
    assert age is not None
    assert age == pytest.approx(5.0, abs=0.5)  # 来自 trade，不是未来 equity


def test_pnl_age_returns_none_when_only_future_rows(tmp_path: Path) -> None:
    """只有 future-dated 行 → 视为没有有效数据 → None（main 走 cold-start WARN 分支）。"""
    db = _make_pnl_db(tmp_path)
    future = int(time.time() * 1000) + 3600_000

    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO equities VALUES (?, ?, ?)",
        ("s1", future, 5000.0),
    )
    conn.commit()
    conn.close()

    assert hc.pnl_age_minutes(db) is None


def test_pnl_age_uses_max_real_ts(tmp_path: Path) -> None:
    """trade=10min 前、equity=2min 前 → 取较新的 equity → ≈2min。"""
    db = _make_pnl_db(tmp_path)
    now_ms = int(time.time() * 1000)

    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trades VALUES (?, ?, ?, ?, ?)",
        ("s1", "BTC-USDT-SWAP", now_ms - 10 * 60_000, 1.0, 0.2),
    )
    conn.execute(
        "INSERT INTO equities VALUES (?, ?, ?)",
        ("s1", now_ms - 2 * 60_000, 5000.0),
    )
    conn.commit()
    conn.close()

    age = hc.pnl_age_minutes(db)
    assert age is not None
    assert age == pytest.approx(2.0, abs=0.5)


def test_pnl_age_returns_none_when_db_missing(tmp_path: Path) -> None:
    assert hc.pnl_age_minutes(tmp_path / "nope.sqlite") is None
