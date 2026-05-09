"""每日报告：把当日 PnL / equity / 风控状态拍扁成 JSON。

调用：
    reporter = DailyReporter(tracker, output_dir="var/daily_reports")
    reporter.write_for_date("2026-05-08")  # 写出 var/daily_reports/2026-05-08.json

字段
----
- ``date``：UTC 日期
- ``per_strategy``：{strategy_id: {trade_count, pnl_usdt, win_rate, ending_equity}}
- ``totals``：{trade_count, pnl_usdt}

后台调度
--------
``run_loop()`` 是 asyncio 死循环，每天 UTC ``0:01:00`` 写一份"刚结束那天"的
报告（错峰一分钟避开整点 cron 高峰）。``scripts/live.py`` 在 ``--run`` 时通
过 ``loop.create_task(reporter.run_loop())`` 起来；外部 ``cancel()`` 退出。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..pnl import PnLTracker


@dataclass(frozen=True, slots=True)
class StrategyDailyReport:
    strategy_id: str
    trade_count: int
    pnl_usdt: float
    win_rate: float
    ending_equity_usdt: float


@dataclass(frozen=True, slots=True)
class DailyReport:
    date: str  # ``YYYY-MM-DD`` UTC
    per_strategy: list[StrategyDailyReport]
    totals_trade_count: int
    totals_pnl_usdt: float
    generated_at_ts_ms: int = field(default_factory=lambda: int(
        datetime.now(tz=timezone.utc).timestamp() * 1000,
    ))


class DailyReporter:
    def __init__(
        self,
        tracker: PnLTracker,
        *,
        output_dir: str | Path = "var/daily_reports",
    ) -> None:
        self.tracker = tracker
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def build_report(self, date_str: str) -> DailyReport:
        """构建给定 UTC 日期的报告。"""
        day_start = int(
            datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000,
        )
        day_end = day_start + 86_400_000

        per_strategy: list[StrategyDailyReport] = []
        total_count = 0
        total_pnl = 0.0

        for sid in self.tracker.list_strategies():
            trades = [
                t for t in self.tracker.get_trades(sid, since_ms=day_start)
                if t.closed_ts_ms < day_end
            ]
            wins = sum(1 for t in trades if t.pnl_usdt > 0)
            count = len(trades)
            pnl = float(sum(t.pnl_usdt for t in trades)) if trades else 0.0
            win_rate = wins / count if count else 0.0

            equities = self.tracker.get_equities(sid)
            ending = float(equities[-1].equity_usdt) if equities else 0.0

            per_strategy.append(StrategyDailyReport(
                strategy_id=sid,
                trade_count=count,
                pnl_usdt=pnl,
                win_rate=win_rate,
                ending_equity_usdt=ending,
            ))
            total_count += count
            total_pnl += pnl

        return DailyReport(
            date=date_str,
            per_strategy=per_strategy,
            totals_trade_count=total_count,
            totals_pnl_usdt=total_pnl,
        )

    def write_for_date(self, date_str: str) -> Path:
        """构建报告并写到 ``output_dir/{date}.json``，返回路径。"""
        report = self.build_report(date_str)
        out_path = self.output_dir / f"{date_str}.json"
        payload: dict[str, Any] = asdict(report)
        # 把 dataclass 嵌套也转成 dict
        payload["per_strategy"] = [asdict(s) for s in report.per_strategy]
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)
        return out_path

    def write_for_today(self) -> Path:
        return self.write_for_date(datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"))

    @staticmethod
    def _next_run_at(now: datetime) -> datetime:
        """下一次写报告时间：下一个 UTC 0:01:00。"""
        today_001 = now.replace(hour=0, minute=1, second=0, microsecond=0)
        if now < today_001:
            return today_001
        return (now + timedelta(days=1)).replace(
            hour=0, minute=1, second=0, microsecond=0,
        )

    async def run_loop(
        self,
        *,
        sleep_for: Callable[[float], Awaitable[None]] | None = None,
        now: Callable[[], datetime] | None = None,
        on_write: Callable[[Path], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        """死循环，每个 UTC 0:01 写"刚结束那天"的 daily report。

        ``scripts/live.py`` 在 ``--run`` 入口调 ``loop.create_task(reporter.run_loop())``；
        外部 ``task.cancel()`` 退出（CancelledError 由调用方处理）。

        Args:
            sleep_for: 注入的 sleep 协程（默认 asyncio.sleep），便于测试。
            now: 注入的"当前 UTC 时间"函数（默认 datetime.now(tz=utc)），便于测试。
            on_write: 每次成功写报告后回调，参数是写出文件路径（用于打日志 / 通知）。
            on_error: 写报告失败的回调（捕获 Exception 后调用，不抛出，避免拉崩 task）。
        """
        _sleep = sleep_for or asyncio.sleep
        _now = now or (lambda: datetime.now(tz=timezone.utc))

        while True:
            cur = _now()
            wait_s = (self._next_run_at(cur) - cur).total_seconds()
            await _sleep(wait_s)
            try:
                # 写"刚结束的那天"：触发时刻 - 1 小时确保落在前一个 UTC 日内
                yesterday = (_now() - timedelta(hours=1)).strftime("%Y-%m-%d")
                path = self.write_for_date(yesterday)
                if on_write is not None:
                    on_write(path)
            except Exception as exc:  # noqa: BLE001 - 监控循环不能因写失败崩
                if on_error is not None:
                    on_error(exc)


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"non-serializable: {type(o).__name__}")


__all__ = ["DailyReport", "DailyReporter", "StrategyDailyReport"]
