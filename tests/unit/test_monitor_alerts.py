"""M5 monitor.alerts + monitor.live 单测。"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest

from okx_trade.monitor import (
    Alert,
    AlertSeverity,
    JsonlSink,
    LiveMonitor,
    LogSink,
    MonitorThresholds,
    TelegramSink,
    fan_out,
)
from okx_trade.risk import RiskConfig, build_risk_manager


# ---------------------------------------------------------------------------
# Alert / sinks
# ---------------------------------------------------------------------------
def test_alert_to_dict_round_trip() -> None:
    a = Alert(
        severity=AlertSeverity.WARN,
        source="kelly",
        message="hi",
        ts_ms=12345,
        context={"x": 1},
    )
    d = a.to_dict()
    assert d == {
        "severity": "warn", "source": "kelly", "message": "hi",
        "ts_ms": 12345, "context": {"x": 1},
    }


def test_log_sink_writes_to_logger(caplog: pytest.LogCaptureFixture) -> None:
    sink = LogSink()
    with caplog.at_level("INFO", logger="okx_trade.alerts"):
        sink.emit(Alert(AlertSeverity.WARN, "src", "msg", 0))
    assert any("[src] msg" in rec.message for rec in caplog.records)


def test_jsonl_sink_appends_one_line_per_alert(tmp_path: Path) -> None:
    p = tmp_path / "alerts.jsonl"
    sink = JsonlSink(p)
    sink.emit(Alert(AlertSeverity.INFO, "a", "x", 1, {"k": 1}))
    sink.emit(Alert(AlertSeverity.WARN, "b", "y", 2))
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    j1 = json.loads(lines[0])
    assert j1["source"] == "a" and j1["context"] == {"k": 1}


def test_jsonl_sink_creates_parent_dir(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "deep" / "a.jsonl"
    sink = JsonlSink(p)
    sink.emit(Alert(AlertSeverity.INFO, "x", "y", 1))
    assert p.exists()


def test_telegram_sink_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        TelegramSink()


def test_fan_out_continues_when_one_sink_fails() -> None:
    class BadSink:
        def emit(self, a: Alert) -> None:
            raise RuntimeError("boom")

    received: list[Alert] = []

    class GoodSink:
        def emit(self, a: Alert) -> None:
            received.append(a)

    fan_out([BadSink(), GoodSink()], Alert(AlertSeverity.INFO, "x", "y", 0))
    assert len(received) == 1


# ---------------------------------------------------------------------------
# LiveMonitor.poll_once 行为
# ---------------------------------------------------------------------------
def test_monitor_kelly_jump_emits_info() -> None:
    cfg = RiskConfig(enable_kelly=True, kelly_win_rate=0.5)
    _, handles = build_risk_manager(cfg)
    received: list[Alert] = []

    class CaptureSink:
        def emit(self, a: Alert) -> None:
            received.append(a)

    mon = LiveMonitor(
        {"s1": handles}, [CaptureSink()],
        thresholds=MonitorThresholds(kelly_jump=0.05),
        clock=lambda: 1000,
    )
    # 第一次 poll：建立 baseline，无 alert
    mon.poll_once()
    assert received == []

    # 改 win_rate 到 0.6（跨阈值 0.05）
    handles.kelly.set_stats(win_rate=0.6, avg_r=2.0)  # type: ignore[union-attr]
    mon.poll_once()
    assert any(a.source == "kelly" and a.severity == AlertSeverity.INFO for a in received)


def test_monitor_drawdown_state_change_emits_critical() -> None:
    from okx_trade.risk import DrawdownState
    cfg = RiskConfig(enable_drawdown=True, drawdown_daily_pct=0.03)
    _, handles = build_risk_manager(cfg)
    received: list[Alert] = []
    class Cap:
        def emit(self, a: Alert) -> None: received.append(a)
    mon = LiveMonitor({"s1": handles}, [Cap()], clock=lambda: 1000)

    # 模拟 tracker 进入 DAILY_BREACH
    handles.drawdown_tracker.state = DrawdownState.DAILY_BREACH  # type: ignore[union-attr]
    mon.poll_once()
    assert any(
        a.source == "drawdown" and a.severity == AlertSeverity.CRITICAL
        for a in received
    )


def test_monitor_correlation_high_emits_warn() -> None:
    cfg = RiskConfig(enable_correlation=True, correlation_window=10)
    _, handles = build_risk_manager(cfg)
    # 手动喂两个完美相关序列
    handles.correlation._returns["s1"] = deque([0.01, -0.01, 0.02, -0.02, 0.01], maxlen=10)  # type: ignore[union-attr]
    handles.correlation._returns["s2"] = deque([0.01, -0.01, 0.02, -0.02, 0.01], maxlen=10)  # type: ignore[union-attr]
    received: list[Alert] = []
    class Cap:
        def emit(self, a: Alert) -> None: received.append(a)
    mon = LiveMonitor(
        {"s1": handles, "s2": handles}, [Cap()],
        thresholds=MonitorThresholds(correlation_max=0.7),
        clock=lambda: 1000,
    )
    mon.poll_once()
    assert any(a.source == "correlation" and a.severity == AlertSeverity.WARN for a in received)


def test_monitor_no_alerts_when_nothing_changed() -> None:
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    received: list[Alert] = []
    class Cap:
        def emit(self, a: Alert) -> None: received.append(a)
    mon = LiveMonitor({"s1": handles}, [Cap()], clock=lambda: 1000)

    mon.poll_once()
    mon.poll_once()
    mon.poll_once()
    assert received == []


# ---------------------------------------------------------------------------
# 启动信标（修红灯 3b）：每次 systemd 拉起服务，alerts.jsonl 应留痕
# ---------------------------------------------------------------------------
def test_monitor_emits_service_started_on_first_run() -> None:
    """``LiveMonitor._emit_service_started`` 应当 emit 一条 INFO `service` alert。"""
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    received: list[Alert] = []
    class Cap:
        def emit(self, a: Alert) -> None: received.append(a)
    mon = LiveMonitor(
        {"s1": handles, "s2": handles}, [Cap()],
        poll_interval_s=60,
        clock=lambda: 1234567890,
    )

    mon._emit_service_started()

    assert len(received) == 1
    a = received[0]
    assert a.source == "service"
    assert a.severity == AlertSeverity.INFO
    assert a.ts_ms == 1234567890
    assert "monitor started" in a.message
    assert a.context["poll_interval_s"] == 60
    assert a.context["strategy_count"] == 2
    assert set(a.context["strategy_ids"]) == {"s1", "s2"}
    assert isinstance(a.context["pid"], int)


def test_monitor_emits_service_started_via_jsonl_sink(tmp_path: Path) -> None:
    """端到端：JsonlSink 收到 service-started 后写入文件。"""
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    sink_path = tmp_path / "alerts.jsonl"
    mon = LiveMonitor(
        {"s1": handles}, [JsonlSink(sink_path)],
        clock=lambda: 1000,
    )

    mon._emit_service_started()

    assert sink_path.exists()
    line = sink_path.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["source"] == "service"
    assert rec["severity"] == "info"
    assert "monitor started" in rec["message"]


def test_emit_service_started_swallows_sink_exceptions() -> None:
    """sink 抛异常不能让 service-started emit 拉崩 ``run()``。"""
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)

    class BoomSink:
        def emit(self, _: Alert) -> None:
            raise RuntimeError("sink down")

    mon = LiveMonitor({"s1": handles}, [BoomSink()], clock=lambda: 1000)
    # 不应 raise（fan_out 内部已捕获 + run() 入口又包了一层 try）
    mon._emit_service_started()
