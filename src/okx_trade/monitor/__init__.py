"""监控 + 告警 + 日报（M5）。

| 组件 | 用途 |
|---|---|
| ``Alert`` / ``AlertSeverity`` | 告警事件 |
| ``AlertSink`` / ``LogSink`` / ``JsonlSink`` / ``TelegramSink`` | 输出通道 |
| ``LiveMonitor`` | 周期性轮询风控状态，触发条件时 emit |
| ``DailyReporter`` | 每日把 tracker 数据拍扁成 JSON |

工作流：
- ``runtime/live_node.py`` 构造 ``LiveMonitor`` 并 spawn 它的 ``run()`` task；
- 策略 ``on_position_closed`` / ``_feed_risk_data`` 已经把 PnL / equity 写进
  ``PnLTracker``，``DailyReporter`` 负责一次性消费这些数据出报。
"""
from __future__ import annotations

from .alerts import (
    Alert,
    AlertSeverity,
    AlertSink,
    JsonlSink,
    LogSink,
    TelegramSink,
    fan_out,
)
from .daily_report import DailyReport, DailyReporter, StrategyDailyReport
from .live import LiveMonitor, MonitorThresholds

__all__ = [
    "Alert",
    "AlertSeverity",
    "AlertSink",
    "DailyReport",
    "DailyReporter",
    "JsonlSink",
    "LiveMonitor",
    "LogSink",
    "MonitorThresholds",
    "StrategyDailyReport",
    "TelegramSink",
    "fan_out",
]
