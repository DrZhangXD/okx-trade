"""FactorPortfolioStrategy — generic factor synthesizer (linear weighted z-score).

Pure-function layer (NT-independent, used by tests + the NT Strategy class below).
The NT Strategy class is implemented in a follow-up section that imports NT lazily.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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
        """
        instrument_ids: list[str]
        bar_type_template: str
        rebalance_hours: int = 4
        top_k_long: int = 5
        top_k_short: int = 5
        risk_pct: float = 0.002
        account_equity_usdt: float = 10_000.0
        factor_weights: list[tuple[str, float]] = []
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
            # Buffer enough bars for the slowest factor (vol_of_vol_30d = 60d * 24 + slack)
            self._closes: dict[str, deque[float]] = {
                iid.value: deque(maxlen=60 * 24 + 50) for iid in self._inst_ids
            }
            self._volumes: dict[str, deque[float]] = {
                iid.value: deque(maxlen=60 * 24 + 50) for iid in self._inst_ids
            }
            self._last_rebalance_ms: int = 0
            self._positions: dict[str, tuple[str, float, int]] = {}
            self._allocated_equity_usdt: float | None = None
            self._weights = [FactorWeight(id=fid, weight=w)
                             for fid, w in config.factor_weights]
            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._pnl_tracker = None  # type: ignore[var-annotated]

        def on_start(self) -> None:
            for bar_type in self._bar_types.values():
                self.subscribe_bars(bar_type)
            self.log.info(
                f"factor_portfolio start: factors={[w.id for w in self._weights]} "
                f"top_k={self.config.top_k_long}/{self.config.top_k_short}"
            )
            if not self._weights:
                self.log.warning("factor_portfolio: no factor_weights configured; idle")

        def on_stop(self) -> None:
            self.log.info(f"factor_portfolio stop; open_legs={len(self._positions)}")

        def on_bar(self, bar: "Bar") -> None:
            inst_value = bar.bar_type.instrument_id.value
            if inst_value not in self._closes:
                return
            self._closes[inst_value].append(bar.close.as_double())
            self._volumes[inst_value].append(bar.volume.as_double() * bar.close.as_double())

            now_ms = int(bar.ts_event // 1_000_000)
            if (self._weights
                    and now_ms - self._last_rebalance_ms
                    >= self.config.rebalance_hours * 3_600_000):
                self._rebalance(now_ms)
                self._last_rebalance_ms = now_ms

        def _build_panel(self) -> FactorPanel | None:
            inst_ids = tuple(iid.value for iid in self._inst_ids)
            T_min = min(len(self._closes[i]) for i in inst_ids)
            if T_min < 24:
                return None
            T = T_min
            close = np.column_stack([
                np.asarray(list(self._closes[i])[-T:], dtype=float) for i in inst_ids
            ])
            volume = np.column_stack([
                np.asarray(list(self._volumes[i])[-T:], dtype=float) for i in inst_ids
            ])
            ts = tuple(range(T))  # synthetic ts; factor functions don't use ts
            return FactorPanel(
                inst_ids=inst_ids, timestamps_ms=ts,
                close=close, volume_usdt=volume,
                funding_rate=None, open_interest=None, basis_apr=None,
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
            entry_px = closes[-1]
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
