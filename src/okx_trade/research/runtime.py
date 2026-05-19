"""Async runtime helpers for the research CLI.

Separates the asyncio + REST-client wiring from the pure argparse layer in ``cli.py``.
All functions in this module are coroutines and assume an ``OKXRestClient`` can be
constructed from ambient ``OKXSettings`` (env vars / ``.env``).

Public surface used by ``cli.py``:
- ``parse_ymd_to_ms``
- ``parse_horizon_to_bars``
- ``resolve_universe``
- ``cmd_fetch``
- ``cmd_eval``
- ``cmd_grade_all``
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..enums import InstType
from .compute import compute_factor
from .data import fetch_panel
from .grade import FactorGrade, GradeThresholds, grade_factor
from .panel import FactorPanel
from .registry import get_factor, list_factors
from .report import render_grade_report
from .store import FactorStore, GradeRecord


_INCLUDE_FULL: tuple[str, ...] = (
    "close", "volume_usdt", "funding_rate", "open_interest", "basis_apr",
)


class _RestClient(Protocol):
    market: object
    public: object


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_ymd_to_ms(ymd: str) -> int:
    """``YYYY-MM-DD`` → UTC midnight ms timestamp."""
    dt = datetime.strptime(ymd, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_horizon_to_bars(horizon: str, *, bar_minutes: int = 60) -> int:
    """``1d`` / ``4h`` / ``1h`` / ``30m`` → bar count at the panel's bar interval.

    Defaults assume 1H bars (``bar_minutes=60``). Raises ValueError for unknown formats.
    """
    h = horizon.strip().lower()
    if h.endswith("d"):
        days = int(h[:-1])
        return max(1, days * 24 * 60 // bar_minutes)
    if h.endswith("h"):
        hours = int(h[:-1])
        return max(1, hours * 60 // bar_minutes)
    if h.endswith("m"):
        minutes = int(h[:-1])
        return max(1, minutes // bar_minutes)
    raise ValueError(f"unsupported horizon: {horizon!r}; use Nd / Nh / Nm")


# ---------------------------------------------------------------------------
# Universe resolution
# ---------------------------------------------------------------------------


async def resolve_universe(
    rest_client: _RestClient,
    spec: str,
    *,
    settle_ccy: str = "USDT",
) -> list[str]:
    """Resolve universe spec to a concrete list of OKX inst ids.

    Supported specs:
    - ``"topN"``: top N SWAP instruments by 24h volume, settled in ``settle_ccy``
    - comma-separated list: ``"BTC-USDT-SWAP,ETH-USDT-SWAP,..."``
    """
    spec = spec.strip()
    if spec.startswith("top") and spec[3:].isdigit():
        n = int(spec[3:])
        tickers = await rest_client.market.get_tickers(InstType.SWAP)
        suffix = f"-{settle_ccy}-SWAP"
        filtered = [t for t in tickers if t.inst_id.endswith(suffix)]
        filtered.sort(key=lambda t: float(t.vol_ccy_24h or 0), reverse=True)
        return [t.inst_id for t in filtered[:n]]
    if "," in spec:
        return [x.strip() for x in spec.split(",") if x.strip()]
    # Single inst id
    if "-" in spec:
        return [spec]
    raise ValueError(f"unknown universe spec: {spec!r}")


# ---------------------------------------------------------------------------
# Subcommand runtimes
# ---------------------------------------------------------------------------


async def cmd_fetch(
    *,
    rest_client: _RestClient,
    start: str,
    end: str,
    universe: str,
    bar: str,
    cache_dir: Path,
) -> FactorPanel:
    """Resolve universe + fetch panel (full include) + cache."""
    start_ms = parse_ymd_to_ms(start)
    end_ms = parse_ymd_to_ms(end)
    inst_ids = await resolve_universe(rest_client, universe)
    if not inst_ids:
        raise RuntimeError(f"universe {universe!r} resolved to 0 instruments")
    panel = await fetch_panel(
        rest_client=rest_client,
        inst_ids=inst_ids,
        start_ms=start_ms,
        end_ms=end_ms,
        bar=bar,
        include=_INCLUDE_FULL,
        cache_dir=cache_dir,
    )
    return panel


async def cmd_eval(
    *,
    rest_client: _RestClient,
    factor_id: str,
    start: str,
    end: str,
    universe: str,
    bar: str,
    horizon: str,
    top_k: int,
    panel_cache: Path,
    report_dir: Path,
    store: FactorStore,
    thresholds: GradeThresholds | None = None,
) -> tuple[FactorGrade, Path]:
    """Fetch (cache-hit) panel → grade single factor → save sqlite + markdown report.

    Returns (grade, report_path).
    """
    bar_minutes = _bar_to_minutes(bar)
    horizon_bars = parse_horizon_to_bars(horizon, bar_minutes=bar_minutes)
    panel = await cmd_fetch(
        rest_client=rest_client, start=start, end=end,
        universe=universe, bar=bar, cache_dir=panel_cache,
    )
    grade = grade_factor(
        factor_id, panel,
        horizon_bars=horizon_bars, top_k=top_k, thresholds=thresholds,
    )
    _persist_grade(store, grade)
    report_path = _write_report(report_dir, grade)
    return grade, report_path


async def cmd_grade_all(
    *,
    rest_client: _RestClient,
    start: str,
    end: str,
    universe: str,
    bar: str,
    horizon: str,
    top_k: int,
    panel_cache: Path,
    report_dir: Path,
    store: FactorStore,
    thresholds: GradeThresholds | None = None,
) -> list[tuple[FactorGrade, Path]]:
    """Grade every registered factor on the same fetched panel."""
    bar_minutes = _bar_to_minutes(bar)
    horizon_bars = parse_horizon_to_bars(horizon, bar_minutes=bar_minutes)
    panel = await cmd_fetch(
        rest_client=rest_client, start=start, end=end,
        universe=universe, bar=bar, cache_dir=panel_cache,
    )
    out: list[tuple[FactorGrade, Path]] = []
    for spec in list_factors():
        try:
            # compute_factor sanity-checks required_data; skip factors whose required
            # data is None on this panel (e.g. basis_apr if non-SWAP universe).
            compute_factor(spec.id, panel)
        except ValueError as exc:
            print(f"[skip] {spec.id}: {exc}")
            continue
        grade = grade_factor(
            spec.id, panel,
            horizon_bars=horizon_bars, top_k=top_k, thresholds=thresholds,
        )
        _persist_grade(store, grade)
        report_path = _write_report(report_dir, grade)
        out.append((grade, report_path))
        verdict_badge = "PASS" if grade.verdict == "pass" else "fail"
        print(
            f"  {spec.id:<28} IC={grade.ic_mean:+.4f} IR={grade.ir:+.3f} "
            f"t={grade.ic_t_stat:+.2f} net={grade.net_ls_spread_after_fees:+.4f} "
            f"→ {verdict_badge}"
        )
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bar_to_minutes(bar: str) -> int:
    """``1H`` → 60; ``5m`` → 5; ``1D`` → 1440."""
    s = bar.strip()
    if s.lower().endswith("h"):
        return int(s[:-1]) * 60
    if s.lower().endswith("d"):
        return int(s[:-1]) * 24 * 60
    if s.lower().endswith("m"):
        return int(s[:-1])
    raise ValueError(f"unsupported bar: {bar!r}")


def _persist_grade(store: FactorStore, grade: FactorGrade) -> None:
    """FactorGrade → GradeRecord (drops ic_decay list field) → sqlite insert."""
    rec = GradeRecord(
        factor_id=grade.factor_id,
        panel_start_ms=grade.panel_start_ms,
        panel_end_ms=grade.panel_end_ms,
        horizon_bars=grade.horizon_bars,
        ic_mean=grade.ic_mean, ic_std=grade.ic_std, ir=grade.ir,
        ic_t_stat=grade.ic_t_stat, ic_positive_rate=grade.ic_positive_rate,
        turnover_avg=grade.turnover_avg, autocorr_1=grade.autocorr_1,
        long_short_spread=grade.long_short_spread,
        net_ls_spread_after_fees=grade.net_ls_spread_after_fees,
        n_periods=grade.n_periods, n_instruments=grade.n_instruments,
        verdict=grade.verdict, graded_at_ms=grade.graded_at_ms,
    )
    store.save_grade(rec)


def _write_report(report_dir: Path, grade: FactorGrade) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    iso_date = datetime.fromtimestamp(
        grade.graded_at_ms / 1000, tz=timezone.utc,
    ).strftime("%Y-%m-%d")
    out = report_dir / f"{grade.factor_id}_{iso_date}.md"
    out.write_text(render_grade_report(grade))
    return out


__all__ = [
    "cmd_eval",
    "cmd_fetch",
    "cmd_grade_all",
    "parse_horizon_to_bars",
    "parse_ymd_to_ms",
    "resolve_universe",
]
