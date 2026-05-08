"""每日报告：把当日 PnL / equity / 风控状态拍扁成 JSON。

调用：
    reporter = DailyReporter(tracker, output_dir="var/daily_reports")
    reporter.write_for_date("2026-05-08")  # 写出 var/daily_reports/2026-05-08.json

字段
----
- ``date``：UTC 日期
- ``per_strategy``：{strategy_id: {trade_count, pnl_usdt, win_rate, ending_equity}}
- ``totals``：{trade_count, pnl_usdt}
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"non-serializable: {type(o).__name__}")


__all__ = ["DailyReport", "DailyReporter", "StrategyDailyReport"]
