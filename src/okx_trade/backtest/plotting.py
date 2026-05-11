"""回测净值曲线绘制（plotly 交互 HTML）。

NT 的 ``Trader.generate_account_report(venue)`` 返回的是 *长格式* 账户事件
DataFrame——每个 ``AccountState`` 事件每个币种一行，含 ``total / free / locked``。
本模块把它 pivot 成单币种的净值时序，再画成 plotly 双面板 HTML：
上方净值线，下方相对历史峰值的回撤百分比。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from nautilus_trader.backtest.node import BacktestNode
    from nautilus_trader.model.identifiers import Venue


def extract_equity_curve(
    node: BacktestNode,
    venue: Venue,
    *,
    currency: str = "USDT",
) -> pd.DataFrame:
    """从跑完的 ``BacktestNode`` 抽出账户净值时序。

    Args:
        node: ``run_backtest_with_node`` 返回的 node。
        venue: 账户所在 venue（如 ``OKX_VENUE``）。
        currency: 取该币种的余额作为净值；默认 USDT。

    Returns:
        DatetimeIndex (UTC) 的 DataFrame，列 ``total / free / locked``（均为 float）。
        若账户事件为空，返回空 DataFrame。
    """
    engines = node.get_engines()
    if not engines:
        raise RuntimeError("BacktestNode has no engines — was .run() called?")
    trader = engines[0].trader
    raw = trader.generate_account_report(venue=venue)
    if raw.empty:
        return raw

    # 长格式 → 单币种过滤
    if "currency" in raw.columns:
        raw = raw[raw["currency"] == currency]
    if raw.empty:
        return raw

    out = raw[["total", "free", "locked"]].copy()
    for col in out.columns:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["total"])
    # 同一 ts 可能有多个事件（多次撮合）——保留最后一个
    out = out[~out.index.duplicated(keep="last")]
    return out


def plot_equity_curve_html(
    equity: pd.DataFrame,
    *,
    output_path: Path | str,
    title: str = "Equity Curve",
    starting_balance: float | None = None,
    subtitle: str | None = None,
) -> Path:
    """画 plotly 交互 HTML：上 = 账户净值，下 = drawdown %。

    Args:
        equity: ``extract_equity_curve`` 返回的 DataFrame（需要 ``total`` 列）。
        output_path: HTML 输出路径。
        title: 主标题。
        starting_balance: 用于计算总收益率；缺省时用首行 ``total`` 当基准。
        subtitle: 标题副行（如 "PnL +12.3% Sharpe 1.20 maxDD -8.5%"）。

    Returns:
        实际写出的 Path（已 ``resolve()``）。
    """
    if equity.empty or "total" not in equity.columns:
        raise ValueError("equity DataFrame 为空或缺少 'total' 列")

    from plotly.subplots import make_subplots
    import plotly.graph_objects as go

    total = equity["total"].astype(float)
    base = float(starting_balance) if starting_balance is not None else float(total.iloc[0])
    pnl_pct = (total / base - 1.0) * 100.0
    drawdown_pct = (total / total.cummax() - 1.0) * 100.0  # ≤ 0

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.7, 0.3], vertical_spacing=0.06,
        subplot_titles=("Equity (USDT)", "Drawdown (%)"),
    )

    fig.add_trace(
        go.Scatter(
            x=total.index, y=total.values, mode="lines", name="Equity",
            line={"color": "#1f77b4", "width": 1.5},
            customdata=pnl_pct.values.reshape(-1, 1),
            hovertemplate=(
                "<b>%{x|%Y-%m-%d %H:%M}</b><br>"
                "Equity: %{y:,.2f} USDT<br>"
                "PnL: %{customdata[0]:+.2f}%<extra></extra>"
            ),
        ),
        row=1, col=1,
    )
    fig.add_hline(y=base, line={"dash": "dot", "color": "#888"}, row=1, col=1)

    fig.add_trace(
        go.Scatter(
            x=drawdown_pct.index, y=drawdown_pct.values, mode="lines",
            name="Drawdown", fill="tozeroy",
            line={"color": "#d62728", "width": 1.0},
            hovertemplate="<b>%{x|%Y-%m-%d %H:%M}</b><br>DD: %{y:.2f}%<extra></extra>",
        ),
        row=2, col=1,
    )

    full_title = title if subtitle is None else f"{title}<br><sub>{subtitle}</sub>"
    fig.update_layout(
        title={"text": full_title, "x": 0.5, "xanchor": "center"},
        showlegend=False,
        height=720,
        template="plotly_white",
        hovermode="x unified",
        margin={"l": 60, "r": 30, "t": 80, "b": 50},
    )
    fig.update_yaxes(title_text="USDT", row=1, col=1)
    fig.update_yaxes(title_text="%", row=2, col=1)
    fig.update_xaxes(title_text="Time (UTC)", row=2, col=1)

    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(out), include_plotlyjs="cdn")
    return out


__all__ = [
    "extract_equity_curve",
    "plot_equity_curve_html",
]
