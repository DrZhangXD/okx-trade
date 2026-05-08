"""Alert + AlertSink 抽象 + 内置 sink 实现。

设计
----
- ``Alert``：immutable dataclass，包含 severity / source / message / context；
- ``AlertSink``：``emit(alert)`` 协议；
- 默认 sink：
  * ``LogSink`` —— 写到 stdlib logger（LiveMonitor / runtime 都接得上）；
  * ``JsonlSink`` —— append 一行 JSON 到本地文件（``var/alerts.jsonl``）；
  * ``TelegramSink`` —— stub，``__init__`` 即 ``NotImplementedError``，留 M6 接。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class AlertSeverity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Alert:
    """单个告警事件。

    Attributes:
        severity: ``INFO`` / ``WARN`` / ``CRITICAL``。
        source: 告警源标识（``drawdown`` / ``kelly`` / ``correlation`` /
            ``order_reject``）。
        message: 人类可读 1-3 句的描述。
        ts_ms: 触发时刻（unix ms）。
        context: 自由扩展字段（具体的数值 / 策略 id 等），用于 JSON sink。
    """

    severity: AlertSeverity
    source: str
    message: str
    ts_ms: int
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


class AlertSink(Protocol):
    def emit(self, alert: Alert) -> None: ...


class LogSink:
    """把 alert 转成 logger.info/warning/error。"""

    def __init__(self, logger_name: str = "okx_trade.alerts") -> None:
        self.logger = logging.getLogger(logger_name)

    def emit(self, alert: Alert) -> None:
        msg = f"[{alert.source}] {alert.message}"
        if alert.severity == AlertSeverity.CRITICAL:
            self.logger.error(msg)
        elif alert.severity == AlertSeverity.WARN:
            self.logger.warning(msg)
        else:
            self.logger.info(msg)


class JsonlSink:
    """把 alert 序列化为一行 JSON 追加到文件。

    每行一条 ``Alert.to_dict()``。文件夹自动创建；线程安全。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, alert: Alert) -> None:
        line = json.dumps(alert.to_dict(), ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


class TelegramSink:
    """stub —— M5 不实接，留 M6 task。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "TelegramSink not implemented yet (planned for M6); "
            "use LogSink + JsonlSink for now."
        )

    def emit(self, alert: Alert) -> None:  # pragma: no cover
        raise NotImplementedError


def fan_out(sinks: list[AlertSink], alert: Alert) -> None:
    """把同一条 alert 推给所有 sink；单个 sink 异常不影响其他 sink。"""
    for s in sinks:
        try:
            s.emit(alert)
        except Exception:
            logging.getLogger("okx_trade.alerts").exception(
                f"sink {type(s).__name__} failed",
            )


__all__ = [
    "Alert",
    "AlertSeverity",
    "AlertSink",
    "JsonlSink",
    "LogSink",
    "TelegramSink",
    "fan_out",
]
