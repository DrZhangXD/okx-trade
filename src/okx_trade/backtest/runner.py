"""NT BacktestNode 包装：构造 venue/data/engine 配置，跑回测，输出指标摘要。

设计原则：
- **薄包装**：不重新发明轮子，直接转发到 ``BacktestNode.run()``；
- **OKX 默认值**：``build_okx_venue_config`` 把 OKX 模拟交易所设置（OmsType / 杠杆 /
  fill model）做成函数级默认；
- **指标摘要**：从 NT ``BacktestResult`` 拍扁出 PnL / Sharpe / max DD / 交易次数，
  方便 CLI 输出。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from ..adapter.constants import OKX_VENUE

if TYPE_CHECKING:
    from nautilus_trader.backtest.config import (
        BacktestDataConfig,
        BacktestEngineConfig,
        BacktestVenueConfig,
    )
    from nautilus_trader.backtest.node import BacktestNode
    from nautilus_trader.backtest.results import BacktestResult
    from nautilus_trader.config import ImportableStrategyConfig


@dataclass(frozen=True, slots=True)
class BacktestSummary:
    """简化版回测指标摘要（PnL/Sharpe/maxDD/交易次数）。

    NT ``BacktestResult.stats_pnls`` 形如 ``{"USDT": {"PnL (total)": ..., "Sharpe Ratio": ...}}``，
    本类把第一币种的关键字段拍平到 attribute，便于 CLI / 日志展示。
    """

    iterations: int             # 处理的事件数
    total_orders: int
    total_positions: int
    elapsed_seconds: float
    pnl_total: float            # 总 PnL（quote currency）
    pnl_pct: float              # 收益率（相对起始净值）
    sharpe_ratio: float
    max_drawdown_pct: float     # 最大回撤（百分比，负值）
    win_rate: float             # 胜率 [0, 1]
    raw: dict                   # 完整 stats_pnls + stats_returns，便于排错

    def __str__(self) -> str:
        return (
            f"events={self.iterations} orders={self.total_orders} positions={self.total_positions} "
            f"PnL={self.pnl_total:.2f} ({self.pnl_pct:+.2%}) "
            f"Sharpe={self.sharpe_ratio:.2f} maxDD={self.max_drawdown_pct:.2%} "
            f"win={self.win_rate:.1%} elapsed={self.elapsed_seconds:.1f}s"
        )


def build_okx_venue_config(
    *,
    starting_balance_usdt: float = 10000.0,
    leverage: int = 10,
    base_currency: str | None = None,
) -> BacktestVenueConfig:
    """OKX 模拟交易所标准配置（NETTING + MARGIN + 默认 leverage=10）。

    Args:
        starting_balance_usdt: 起始 USDT 余额，默认 10K（用户决策的资金体量下限）。
        leverage: 默认杠杆。
        base_currency: 账户基础币种；None → 多币种保证金（与 OKX cross 模式一致）。
    """
    from nautilus_trader.backtest.config import BacktestVenueConfig

    return BacktestVenueConfig(
        name=str(OKX_VENUE),
        oms_type="NETTING",          # 单向持仓（与 ExecClient 默认 net 模式一致）
        account_type="MARGIN",
        starting_balances=[f"{starting_balance_usdt} USDT"],
        base_currency=base_currency,
        default_leverage=Decimal(leverage),
        # 其余字段用 NT 默认（fill_model=PerfectFill / latency=0 等）
    )


def run_backtest(
    venue: BacktestVenueConfig,
    *,
    data: list[BacktestDataConfig],
    strategies: list[ImportableStrategyConfig],
    start: int | None = None,
    end: int | None = None,
    engine_config: BacktestEngineConfig | None = None,
) -> BacktestSummary:
    """跑单个回测，返回 ``BacktestSummary``。

    Args:
        venue: ``build_okx_venue_config`` 的返回。
        data: ``BacktestDataConfig`` 列表（每个 instrument + bar_type 一条）。
        strategies: ``ImportableStrategyConfig`` 列表（NT 用 import path 反射加载）。
        start / end: 回测时间范围（毫秒）。None → 用全部数据。
        engine_config: 自定义 engine 配置；默认仅设 trader_id + 关掉冗长 logging。
    """
    summary, _node = run_backtest_with_node(
        venue,
        data=data,
        strategies=strategies,
        start=start,
        end=end,
        engine_config=engine_config,
    )
    return summary


def run_backtest_with_node(
    venue: BacktestVenueConfig,
    *,
    data: list[BacktestDataConfig],
    strategies: list[ImportableStrategyConfig],
    start: int | None = None,
    end: int | None = None,
    engine_config: BacktestEngineConfig | None = None,
) -> tuple[BacktestSummary, BacktestNode]:
    """与 :func:`run_backtest` 同逻辑，但额外返回 ``BacktestNode``，

    用于跑完后从 ``node.get_engines()[0].trader.generate_account_report(venue)``
    抽出账户余额时间序列（净值曲线）。
    """
    from nautilus_trader.backtest.config import BacktestEngineConfig, BacktestRunConfig
    from nautilus_trader.backtest.node import BacktestNode

    if engine_config is None:
        engine_config = BacktestEngineConfig(
            trader_id="OKX-BT-001",
            strategies=strategies,
        )
    else:
        # msgspec Struct 不支持 mutate；构造新实例覆盖 strategies
        engine_config = engine_config.replace(strategies=strategies)  # type: ignore[attr-defined]

    run_config = BacktestRunConfig(
        venues=[venue],
        data=data,
        engine=engine_config,
        start=start,
        end=end,
        dispose_on_completion=False,  # 保留 engine，供事后抽 equity 曲线
    )

    node = BacktestNode(configs=[run_config])
    results: list[BacktestResult] = node.run()
    if not results:
        raise RuntimeError("BacktestNode.run() returned empty results")

    return _summarize(results[0]), node


def _summarize(result: BacktestResult) -> BacktestSummary:
    """从 NT BacktestResult 拍扁出 BacktestSummary。"""
    stats_pnls = result.stats_pnls or {}
    stats_returns = result.stats_returns or {}

    # stats_pnls 第一币种通常是账户基础货币（USDT）
    first_ccy_stats = next(iter(stats_pnls.values()), {}) if stats_pnls else {}

    pnl_total = float(first_ccy_stats.get("PnL (total)", 0.0))
    pnl_pct = float(first_ccy_stats.get("PnL% (total)", 0.0)) / 100.0 \
        if "PnL% (total)" in first_ccy_stats else 0.0
    win_rate = float(first_ccy_stats.get("Win Rate", 0.0))
    if win_rate > 1.0:
        win_rate /= 100.0  # NT 有时返回百分比，标准化到 [0, 1]

    sharpe = float(stats_returns.get("Sharpe Ratio (252 days)", 0.0)) \
        or float(stats_returns.get("Sharpe Ratio", 0.0))
    max_dd_pct = float(first_ccy_stats.get("Max Drawdown (%)", 0.0)) / 100.0
    if max_dd_pct > 0:
        max_dd_pct = -max_dd_pct  # 标准化为负数（回撤）

    return BacktestSummary(
        iterations=result.iterations,
        total_orders=result.total_orders,
        total_positions=result.total_positions,
        elapsed_seconds=result.elapsed_time,
        pnl_total=pnl_total,
        pnl_pct=pnl_pct,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_dd_pct,
        win_rate=win_rate,
        raw={"stats_pnls": stats_pnls, "stats_returns": stats_returns},
    )


__all__ = [
    "BacktestSummary",
    "build_okx_venue_config",
    "run_backtest",
    "run_backtest_with_node",
]
