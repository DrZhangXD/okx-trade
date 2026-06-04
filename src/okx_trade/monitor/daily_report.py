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
``run_loop()`` 是 asyncio 死循环，每天 UTC ``0:10:00`` 写一份"刚结束那天"的
报告（让位 ``reconcile_pnl_from_okx.py`` 的 0:05 cron，保证 trades_okx 在
出报告前已是新一天的权威数据）。``scripts/live.py`` 在 ``--run`` 时通过
``loop.create_task(reporter.run_loop())`` 起来；外部 ``cancel()`` 退出。
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..pnl import PnLTracker

# NT-instance-name → snake_case canonical strategy id（与 trades_okx 一致；
# reconcile_pnl_from_okx.py 写 bills 时用 snake_case bare name）。
# 保持与 PnLTracker._fetch_trades_okx 内嵌 mapping 同步——下次重构合并到一处。
_NT_CLASS_TO_CANONICAL: dict[str, str] = {
    "obimbalance": "ob_imbalance",
    "fundingcarry": "funding_carry",
    "xsmomentum": "xs_momentum",
    "liqreversal": "liq_reversal",
    "basisarb": "basis_arb",
    "factorportfolio": "factor_portfolio",
    "fundingxs": "funding_cross_section",
    "fundingskew": "funding_skew_momentum",
    "statarb": "stat_arb_pairs",
    "optionvol": "option_vol_selling",
    "mlfusion": "ml_fusion",
}


def _canonical_class_id(strategy_id: str) -> str:
    """NT 实例 id（e.g. ``FundingCarryStrategy-001``）→ snake_case canonical
    （``funding_carry``）。

    无 ``Strategy`` 字样的 id（旧式 ``s1``/``s2``、回测 fixture）原样返回 —
    避免误改无关命名空间。
    """
    if "Strategy" not in strategy_id:
        return strategy_id
    bare = strategy_id.split("Strategy")[0].lower()
    return _NT_CLASS_TO_CANONICAL.get(bare, bare)


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
        """构建给定 UTC 日期的报告。

        Dedupe 语义（2026-05-27 修 attribution bug）
        ----------------------------------------------
        ``PnLTracker.list_strategies()`` 把 ``trades`` ∪ ``equities`` 表里
        所有曾出现过的 strategy_id UNION 返回，包含：

        - 当前 NT 进程的活实例（``FundingCarryStrategy-000``）；
        - 之前部署的死 NT 实例（``FundingCarryStrategy-001`` 等，trades 表残留）。

        而 ``_fetch_trades_okx`` 把 NT 实例名模糊匹配到 snake_case bare name
        （``funding_carry``），所以**同类的所有 NT 实例都拉到同一份 OKX bills**
        → 旧实现每个实例 emit 一行 → per_strategy 里出现 N 份完全相同的
        trade_count/pnl，totals 也被 N 倍夸大。

        修复：按 ``_canonical_class_id`` 把 list_strategies 的结果分桶 →
        每个 canonical class emit 一行；trade lookup 用 canonical id（直接命中
        trades_okx 的 snake_case 行）；ending_equity 只看当日 in-window 快照
        across 桶内所有 NT 实例 → 死实例的多天前 stale equity 不会污染。
        """
        day_start = int(
            datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000,
        )
        day_end = day_start + 86_400_000

        # 按 canonical class 分桶；每个桶保留组成它的 NT 实例 id 集合
        buckets: dict[str, list[str]] = defaultdict(list)
        for sid in self.tracker.list_strategies():
            buckets[_canonical_class_id(sid)].append(sid)
        # 也纳入只在 trades_okx 出现的策略——不写 equity 快照者(如 factor_portfolio)
        # 不在 list_strategies 里,否则会被整份报告漏掉、TOTAL 低估亏损(2026-06-04 修)。
        for sid in self.tracker.list_okx_strategies():
            canon = _canonical_class_id(sid)
            if sid not in buckets[canon]:
                buckets[canon].append(sid)

        per_strategy: list[StrategyDailyReport] = []
        total_count = 0
        total_pnl = 0.0

        for canonical, _ in sorted(buckets.items()):
            # 用 canonical（snake_case）直接查 — trades_okx 主路径精确命中，
            # 不再依赖 _fetch_trades_okx 内的模糊回退；同类 N 个 NT 实例不再
            # 拉到 N 倍重复行。
            trades = [
                t for t in self.tracker.get_trades(canonical, since_ms=day_start)
                if t.closed_ts_ms < day_end
            ]
            # pnl_usdt = 当日全部订单(开+平)的 net 求和 → 含开仓手续费这笔真实
            # 成本,所以保留 get_trades(每个订单一行)。
            pnl = float(sum(t.pnl_usdt for t in trades)) if trades else 0.0
            # trade_count / win_rate 只按已平仓回合算 —— 开仓单 pnl=0 不是一笔
            # 成交回合,算进去会把持仓过夜策略的 win_rate 结构性压到 0
            # (2026-05-29 修)。胜负按 net(pnl+fee)>0 判定。
            round_trips = [
                t for t in self.tracker.get_round_trips(canonical, since_ms=day_start)
                if t.closed_ts_ms < day_end
            ]
            count = len(round_trips)
            wins = sum(1 for t in round_trips if t.pnl_usdt > 0)
            win_rate = wins / count if count else 0.0

            ending = self._latest_equity_in_window(
                buckets[canonical], day_start, day_end,
            )

            per_strategy.append(StrategyDailyReport(
                strategy_id=canonical,
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

    def _latest_equity_in_window(
        self, sids: list[str], day_start: int, day_end: int,
    ) -> float:
        """桶内最新的 in-window equity 快照（across 所有 NT 实例 id）。

        - in-window = ``day_start <= ts_ms < day_end``；
        - 多个活实例共享同一账户（observe 同一份 totalEq），取**最新一笔**
          即代表当日 ending；
        - 死实例没 in-window 快照（``trades`` 表残留但 ``equities`` 无新写入）
          → 自动忽略，不污染。

        全员都没 in-window 快照 → 0.0（信号：这个 class 今日完全死了）。
        """
        latest_ts = -1
        latest_eq = 0.0
        for sid in sids:
            for snap in self.tracker.get_equities(sid, since_ms=day_start):
                if snap.ts_ms < day_end and snap.ts_ms > latest_ts:
                    latest_ts = snap.ts_ms
                    latest_eq = float(snap.equity_usdt)
        return latest_eq

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
        """下一次写报告时间：下一个 UTC 0:10:00。

        为什么 0:10 而不是 0:01？``scripts/reconcile_pnl_from_okx.py`` 在
        UTC 0:05 跑（cron），把 OKX bills 写入 ``trades_okx``。daily_report 必须
        等 reconcile 完成 → 否则报告里 trades_okx 还没今天的 bills，class 显示
        0 trades（2026-05-26 prod bug：StatArbStrategy-008 实有 5501 笔但
        报告 0 trades）。
        """
        today_010 = now.replace(hour=0, minute=10, second=0, microsecond=0)
        if now < today_010:
            return today_010
        return (now + timedelta(days=1)).replace(
            hour=0, minute=10, second=0, microsecond=0,
        )

    async def run_loop(
        self,
        *,
        sleep_for: Callable[[float], Awaitable[None]] | None = None,
        now: Callable[[], datetime] | None = None,
        on_write: Callable[[Path], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        """死循环，每个 UTC 0:10 写"刚结束那天"的 daily report。

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
