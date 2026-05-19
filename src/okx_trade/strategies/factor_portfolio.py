"""FactorPortfolioStrategy — generic factor synthesizer (linear weighted z-score).

Pure-function layer (NT-independent, used by tests + the NT Strategy class below).
The NT Strategy class is implemented in a follow-up section that imports NT lazily.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from ..research.compute import compute_factor
from ..research.panel import FactorPanel
from ..research.registry import get_factor

if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar


@dataclass(frozen=True, slots=True)
class FactorWeight:
    id: str
    weight: float


def cross_section_zscore(vals: np.ndarray) -> np.ndarray:
    """Per-row z-score across instruments. NaN-safe. Returns all-NaN if std=0."""
    mu = np.nanmean(vals)
    sd = np.nanstd(vals)
    if not np.isfinite(sd) or sd == 0:
        return np.full_like(vals, np.nan, dtype=float)
    return (vals - mu) / sd


def synthesize_score(
    panel: FactorPanel,
    weights: list[FactorWeight],
) -> tuple[np.ndarray, list[str]]:
    """Compute each factor's last-row values, z-score, weight, and sum.

    Direction handling: ``long_low`` factors are negated so high score → long.

    Returns:
        (score, missing_ids): score is shape (panel.n,); missing_ids lists weight
        entries whose factor id isn't registered (skipped gracefully).
    """
    accumulated = np.zeros(panel.n, dtype=float)
    used_weight = 0.0
    missing: list[str] = []
    for w in weights:
        try:
            spec = get_factor(w.id)
        except KeyError:
            missing.append(w.id)
            continue
        try:
            arr = compute_factor(w.id, panel)
        except ValueError:
            missing.append(w.id)
            continue
        last = arr[-1].astype(float)
        if spec.direction == "long_low":
            last = -last
        z = cross_section_zscore(last)
        # If a row is all-NaN, skip it (don't pollute accumulated)
        if np.all(np.isnan(z)):
            missing.append(w.id)
            continue
        # NaN entries pass through as 0 contribution; finite entries contribute
        contrib = np.where(np.isnan(z), 0.0, z * w.weight)
        accumulated = accumulated + contrib
        used_weight += w.weight
    if used_weight == 0:
        return np.full(panel.n, np.nan, dtype=float), missing
    return accumulated, missing


def select_top_bot(
    score: np.ndarray, *, top_k_long: int, top_k_short: int,
) -> tuple[list[int], list[int]]:
    """Pick top-K (long) and bot-K (short) indices, skipping NaN scores.

    Returns indices into the panel's ``inst_ids`` array. Longs sorted descending,
    shorts sorted ascending (most-negative first).
    """
    valid = np.where(np.isfinite(score))[0]
    if len(valid) == 0:
        return [], []
    order = valid[np.argsort(score[valid])]  # ascending
    longs = order[-top_k_long:][::-1].tolist() if top_k_long > 0 else []
    shorts = order[:top_k_short].tolist() if top_k_short > 0 else []
    return longs, shorts


# ---------------------------------------------------------------------------
# NT Strategy (lazy-loaded — only available if nautilus_trader is installed)
# ---------------------------------------------------------------------------

try:
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.trading.config import StrategyConfig
    from nautilus_trader.trading.strategy import Strategy

    _NT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NT_AVAILABLE = False
    StrategyConfig = object  # type: ignore[assignment,misc]
    Strategy = object        # type: ignore[assignment,misc]


def _ffill_to_axis(
    by_inst,  # dict[str, deque[tuple[int, float]]]
    ts_axis,  # tuple[int, ...]
    inst_ids,  # tuple[str, ...]
):
    """Forward-fill per-inst (ts, val) deques onto a shared ts axis.

    Returns shape ``(T, N)`` np.ndarray with NaN where no value was observed
    at-or-before the corresponding ts for that inst. Used to align REST-polled
    funding / OI data (irregular cadence) onto the regular bar timestamp axis.
    """
    T, N = len(ts_axis), len(inst_ids)
    arr = np.full((T, N), np.nan, dtype=float)
    for col, inst in enumerate(inst_ids):
        sparse = sorted(by_inst.get(inst, []))
        if not sparse:
            continue
        j = 0
        last_val: float | None = None
        for row, ts in enumerate(ts_axis):
            while j < len(sparse) and sparse[j][0] <= ts:
                last_val = sparse[j][1]
                j += 1
            if last_val is not None:
                arr[row, col] = last_val
    return arr


def _derive_spot_inst_id(perp_inst_id: str) -> str | None:
    """Derive the SPOT inst id from a SWAP one, preserving any venue suffix.

    Examples:
        "BTC-USDT-SWAP.OKX" -> "BTC-USDT.OKX"
        "ETH-USDT-SWAP"     -> "ETH-USDT"
        "BTC-USDT"          -> None   (already spot)
        "BTC-USD-260925"    -> None   (delivery future, no spot equivalent)
    """
    head, _, venue = perp_inst_id.partition(".")
    if not head.endswith("-SWAP"):
        return None
    spot_head = head[: -len("-SWAP")]
    return f"{spot_head}.{venue}" if venue else spot_head


if _NT_AVAILABLE:

    from collections import deque

    from ..research import factors as _trigger_factor_registration  # noqa: F401
    from ..risk import RiskConfig, RiskIntent, apply_risk_manager, build_risk_manager
    from .base import effective_equity_usdt, position_contracts
    from .qty import safe_make_qty


    class FactorPortfolioConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """Configuration mirror of ``configs/factor_portfolio.yaml``.

        ``factor_weights`` is a list of (id, weight) tuples (StrategyConfig requires
        hashable/frozen types — list-of-tuples avoids dict mutation while staying
        round-trippable to/from yaml).

        ``subscribe_spot_for_basis`` (default True) makes the strategy also subscribe
        to the SPOT pair of each perp (BTC-USDT for BTC-USDT-SWAP) so the
        ``basis_apr`` panel field can be populated from (perp_close - spot_close).
        Required by ``basis_apr`` / ``basis_z_30d`` factors. Disable to fall back
        to close-only factors (e.g. momentum_*_reversal).

        ``enable_rest_polling`` (default True in live) spawns an asyncio background
        task that polls ``GET /api/v5/public/funding-rate`` and
        ``/api/v5/public/open-interest`` for each perp every
        ``rest_poll_interval_sec`` (default 300). The values are buffered and
        forward-filled into the ``funding_rate`` and ``open_interest`` panel
        columns at rebalance time. Required by ``funding_*`` / ``oi_*`` factors.
        Backtest CLI sets this False because the BacktestEngine's simulated clock
        doesn't match real OKX time.
        """
        instrument_ids: list[str]
        bar_type_template: str
        rebalance_hours: int = 4
        top_k_long: int = 5
        top_k_short: int = 5
        risk_pct: float = 0.002
        account_equity_usdt: float = 10_000.0
        factor_weights: list[tuple[str, float]] = []
        subscribe_spot_for_basis: bool = True
        enable_rest_polling: bool = True
        rest_poll_interval_sec: int = 300
        # If set, on_start reads a fetch_panel parquet from this path and pre-populates
        # all per-inst buffers (close/volume/funding/OI/derived-spot-close). This warms
        # the rolling-window factors (basis_z_30d, funding_z_30d need 30d) so they're
        # active from bar 1 instead of bar 720. Used by backtest CLI --warmup-days N.
        warmup_panel_cache_path: str | None = None
        risk_config: RiskConfig | None = None


    class FactorPortfolioStrategy(Strategy):  # type: ignore[misc]
        """Generic factor portfolio: read approved factors -> synthesize -> top-K trade.

        Compatible with the existing risk / pnl / portfolio infrastructure (matches
        the patterns in xs_momentum + ml_fusion).
        """

        def __init__(self, config: FactorPortfolioConfig) -> None:
            super().__init__(config)
            self._inst_ids = [InstrumentId.from_str(s) for s in config.instrument_ids]
            self._bar_types = {
                iid: BarType.from_str(config.bar_type_template.format(inst=iid.value))
                for iid in self._inst_ids
            }
            # Buffer enough bars for the slowest factor (vol_of_vol_30d = 60d * 24 + slack).
            # Switched from deque[float] to deque[tuple[ts_ms, value]] so we can align
            # perp and spot bars by timestamp when computing basis_apr.
            _buf_len = 60 * 24 + 50
            self._closes: dict[str, deque[tuple[int, float]]] = {
                iid.value: deque(maxlen=_buf_len) for iid in self._inst_ids
            }
            self._volumes: dict[str, deque[tuple[int, float]]] = {
                iid.value: deque(maxlen=_buf_len) for iid in self._inst_ids
            }
            # Spot pair tracking for basis_apr. _spot_to_perp lets us route an
            # incoming spot bar back to its perp inst's buffer.
            self._spot_closes: dict[str, deque[tuple[int, float]]] = {
                iid.value: deque(maxlen=_buf_len) for iid in self._inst_ids
            }
            self._spot_to_perp: dict[str, str] = {}
            self._spot_bar_types: dict[str, BarType] = {}
            if config.subscribe_spot_for_basis:
                for iid in self._inst_ids:
                    perp_value = iid.value  # e.g. "BTC-USDT-SWAP.OKX"
                    spot_value = _derive_spot_inst_id(perp_value)
                    if spot_value is not None:
                        self._spot_to_perp[spot_value] = perp_value
                        spot_bar_str = config.bar_type_template.format(inst=spot_value)
                        self._spot_bar_types[spot_value] = BarType.from_str(spot_bar_str)
            # Funding / OI buffers — populated by a background REST polling task
            # in live mode (enable_rest_polling=True). Stay empty in backtest.
            self._funding_rates: dict[str, deque[tuple[int, float]]] = {
                iid.value: deque(maxlen=_buf_len) for iid in self._inst_ids
            }
            self._open_interests: dict[str, deque[tuple[int, float]]] = {
                iid.value: deque(maxlen=_buf_len) for iid in self._inst_ids
            }
            self._rest_poll_task: Any = None
            self._last_rebalance_ms: int = 0
            self._positions: dict[str, tuple[str, float, int]] = {}
            self._allocated_equity_usdt: float | None = None
            self._weights = [FactorWeight(id=fid, weight=w)
                             for fid, w in config.factor_weights]
            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._pnl_tracker = None  # type: ignore[var-annotated]

        def on_start(self) -> None:
            # Pre-populate buffers from a fetched panel cache (backtest --warmup-days).
            # Must happen before subscribe_bars so the first incoming bar appends to
            # already-warm history rather than starting from scratch.
            if self.config.warmup_panel_cache_path:
                self._load_warmup_panel(self.config.warmup_panel_cache_path)
            for bar_type in self._bar_types.values():
                self.subscribe_bars(bar_type)
            for spot_bar_type in self._spot_bar_types.values():
                try:
                    self.subscribe_bars(spot_bar_type)
                except Exception as exc:  # noqa: BLE001
                    # Spot instrument may not be loaded in the cache (e.g. live
                    # mode where instrument_provider only loaded SWAP). Log + skip:
                    # the perp's spot_closes buffer stays empty → basis_apr factors
                    # produce NaN → synthesize_score skips them gracefully.
                    self.log.warning(
                        f"factor_portfolio: spot subscribe failed for {spot_bar_type}: {exc}"
                    )
            # Spawn REST polling task for funding + OI. Asyncio task lives on the
            # NT engine loop; cancelled in on_stop. Backtest sets enable_rest_polling
            # False because simulated clock != real OKX time.
            if self.config.enable_rest_polling:
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    self._rest_poll_task = loop.create_task(self._poll_rest_loop())
                except RuntimeError as exc:
                    self.log.warning(
                        f"factor_portfolio: cannot spawn rest_poll_task "
                        f"(no event loop): {exc}"
                    )
            self.log.info(
                f"factor_portfolio start: factors={[w.id for w in self._weights]} "
                f"top_k={self.config.top_k_long}/{self.config.top_k_short} "
                f"spot_pairs={len(self._spot_to_perp)} "
                f"rest_poll={'on' if self._rest_poll_task else 'off'}"
            )
            if not self._weights:
                self.log.warning("factor_portfolio: no factor_weights configured; idle")

        def on_stop(self) -> None:
            if self._rest_poll_task is not None and not self._rest_poll_task.done():
                self._rest_poll_task.cancel()
                self._rest_poll_task = None
            self.log.info(f"factor_portfolio stop; open_legs={len(self._positions)}")

        def _load_warmup_panel(self, path: str) -> None:
            """Pre-populate buffers from a fetch_panel parquet cache.

            Reconstructs spot_close from basis_apr algebraically:
              basis = (perp - spot) / spot  →  spot = perp / (1 + basis)
            so we don't need spot_close as a separate column in the cache file.
            NaN values are skipped per (ts, inst) cell so partial coverage is OK.
            """
            from pathlib import Path as _Path

            from ..research.data import _load_cache

            panel = _load_cache(_Path(path))
            if panel is None:
                self.log.warning(
                    f"factor_portfolio: warmup panel not found / not loadable: {path}"
                )
                return

            # Match strategy inst ids (may include .OKX venue suffix) against panel
            # ids (typically bare like BTC-USDT-SWAP). Try both forms.
            col_by_inst: dict[str, int] = {}
            for i, panel_iid in enumerate(panel.inst_ids):
                col_by_inst[panel_iid] = i
            counts = {"close": 0, "spot": 0, "funding": 0, "oi": 0}
            for inst_iid in self._closes.keys():
                bare = inst_iid.split(".")[0]
                col = col_by_inst.get(inst_iid)
                if col is None:
                    col = col_by_inst.get(bare)
                if col is None:
                    continue
                for row, ts in enumerate(panel.timestamps_ms):
                    close = float(panel.close[row, col])
                    if not np.isnan(close):
                        self._closes[inst_iid].append((ts, close))
                        vol = float(panel.volume_usdt[row, col])
                        self._volumes[inst_iid].append((ts, vol if not np.isnan(vol) else 0.0))
                        counts["close"] += 1
                        if panel.basis_apr is not None:
                            basis = float(panel.basis_apr[row, col])
                            if not np.isnan(basis):
                                spot = close / (1.0 + basis)
                                if spot > 0:
                                    self._spot_closes[inst_iid].append((ts, spot))
                                    counts["spot"] += 1
                    if panel.funding_rate is not None:
                        fr = float(panel.funding_rate[row, col])
                        if not np.isnan(fr):
                            self._funding_rates[inst_iid].append((ts, fr))
                            counts["funding"] += 1
                    if panel.open_interest is not None:
                        oi = float(panel.open_interest[row, col])
                        if not np.isnan(oi):
                            self._open_interests[inst_iid].append((ts, oi))
                            counts["oi"] += 1
            self.log.info(
                f"factor_portfolio: warmup loaded — close={counts['close']} "
                f"spot={counts['spot']} funding={counts['funding']} oi={counts['oi']}"
            )

        async def _poll_rest_loop(self) -> None:
            """Background task: poll funding + OI for each perp every N seconds.

            Per-inst errors are caught + logged so one bad inst doesn't kill the loop.
            Outer ``CancelledError`` propagates so on_stop can clean up.
            """
            import asyncio

            from .. import OKXRestClient, OKXSettings

            interval = max(60, int(self.config.rest_poll_interval_sec))
            try:
                async with OKXRestClient(OKXSettings()) as client:
                    while True:
                        for iid in self._inst_ids:
                            bare = iid.value.split(".")[0]  # strip ".OKX" venue suffix
                            try:
                                fr = await client.public.get_funding_rate(bare)
                                self._funding_rates[iid.value].append(
                                    (int(fr.funding_time), float(fr.funding_rate)),
                                )
                            except Exception as exc:  # noqa: BLE001
                                self.log.warning(
                                    f"factor_portfolio: funding poll {bare}: {exc}"
                                )
                            try:
                                oi = await client.public.get_open_interest(bare)
                                self._open_interests[iid.value].append(
                                    (int(oi.ts), float(oi.oi_ccy)),
                                )
                            except Exception as exc:  # noqa: BLE001
                                self.log.warning(
                                    f"factor_portfolio: oi poll {bare}: {exc}"
                                )
                        await asyncio.sleep(interval)
            except asyncio.CancelledError:
                self.log.info("factor_portfolio: rest_poll_task cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"factor_portfolio: rest_poll_task crashed: {exc}")

        def on_bar(self, bar: "Bar") -> None:
            inst_value = bar.bar_type.instrument_id.value
            ts_ms = int(bar.ts_event // 1_000_000)
            if inst_value in self._closes:
                # Perp bar
                self._closes[inst_value].append((ts_ms, bar.close.as_double()))
                self._volumes[inst_value].append(
                    (ts_ms, bar.volume.as_double() * bar.close.as_double()),
                )
                if (self._weights
                        and ts_ms - self._last_rebalance_ms
                        >= self.config.rebalance_hours * 3_600_000):
                    self._rebalance(ts_ms)
                    self._last_rebalance_ms = ts_ms
                return
            if inst_value in self._spot_to_perp:
                # Spot bar — buffer for the corresponding perp's basis computation.
                # Does NOT trigger rebalance (perp bar drives rebalance cadence).
                perp_inst = self._spot_to_perp[inst_value]
                self._spot_closes[perp_inst].append((ts_ms, bar.close.as_double()))

        def _build_panel(self) -> FactorPanel | None:
            inst_ids = tuple(iid.value for iid in self._inst_ids)
            T_min = min(len(self._closes[i]) for i in inst_ids)
            if T_min < 24:
                return None
            T = T_min
            # Real bar timestamps (last T per inst), aligned across inst via tail-take.
            # We use perp inst[0]'s timestamps as the canonical axis since rebalance is
            # driven by perp bars; spot ts intersection happens per-cell during basis fill.
            canonical_inst = inst_ids[0]
            ts_axis = tuple(t for t, _ in list(self._closes[canonical_inst])[-T:])

            close = np.column_stack([
                np.asarray([v for _, v in list(self._closes[i])[-T:]], dtype=float)
                for i in inst_ids
            ])
            volume = np.column_stack([
                np.asarray([v for _, v in list(self._volumes[i])[-T:]], dtype=float)
                for i in inst_ids
            ])

            # basis_apr: (perp_close - spot_close) / spot_close, aligned by ts.
            # Each perp has its own (ts, spot_close) deque; we look up each canonical
            # ts in that deque and fill NaN if no spot bar at that ts.
            basis_arr = np.full((T, len(inst_ids)), np.nan, dtype=float)
            any_basis = False
            perp_close_by_ts = {
                i: {t: v for t, v in list(self._closes[i])[-T:]}
                for i in inst_ids
            }
            for col, perp_inst in enumerate(inst_ids):
                spot_close_by_ts = {t: v for t, v in self._spot_closes[perp_inst]}
                if not spot_close_by_ts:
                    continue
                for row, ts in enumerate(ts_axis):
                    spot_v = spot_close_by_ts.get(ts)
                    perp_v = perp_close_by_ts[perp_inst].get(ts)
                    if spot_v is not None and spot_v > 0 and perp_v is not None:
                        basis_arr[row, col] = (perp_v - spot_v) / spot_v
                        any_basis = True

            # funding_rate / open_interest: forward-fill from polled (ts, val) deques
            # to the canonical perp ts axis. Empty buffers → array stays NaN → factor
            # returns NaN → synthesize_score skips it (no false trades from stale data).
            funding_arr = _ffill_to_axis(self._funding_rates, ts_axis, inst_ids)
            oi_arr = _ffill_to_axis(self._open_interests, ts_axis, inst_ids)

            return FactorPanel(
                inst_ids=inst_ids, timestamps_ms=ts_axis,
                close=close, volume_usdt=volume,
                funding_rate=funding_arr if np.any(np.isfinite(funding_arr)) else None,
                open_interest=oi_arr if np.any(np.isfinite(oi_arr)) else None,
                basis_apr=basis_arr if any_basis else None,
            )

        def _rebalance(self, ts_ms: int) -> None:
            panel = self._build_panel()
            if panel is None:
                return
            score, missing = synthesize_score(panel, self._weights)
            if missing:
                self.log.warning(f"factor_portfolio: skipped factors {missing}")
            if not np.any(np.isfinite(score)):
                self.log.warning("factor_portfolio: all-NaN score; no trades this round")
                return
            longs, shorts = select_top_bot(
                score,
                top_k_long=self.config.top_k_long,
                top_k_short=self.config.top_k_short,
            )
            target = {panel.inst_ids[i]: "long" for i in longs}
            target.update({panel.inst_ids[i]: "short" for i in shorts})

            # Close legs no longer in target
            for inst_v, (cur_dir, _, _) in list(self._positions.items()):
                if inst_v not in target or target[inst_v] != cur_dir:
                    self._close_leg(inst_v, reason="REBALANCE", exit_ts_ms=ts_ms)

            for inst_v, direction in target.items():
                if inst_v not in self._positions:
                    self._open_leg(inst_v, direction, ts_ms=ts_ms)

            self.log.info(
                f"factor_portfolio rebalance: longs={longs} shorts={shorts} "
                f"open_legs={len(self._positions)}"
            )

        def _open_leg(self, inst_value: str, direction: str, *, ts_ms: int) -> None:
            cfg: FactorPortfolioConfig = self.config  # type: ignore[assignment]
            inst_id = InstrumentId.from_str(inst_value)
            inst = self.cache.instrument(inst_id)
            if inst is None:
                return
            closes = list(self._closes[inst_value])
            if not closes:
                return
            entry_px = closes[-1][1]  # _closes now stores (ts_ms, close); take value
            ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
            lot = float(inst.size_increment) if float(inst.size_increment) > 0 else 1.0
            sl_distance = entry_px * 0.01  # conservative 1% SL fallback
            stop = entry_px - sl_distance if direction == "long" else entry_px + sl_distance
            equity = effective_equity_usdt(self._allocated_equity_usdt, cfg.account_equity_usdt)
            contracts = position_contracts(
                account_equity_usdt=equity, risk_pct=cfg.risk_pct,
                entry_price=entry_px, stop_price=stop,
                ct_val=ct_val, min_sz=lot, lot_sz=lot,
            )
            if contracts <= 0:
                return
            intent = RiskIntent(
                strategy_id=str(self.id), instrument_id=inst_value,
                direction=direction,  # type: ignore[arg-type]
                size=contracts, entry_price=entry_px, stop_price=stop,
                account_equity_usdt=equity,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None or adjusted <= 0:
                return
            qty_obj = safe_make_qty(inst, adjusted, self.log, ctx=f"open {inst_value}")
            if qty_obj is None:
                return
            side = OrderSide.BUY if direction == "long" else OrderSide.SELL
            self.submit_order(self.order_factory.market(
                instrument_id=inst_id, order_side=side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
            ))
            self._positions[inst_value] = (direction, adjusted, ts_ms)
            self.log.info(f"OPEN {direction} {inst_value} qty={adjusted}")

        def _close_leg(self, inst_value: str, *, reason: str, exit_ts_ms: int) -> None:
            pos = self._positions.get(inst_value)
            if pos is None:
                return
            direction, contracts, _entry_ts = pos
            inst_id = InstrumentId.from_str(inst_value)
            inst = self.cache.instrument(inst_id)
            if inst is None:
                return
            qty_obj = safe_make_qty(inst, contracts, ctx=f"close {inst_value}")
            if qty_obj is None:
                return
            close_side = OrderSide.SELL if direction == "long" else OrderSide.BUY
            self.submit_order(self.order_factory.market(
                instrument_id=inst_id, order_side=close_side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
                reduce_only=True,
            ))
            self.log.info(f"CLOSE {direction} {inst_value} reason={reason}")
            self._positions.pop(inst_value, None)


else:  # pragma: no cover

    class FactorPortfolioConfig:  # type: ignore[no-redef]
        pass

    class FactorPortfolioStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError(
                "nautilus_trader not installed; run pip install -e '.[strategy]'"
            )


__all__ = [
    "FactorPortfolioConfig", "FactorPortfolioStrategy",
    "FactorWeight",
    "cross_section_zscore", "select_top_bot", "synthesize_score",
]
