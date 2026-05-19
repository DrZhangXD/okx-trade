"""Factor registry: @register_factor decorator + global lookup.

Pure module-level dict — no thread safety required (research pipeline is single-threaded;
strategy reads at startup only).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

import numpy as np

from .panel import FactorPanel

FactorFunc = Callable[[FactorPanel], np.ndarray]
Direction = Literal["long_high", "long_low"]
_VALID_DIRECTIONS = ("long_high", "long_low")


@dataclass(frozen=True, slots=True)
class FactorSpec:
    id: str
    category: str
    description: str
    direction: Direction
    required_data: tuple[str, ...]
    min_history_bars: int
    rebalance_minutes: int
    func: FactorFunc


_REGISTRY: dict[str, FactorSpec] = {}


def register_factor(
    *,
    id: str,
    category: str,
    description: str,
    direction: Direction,
    required_data: tuple[str, ...],
    min_history_bars: int,
    rebalance_minutes: int,
) -> Callable[[FactorFunc], FactorFunc]:
    """Decorator that registers a factor function.

    Raises:
        ValueError: if ``id`` already registered or ``direction`` invalid.
    """
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(
            f"direction must be one of {_VALID_DIRECTIONS}, got {direction!r}"
        )

    def deco(func: FactorFunc) -> FactorFunc:
        if id in _REGISTRY:
            raise ValueError(f"factor {id!r} already registered")
        _REGISTRY[id] = FactorSpec(
            id=id, category=category, description=description,
            direction=direction, required_data=tuple(required_data),
            min_history_bars=min_history_bars,
            rebalance_minutes=rebalance_minutes, func=func,
        )
        return func

    return deco


def get_factor(factor_id: str) -> FactorSpec:
    if factor_id not in _REGISTRY:
        raise KeyError(f"factor {factor_id!r} not registered")
    return _REGISTRY[factor_id]


def list_factors() -> list[FactorSpec]:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def clear_registry() -> None:
    """Test-only: wipe registry between tests."""
    _REGISTRY.clear()


__all__ = [
    "Direction", "FactorFunc", "FactorSpec",
    "clear_registry", "get_factor", "list_factors", "register_factor",
]
