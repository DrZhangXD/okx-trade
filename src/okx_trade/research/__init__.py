"""okx_trade.research — factor research lab (offline pipeline + registry).

Importing this package loads all built-in factor modules and populates the registry.
"""
from __future__ import annotations

from . import factors  # noqa: F401 — triggers factor registration

__all__ = ["factors"]
