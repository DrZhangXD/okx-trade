"""Shared 1-minute bar buffer + outlier guard singleton (2026-05-26 Phase 1).

Strategies subscribe 1m bars via NT and call ``feed_bar`` on each tick.
NT DataEngine dedups bar subscriptions, so multiple strategies watching
the same inst share a single feed naturally — this service just maintains
the rolling buffer + delegates outlier math to ``outlier_check``.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class VolatilityFilterConfig:
    """Global config; one instance shared across all strategies.

    Loaded from ``live.yaml.volatility_filter`` block.
    """
    enable: bool = False
    window_min: int = 60
    baseline_min: int = 1440
    warmup_min: int = 1440
    ratio_threshold: float = 3.0
    buffer_max: int = 2000


class VolatilityFilter:
    """1m bar buffer + outlier check. Singleton owned by build_live_context."""

    def __init__(self, config: VolatilityFilterConfig, log) -> None:
        self._cfg = config
        self._log = log
        self._closes: dict[str, deque[float]] = {}

    def feed_bar(self, inst_id: str, close: float) -> None:
        """Append a 1m close to the inst's buffer. Lazy-creates the deque
        with ``maxlen=config.buffer_max`` on first call for the inst.
        ``inst_id`` is OKX format (no ``.OKX`` suffix); callers strip.
        """
        buf = self._closes.get(inst_id)
        if buf is None:
            buf = deque(maxlen=self._cfg.buffer_max)
            self._closes[inst_id] = buf
        buf.append(float(close))

    def buffer_size(self, inst_id: str) -> int:
        """Diagnostic: number of bars accumulated. 0 if not yet fed."""
        return len(self._closes.get(inst_id, ()))

    def allow(self, inst_id: str) -> tuple[bool, str]:
        """Decide if a new leg on ``inst_id`` should be allowed by the
        outlier guard.

        Returns:
            ``(True, "disabled")`` if config.enable is False.
            ``(True, "warmup")`` if buffer has < warmup_min entries.
            ``(True, "no_baseline")`` if baseline std == 0 (flat history).
            ``(True, "ok")`` if recent vol within ratio_threshold of baseline.
            ``(False, "vol_ratio=R>T")`` otherwise.
        """
        if not self._cfg.enable:
            return True, "disabled"
        from ..strategies._isolated_helpers import outlier_check
        closes = self._closes.get(inst_id)
        if closes is None:
            return True, "warmup"
        return outlier_check(
            closes=list(closes),
            window=self._cfg.window_min,
            baseline=self._cfg.baseline_min,
            warmup=self._cfg.warmup_min,
            ratio_threshold=self._cfg.ratio_threshold,
        )
