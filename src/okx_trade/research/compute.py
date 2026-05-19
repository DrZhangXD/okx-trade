"""compute_factor: validate panel has required data, run factor, validate output shape."""
from __future__ import annotations

import numpy as np

from .panel import FactorPanel
from .registry import get_factor


def compute_factor(factor_id: str, panel: FactorPanel) -> np.ndarray:
    """Run a registered factor on a panel.

    Raises:
        ValueError: if panel is missing any of the factor's ``required_data``
            (the corresponding panel attribute is ``None``), or if the factor
            function returns an array whose shape != ``(panel.t, panel.n)``.
    """
    spec = get_factor(factor_id)
    for field in spec.required_data:
        if getattr(panel, field, None) is None:
            raise ValueError(
                f"factor {factor_id!r} requires panel.{field} but it is None"
            )
    out = spec.func(panel)
    if not isinstance(out, np.ndarray):
        raise ValueError(
            f"factor {factor_id!r} returned {type(out).__name__}, expected np.ndarray"
        )
    if out.shape != (panel.t, panel.n):
        raise ValueError(
            f"factor {factor_id!r} returned shape {out.shape}, "
            f"expected ({panel.t}, {panel.n})"
        )
    return out


__all__ = ["compute_factor"]
