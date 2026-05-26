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


# ---------------------------------------------------------------------------
# Heartbeat（healthcheck 读取的存活信号）
# ---------------------------------------------------------------------------
def test_heartbeat_written_with_clock_ms(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``_write_heartbeat`` 应把 ``clock()`` 返回的 ms 原子写入指定文件。"""
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    hb = tmp_path / "heartbeat.ts"
    mon = LiveMonitor(
        {"s1": handles}, [],
        clock=lambda: 1_700_000_000_000,
        heartbeat_path=hb,
    )
    mon._write_heartbeat()
    assert hb.exists()
    assert hb.read_text().strip() == "1700000000000"


def test_heartbeat_creates_parent_dir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """构造时 heartbeat 文件父目录不存在 → 应自动 mkdir。"""
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    hb = tmp_path / "newdir" / "heartbeat.ts"
    mon = LiveMonitor(
        {"s1": handles}, [],
        clock=lambda: 1234,
        heartbeat_path=hb,
    )
    assert hb.parent.exists()
    mon._write_heartbeat()
    assert hb.read_text().strip() == "1234"


def test_heartbeat_disabled_when_path_none(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``heartbeat_path=None`` 视为关闭；``_write_heartbeat`` no-op。"""
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    mon = LiveMonitor(
        {"s1": handles}, [],
        clock=lambda: 1234,
        heartbeat_path=None,
    )
    mon._write_heartbeat()  # 不应 raise，也不应写任何文件
    assert mon.heartbeat_path is None


def test_heartbeat_overwrites_on_repeat_call(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """连续两次 ``_write_heartbeat`` 应原子覆盖，文件只剩最新值。"""
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    hb = tmp_path / "heartbeat.ts"
    ticks = iter([1000, 2000, 3000])
    mon = LiveMonitor(
        {"s1": handles}, [],
        clock=lambda: next(ticks),
        heartbeat_path=hb,
    )
    mon._write_heartbeat()
    mon._write_heartbeat()
    mon._write_heartbeat()
    assert hb.read_text().strip() == "3000"
    # 临时文件不应残留
    assert not (tmp_path / "heartbeat.ts.tmp").exists()


# ---------------------------------------------------------------------------
# Alloc refresh:把 OKX 实时余额 重新分配 写到每个 strategy 实例
# 回归 2026-05-18 发现:live.yaml 的 account.equity_usdt 硬编码 10000,demo
# 真实 81k 没参与 sizing → 持仓只占总资金 ~10%。
# ---------------------------------------------------------------------------


class _FakeStrategy:
    """裸壳 Strategy 替身,只承载 `_allocated_equity_usdt` 属性。"""

    def __init__(self) -> None:
        self._allocated_equity_usdt: float | None = None


class _FakeAllocator:
    """按 total / N 平均分(equal-weight),足够测试 monitor 的写回路径。"""

    def __init__(self, raises: Exception | None = None) -> None:
        self._raises = raises
        self.calls: list[tuple[list[str], Any]] = []

    def allocate(self, names, tracker, *, total_equity_usdt):  # noqa: ANN001
        self.calls.append((list(names), float(total_equity_usdt)))
        if self._raises:
            raise self._raises
        share = float(total_equity_usdt) / max(1, len(names))
        return {n: share for n in names}


async def _mk_mon_with_alloc(equity_value, allocator=None, strategies=None,
                              alloc_interval_s=3600):  # noqa: ANN001
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    if equity_value is None:
        provider = lambda: None  # noqa: E731
    elif callable(equity_value):
        provider = equity_value
    else:
        async def provider():  # noqa
            return equity_value
    strategies = strategies if strategies is not None else {
        "s1": _FakeStrategy(), "s2": _FakeStrategy(),
    }
    allocator = allocator or _FakeAllocator()
    mon = LiveMonitor(
        {"s1": handles, "s2": handles}, [],
        clock=lambda: 1_000_000,
        heartbeat_path=None,
        equity_provider=provider,
        allocator=allocator,
        strategies_by_name=strategies,
        pnl_tracker=None,
        alloc_interval_s=alloc_interval_s,
    )
    return mon, allocator, strategies


@pytest.mark.asyncio
async def test_alloc_refresh_writes_equity_to_strategies() -> None:
    """正常路径:provider → allocator → 每个 strategy 拿到 share。"""
    mon, alloc, strategies = await _mk_mon_with_alloc(80000.0)
    await mon._refresh_allocations()
    assert strategies["s1"]._allocated_equity_usdt == 40000.0
    assert strategies["s2"]._allocated_equity_usdt == 40000.0
    assert len(alloc.calls) == 1
    assert alloc.calls[0][1] == 80000.0


@pytest.mark.asyncio
async def test_alloc_refresh_writes_account_total_equity_to_strategies() -> None:
    """2026-05-26 fix: monitor 把账户级 totalEq 也下发,strategy._feed_risk_data
    把它写进 equities 表的 snapshot,而不是 NT USDT 单币 balance(后者在仓位
    有非 USDT collateral 时严重低估,2026-05-26 事故)."""
    mon, _alloc, strategies = await _mk_mon_with_alloc(80000.0)
    await mon._refresh_allocations()
    assert strategies["s1"]._account_total_equity_usdt == 80000.0
    assert strategies["s2"]._account_total_equity_usdt == 80000.0


@pytest.mark.asyncio
async def test_alloc_refresh_provider_returns_none_skips() -> None:
    """provider 返回 None → 不改 strategy 状态。"""
    mon, _alloc, strategies = await _mk_mon_with_alloc(None)
    strategies["s1"]._allocated_equity_usdt = 999.0  # 先有上次值
    await mon._refresh_allocations()
    assert strategies["s1"]._allocated_equity_usdt == 999.0


@pytest.mark.asyncio
async def test_alloc_refresh_provider_returns_zero_skips() -> None:
    """provider 返回 0 / 负数 → 跳过(防 sizing 算 0)。"""
    mon, _alloc, strategies = await _mk_mon_with_alloc(0.0)
    strategies["s1"]._allocated_equity_usdt = 100.0
    await mon._refresh_allocations()
    assert strategies["s1"]._allocated_equity_usdt == 100.0


@pytest.mark.asyncio
async def test_alloc_refresh_provider_exception_emits_warn() -> None:
    """provider 异常 → emit WARN alert,不挂 monitor。"""
    received: list[Alert] = []
    class Cap:
        def emit(self, a: Alert) -> None: received.append(a)
    async def boom():
        raise RuntimeError("net down")
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    strategies = {"s1": _FakeStrategy()}
    mon = LiveMonitor(
        {"s1": handles}, [Cap()],
        clock=lambda: 1234,
        heartbeat_path=None,
        equity_provider=boom,
        allocator=_FakeAllocator(),
        strategies_by_name=strategies,
    )
    await mon._refresh_allocations()
    assert strategies["s1"]._allocated_equity_usdt is None
    assert any(a.source == "alloc_refresh" and a.severity == AlertSeverity.WARN
               for a in received)


@pytest.mark.asyncio
async def test_alloc_refresh_allocator_exception_emits_warn() -> None:
    """allocator.allocate 异常 → emit WARN,strategy 不动。"""
    received: list[Alert] = []
    class Cap:
        def emit(self, a: Alert) -> None: received.append(a)
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    strategies = {"s1": _FakeStrategy()}
    mon = LiveMonitor(
        {"s1": handles}, [Cap()],
        clock=lambda: 1234,
        heartbeat_path=None,
        equity_provider=lambda: 50000.0,
        allocator=_FakeAllocator(raises=RuntimeError("bad allocator")),
        strategies_by_name=strategies,
    )
    await mon._refresh_allocations()
    assert strategies["s1"]._allocated_equity_usdt is None
    assert any(a.source == "alloc_refresh" and a.severity == AlertSeverity.WARN
               for a in received)


@pytest.mark.asyncio
async def test_alloc_refresh_emits_info_on_change() -> None:
    """成功 re-allocate 且 allocations 变了 → INFO alert with allocations 字典。"""
    received: list[Alert] = []
    class Cap:
        def emit(self, a: Alert) -> None: received.append(a)
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    strategies = {"s1": _FakeStrategy(), "s2": _FakeStrategy()}
    mon = LiveMonitor(
        {"s1": handles, "s2": handles}, [Cap()],
        clock=lambda: 1234,
        heartbeat_path=None,
        equity_provider=lambda: 100000.0,
        allocator=_FakeAllocator(),
        strategies_by_name=strategies,
    )
    await mon._refresh_allocations()
    info_alerts = [a for a in received if a.source == "alloc_refresh"
                   and a.severity == AlertSeverity.INFO]
    assert len(info_alerts) == 1
    ctx = info_alerts[0].context
    assert ctx["total_equity"] == 100000.0
    assert ctx["allocations"] == {"s1": 50000.0, "s2": 50000.0}


@pytest.mark.asyncio
async def test_alloc_refresh_no_info_when_unchanged() -> None:
    """同一 equity 调两次 → 第二次 allocations 没变,不再 emit INFO(降噪)。"""
    received: list[Alert] = []
    class Cap:
        def emit(self, a: Alert) -> None: received.append(a)
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    strategies = {"s1": _FakeStrategy()}
    mon = LiveMonitor(
        {"s1": handles}, [Cap()],
        clock=lambda: 1234,
        heartbeat_path=None,
        equity_provider=lambda: 100000.0,
        allocator=_FakeAllocator(),
        strategies_by_name=strategies,
    )
    await mon._refresh_allocations()
    await mon._refresh_allocations()
    info_alerts = [a for a in received if a.source == "alloc_refresh"
                   and a.severity == AlertSeverity.INFO]
    assert len(info_alerts) == 1  # 只第一次


@pytest.mark.asyncio
async def test_alloc_refresh_noop_when_provider_missing() -> None:
    """没配 equity_provider → 完全 no-op,不调 allocator。"""
    cfg = RiskConfig(enable_kelly=True)
    _, handles = build_risk_manager(cfg)
    alloc = _FakeAllocator()
    strategies = {"s1": _FakeStrategy()}
    mon = LiveMonitor(
        {"s1": handles}, [],
        clock=lambda: 1234,
        heartbeat_path=None,
        equity_provider=None,
        allocator=alloc,
        strategies_by_name=strategies,
    )
    await mon._refresh_allocations()
    assert alloc.calls == []
    assert strategies["s1"]._allocated_equity_usdt is None
