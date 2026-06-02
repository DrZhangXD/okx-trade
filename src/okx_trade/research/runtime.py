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
- ``cmd_backtest_portfolio``
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..enums import InstType
from .compute import compute_factor
from .data import _cache_key, fetch_panel
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


async def cmd_wf_grade(
    *,
    rest_client: _RestClient,
    factor_id: str,
    start: str,
    end: str,
    universe: str,
    bar: str,
    horizon: str,
    train_days: int,
    test_days: int,
    panel_cache: Path,
) -> list:
    """Walk-forward OOS grade: roll a (train, test) split + grade each test slice.

    Returns list of FactorGrade. Prints a one-row-per-window summary table.
    """
    from .walk_forward_grade import walk_forward_grade

    panel = await cmd_fetch(
        rest_client=rest_client, start=start, end=end,
        universe=universe, bar=bar, cache_dir=panel_cache,
    )
    bar_minutes = _bar_to_minutes(bar)
    horizon_bars = parse_horizon_to_bars(horizon, bar_minutes=bar_minutes)
    train_window = train_days * 24 * 60 // bar_minutes
    test_window = test_days * 24 * 60 // bar_minutes

    grades = walk_forward_grade(
        factor_id, panel,
        horizon_bars=horizon_bars,
        train_window=train_window, test_window=test_window,
    )
    if not grades:
        print(f"  {factor_id}: 0 windows (panel too short for train={train_days}d + test={test_days}d)")
        return grades

    print(f"\nWalk-forward OOS for {factor_id} ({train_days}d train + {test_days}d test):")
    ics = [g.ic_mean for g in grades]
    sign = 1 if sum(ics) >= 0 else -1
    wins = sum(1 for x in ics if (x >= 0) == (sign >= 0))
    ic_str = "  ".join(f"{x:+.3f}" for x in ics)
    print(f"  windows: {len(grades)}   same-direction: {wins}/{len(grades)} ({wins/len(grades):.0%})")
    print(f"  IC per window: [{ic_str}]")
    return grades


async def cmd_corr_matrix(
    *,
    rest_client: _RestClient,
    factor_ids: list[str],
    start: str,
    end: str,
    universe: str,
    bar: str,
    tail_days: int,
    panel_cache: Path,
) -> dict:
    """Pairwise Pearson correlation of factor scores over the last ``tail_days``.

    Returns dict with ``{factor_ids, matrix}``. Prints a pretty table.
    """
    import numpy as np

    panel = await cmd_fetch(
        rest_client=rest_client, start=start, end=end,
        universe=universe, bar=bar, cache_dir=panel_cache,
    )
    bar_minutes = _bar_to_minutes(bar)
    tail_bars = tail_days * 24 * 60 // bar_minutes

    vals: dict[str, "np.ndarray"] = {}
    for fid in factor_ids:
        try:
            arr = compute_factor(fid, panel)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {fid}: {exc}")
            continue
        vals[fid] = arr[-tail_bars:].flatten()
    valid = list(vals.keys())
    n = len(valid)
    matrix = np.full((n, n), np.nan)
    for i, a in enumerate(valid):
        for j, b in enumerate(valid):
            x, y = vals[a], vals[b]
            m = ~(np.isnan(x) | np.isnan(y))
            if m.sum() >= 50:
                matrix[i, j] = float(np.corrcoef(x[m], y[m])[0, 1])

    print(f"\nFactor correlation matrix (last {tail_days}d × {panel.n} inst):\n")
    print(f"{'':<22}" + "".join(f"{v[:10]:>11}" for v in valid))
    for i, a in enumerate(valid):
        cells = []
        for j in range(n):
            c = matrix[i, j]
            cells.append(f"{c:+.3f}".rjust(11) if not np.isnan(c) else "       nan ")
        print(f"{a:<22}" + "".join(cells))
    print("\nPairs with |corr| > 0.5 (potential duplicate alpha):")
    flagged = 0
    for i in range(n):
        for j in range(i + 1, n):
            c = matrix[i, j]
            if not np.isnan(c) and abs(c) > 0.5:
                print(f"  {valid[i]:<24} <-> {valid[j]:<24} corr={c:+.3f}")
                flagged += 1
    if flagged == 0:
        print("  (none)")
    return {"factor_ids": valid, "matrix": matrix}


