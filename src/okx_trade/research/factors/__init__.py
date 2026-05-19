"""Importing this package triggers @register_factor side effects for all built-in factors."""
from __future__ import annotations

from . import basis, flow, funding_oi, momentum, volatility  # noqa: F401

__all__ = ["basis", "flow", "funding_oi", "momentum", "volatility"]
