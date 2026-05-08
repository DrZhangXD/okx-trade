"""Runtime（M5）：把 yaml + tracker + allocator 装进 NT TradingNode。"""
from __future__ import annotations

from .live_node import LiveContext, build_live_context

__all__ = ["LiveContext", "build_live_context"]