async def cmd_backtest_portfolio(
    *,
    rest_client: _RestClient,
    yaml_cfg: dict,
    bar: str,
    total_bars: int,
    catalog_path: Path,
    taker_fee_bps: float = 5.0,
    maker_fee_bps: float = 2.0,
    warmup_days: int = 0,
    warmup_panel_dir: Path | None = None,
    reuse_data: bool = False,
) -> dict:
    """Run FactorPortfolioStrategy backtest on freshly-downloaded bars.

    Reads instruments + factor weights from ``yaml_cfg`` (a parsed factor_portfolio.yaml).
    Downloads bars for each instrument, builds NT BacktestDataConfig, runs the strategy.
    Returns a summary dict (pnl, sharpe, max_drawdown, total_orders, etc.).

    ``warmup_days``: if > 0, fetches an additional panel covering ``warmup_days``
    before the backtest window and saves to ``warmup_panel_dir/warmup_panel.parquet``.
    The strategy will load it in on_start to pre-populate buffers, so rolling-window
    factors (basis_z_30d, funding_z_30d need 30d) are active from bar 1 instead
    of after their warmup period elapses.
    """
    import time as _time

    from okx_trade.adapter.constants import OKX_VENUE
    from okx_trade.adapter.parsing import make_bar_type
    from okx_trade.backtest import (
        build_okx_venue_config,
        extract_equity_curve,
        run_backtest_with_node,
    )
    from .data import _save_cache, fetch_panel
    from okx_trade.backtest.data_loader import prepare_backtest_catalog

    inst_ids_with_venue: list[str] = list(yaml_cfg.get("instrument_ids", []))
    if not inst_ids_with_venue:
        raise RuntimeError(
            "factor_portfolio yaml has empty instrument_ids; cannot run backtest"
        )
    # Strip ".OKX" suffix to get bare OKX inst id for REST calls
    bare_ids = [s.split(".")[0] for s in inst_ids_with_venue]

    # Derive spot pairs (e.g., BTC-USDT for BTC-USDT-SWAP) so the strategy can
    # compute basis_apr at backtest time. Mirrors _derive_spot_inst_id but on
    # bare ids (no .OKX suffix here).
    spot_ids: list[str] = []
    for bid in bare_ids:
        if bid.endswith("-SWAP"):
            spot_ids.append(bid[: -len("-SWAP")])

    if reuse_data:
        # 复用已有 catalog 的 bars(上次下载留下的)。跳过全部网络下载 →
        # 同窗 A/B 第二轮可瞬时重跑,且避开"窗口漂移→catalog 非disjoint 写冲突"。
        print(
            f"[1/3] reusing catalog bars (--reuse-data) for "
            f"{len(bare_ids)} perps + {len(spot_ids)} spot pairs"
        )
    else:
        print(
            f"[1/3] downloading {total_bars} × {bar} bars for "
            f"{len(bare_ids)} perps + {len(spot_ids)} spot pairs"
        )
        for inst_id in bare_ids + spot_ids:
            try:
                _, bars = await prepare_backtest_catalog(
                    rest_client, inst_id, bar,  # type: ignore[arg-type]
                    total=total_bars, catalog_path=catalog_path,
                    taker_fee_bps=taker_fee_bps, maker_fee_bps=maker_fee_bps,
                )
                print(f"        {inst_id}: {len(bars)} bars")
            except Exception as exc:  # noqa: BLE001
                # Spot pair may not exist for some perps — log + continue (strategy
                # will get None basis_apr for that inst, which it tolerates).
                if inst_id in spot_ids:
                    print(f"        {inst_id}: SKIP ({exc})")
                    continue
                print(f"        {inst_id}: FAILED ({exc})")
                raise

    print("[2/3] building backtest config")
    from nautilus_trader.backtest.config import BacktestDataConfig
    from nautilus_trader.config import ImportableStrategyConfig
    from nautilus_trader.model.data import Bar

    # Build BacktestDataConfig for perp bars (drive rebalance) and spot bars
    # (drive basis_apr). Both go into the same catalog.
    all_inst_ids = inst_ids_with_venue + [
        f"{sid}.{OKX_VENUE}" for sid in spot_ids
    ]
    all_bare_ids = bare_ids + spot_ids
    bar_types = [make_bar_type(s, bar) for s in all_bare_ids]
    data_configs = [
        BacktestDataConfig(
            catalog_path=str(catalog_path.resolve()),
            data_cls=Bar.fully_qualified_name(),
            instrument_id=instrument_id,
            bar_types=[str(bar_type)],
        )
        for instrument_id, bar_type in zip(all_inst_ids, bar_types)
    ]

    venue = build_okx_venue_config(
        starting_balance_usdt=float(yaml_cfg.get("account_equity_usdt", 10_000.0)),
        leverage=10,
        enable_fees=True,
    )

    # Optional pre-fetch a warmup panel + save to disk, so the strategy can
    # load it in on_start and start with hot rolling-window buffers.
    warmup_panel_cache_path: str | None = None
    if warmup_days > 0:
        out_dir = warmup_panel_dir or catalog_path.parent
        warmup_path = out_dir / "warmup_panel.parquet"
        if reuse_data and warmup_path.exists():
            # 复用上次写下的 warmup panel,跳过网络拉取。
            warmup_panel_cache_path = str(warmup_path)
            print(f"[1.5/3] reusing warmup panel → {warmup_path}")
        else:
            bar_ms = _bar_to_minutes(bar) * 60_000
            bt_start_ms = int(_time.time() * 1000) - (total_bars * bar_ms)
            warmup_start_ms = bt_start_ms - (warmup_days * 24 * 3_600_000)
            warmup_end_ms = bt_start_ms - 1
            print(
                f"[1.5/3] fetching warmup panel ({warmup_days} days × {len(bare_ids)} perps)"
            )
            warmup_panel = await fetch_panel(
                rest_client=rest_client,
                inst_ids=bare_ids,
                start_ms=warmup_start_ms,
                end_ms=warmup_end_ms,
                bar=bar,
                include=("close", "volume_usdt", "funding_rate", "open_interest", "basis_apr"),
                cache_dir=warmup_panel_dir,
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            _save_cache(warmup_panel, warmup_path)
            warmup_panel_cache_path = str(warmup_path)
            print(f"        wrote warmup parquet → {warmup_path} (T={warmup_panel.t})")

    strategy_config = ImportableStrategyConfig(
        strategy_path="okx_trade.strategies.factor_portfolio:FactorPortfolioStrategy",
        config_path="okx_trade.strategies.factor_portfolio:FactorPortfolioConfig",
        config={
            "instrument_ids": list(inst_ids_with_venue),
            "bar_type_template": yaml_cfg.get(
                "bar_type_template", "{inst}-1-HOUR-LAST-EXTERNAL",
            ),
            "rebalance_hours": int(yaml_cfg.get("rebalance_hours", 4)),
            "top_k_long": int(yaml_cfg.get("top_k_long", 5)),
            "top_k_short": int(yaml_cfg.get("top_k_short", 5)),
            "risk_pct": float(yaml_cfg.get("risk_pct", 0.002)),
            "account_equity_usdt": float(yaml_cfg.get("account_equity_usdt", 10_000.0)),
            "factor_weights": [
                tuple(pair) for pair in yaml_cfg.get("factor_weights", [])
            ],
            "subscribe_spot_for_basis": bool(
                yaml_cfg.get("subscribe_spot_for_basis", True)
            ),
            # Backtest's simulated clock doesn't match real OKX time, so REST
            # polling for funding/OI would buffer "now" values that don't align
            # with the simulated bar timestamps. Force off; factors that need
            # funding/OI will return NaN in backtest UNLESS a warmup panel
            # (--warmup-days) provided pre-populated funding/OI buffers.
            "enable_rest_polling": False,
            "warmup_panel_cache_path": warmup_panel_cache_path,
        },
    )

    print(f"[3/3] running backtest (factors: "
          f"{len(yaml_cfg.get('factor_weights', []))})")
    summary, node = run_backtest_with_node(
        venue=venue, data=data_configs, strategies=[strategy_config],
    )

    # Dual reporting strategy:
    #
    #  nt_* metrics: NT's BacktestSummary, derived from REALIZED trade PnL only.
    #    Conservative — excludes mark-to-market on open positions at backtest end.
    #    Often shows maxDD=0 because realized PnL on a trend-rotating strategy
    #    never dips below 0 if each closed trade is positive.
    #
    #  equity_* metrics (pnl_pct, sharpe_ratio, max_drawdown_pct): derived from
    #    the account equity curve (free + locked USDT including unrealized PnL on
    #    open positions). This is what a portfolio manager sees on their screen.
    #    Will differ from nt_* if the strategy ends with open positions whose
    #    unrealized PnL is significant; for FactorPortfolioStrategy this is the
    #    usual case (it always holds top_k_long + top_k_short legs between rebals).
    #
    # Both are reported; user should look at equity_* for "what would I actually
    # have" and nt_* for "what's locked in." Production paper trading on the VPS
    # will surface the true live equity via the existing daily_report system.
    equity_metrics = _compute_equity_metrics(node, bar)

    return {
        # NT-native (may differ from equity-based; kept for cross-check)
        "nt_pnl_pct": getattr(summary, "pnl_pct", None),
        "nt_sharpe_ratio": getattr(summary, "sharpe_ratio", None),
        "nt_max_drawdown_pct": getattr(summary, "max_drawdown_pct", None),
        "nt_win_rate": getattr(summary, "win_rate", None),
        "total_orders": getattr(summary, "total_orders", None),
        # First-principles (equity curve based)
        **equity_metrics,
        "_raw": summary,
    }


def _compute_equity_metrics(node, bar: str) -> dict:
    """Compute portfolio-level metrics from the equity curve.

    Sharpe is annualized using sqrt(periods_per_year) where periods_per_year is
    derived from the bar interval. maxDD is peak-to-trough on the equity series.
    """
    import numpy as np

    from okx_trade.adapter.constants import OKX_VENUE
    from okx_trade.backtest import extract_equity_curve

    try:
        eq = extract_equity_curve(node, OKX_VENUE)
    except Exception as exc:  # noqa: BLE001
        return {
            "equity_error": str(exc),
            "pnl_pct": None,
            "sharpe_ratio": None,
            "max_drawdown_pct": None,
            "annualized_return_pct": None,
            "n_equity_samples": 0,
        }
    if eq.empty or "total" not in eq.columns:
        return {
            "pnl_pct": None,
            "sharpe_ratio": None,
            "max_drawdown_pct": None,
            "annualized_return_pct": None,
            "n_equity_samples": 0,
        }
    total = eq["total"].astype(float)
    if len(total) < 2:
        return {
            "pnl_pct": 0.0,
            "sharpe_ratio": None,
            "max_drawdown_pct": 0.0,
            "annualized_return_pct": None,
            "n_equity_samples": int(len(total)),
        }
    pnl_pct = float(total.iloc[-1] / total.iloc[0] - 1.0) * 100.0
    drawdown = (total / total.cummax() - 1.0).min()
    max_dd_pct = float(drawdown) * 100.0
    # Approximate periods/year from bar (1H → 24*365 = 8760)
    bar_mins = _bar_to_minutes(bar)
    periods_per_year = (365 * 24 * 60) / max(1, bar_mins)
    # Resample equity to bar frequency to get per-period returns
    rets = total.pct_change().dropna()
    if len(rets) < 2 or rets.std() == 0:
        sharpe = None
    else:
        sharpe = float(rets.mean() / rets.std() * np.sqrt(periods_per_year))
    # Annualized return from total pnl + actual elapsed time
    elapsed_seconds = (total.index[-1] - total.index[0]).total_seconds()
    if elapsed_seconds > 0:
        years = elapsed_seconds / (365 * 24 * 3600)
        ann_ret_pct = float(((total.iloc[-1] / total.iloc[0]) ** (1.0 / years) - 1.0)) * 100.0 if years > 0 else None
    else:
        ann_ret_pct = None
    return {
        "pnl_pct": pnl_pct,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": max_dd_pct,
        "annualized_return_pct": ann_ret_pct,
        "n_equity_samples": int(len(total)),
    }


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


async def ensure_panel_cached(
    *,
    rest_client,
    inst_ids: list[str],
    bar: str,
    start_ms: int,
    end_ms: int,
    include: tuple[str, ...] = ("close", "volume_usdt", "funding_rate", "open_interest"),
    cache_dir: Path,
) -> Path:
    """Return path to cached panel parquet; fetch + write if missing.

    Cache hit (file exists with size > 0) short-circuits — no REST call.
    Cache miss invokes ``fetch_panel`` which writes the parquet, then verifies
    the file is present and raises ``RuntimeError`` if not.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(list(inst_ids), bar, start_ms, end_ms, tuple(include))
    cache_path = cache_dir / f"panel_{key}.parquet"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path
    await fetch_panel(
        rest_client=rest_client,
        inst_ids=list(inst_ids),
        start_ms=start_ms,
        end_ms=end_ms,
        bar=bar,
        include=tuple(include),
        cache_dir=cache_dir,
    )
    if not cache_path.exists():
        raise RuntimeError(f"fetch_panel did not produce expected cache at {cache_path}")
    return cache_path


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
    "cmd_backtest_portfolio",
    "cmd_corr_matrix",
    "cmd_eval",
    "cmd_fetch",
    "cmd_grade_all",
    "cmd_wf_grade",
    "ensure_panel_cached",
    "parse_horizon_to_bars",
    "parse_ymd_to_ms",
    "resolve_universe",
]
