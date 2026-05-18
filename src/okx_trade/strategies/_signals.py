"""跨策略共享的纯信号函数。

设计原则：无 IO、无 NT 依赖、可独立单测。

- ``microprice`` / ``book_imbalance`` / ``ob_signal``：``ob_imbalance`` 策略用
- ``annualized_basis`` / ``basis_decision``：``basis_arb`` 策略用
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Sequence


# ---------------------------------------------------------------------------
# Order book signals (used by ob_imbalance strategy)
# ---------------------------------------------------------------------------


def microprice(
    bid_px: float,
    bid_qty: float,
    ask_px: float,
    ask_qty: float,
) -> float:
    """Stoikov microprice = (bid_px × ask_qty + ask_px × bid_qty) / (bid_qty + ask_qty).

    含义：用对侧数量给本侧价格加权，深度大的一侧把 microprice 推向自己。
    bid 重 → microprice 上浮；ask 重 → 下沉。

    回退：
    - 两侧数量都为 0：取 ``(bid + ask) / 2``；
    - bid/ask 价为 0 且无数量：返回 0.0。
    """
    total = bid_qty + ask_qty
    if total <= 0:
        if bid_px > 0 and ask_px > 0:
            return (bid_px + ask_px) / 2.0
        return 0.0
    return (bid_px * ask_qty + ask_px * bid_qty) / total


def book_imbalance(
    bid_qtys: Sequence[float],
    ask_qtys: Sequence[float],
    depth: int = 5,
) -> float:
    """订单簿不平衡度 ∈ [-1, +1]。

    formula::

        imb = (Σ bid_qtys[:depth] - Σ ask_qtys[:depth])
              / (Σ bid_qtys[:depth] + Σ ask_qtys[:depth])

    正值 → 买盘强；负值 → 卖盘强；0 → 均衡或两侧空。
    """
    bid_sum = sum(bid_qtys[:depth])
    ask_sum = sum(ask_qtys[:depth])
    total = bid_sum + ask_sum
    if total <= 0:
        return 0.0
    return (bid_sum - ask_sum) / total


def ob_signal(
    imbalance: float,
    mid_px: float,
    micro_px: float,
    *,
    imbalance_threshold: float = 0.35,
    microprice_premium_bps: float = 3.0,
) -> Literal["long", "short"] | None:
    """订单流入场信号。

    需要同时满足（and-gate）：
    1. ``|imbalance| > imbalance_threshold``
    2. microprice 偏离 mid 的 bp 量级与 imbalance 同号且 ≥ ``microprice_premium_bps``

    两条件做交叉过滤可以剔除假信号——单纯 imbalance 大但 microprice 没动通常是
    一侧 spoof 挂单；microprice 已经偏离才说明真实交易意图。
    """
    if mid_px <= 0:
        return None
    premium_bps = (micro_px - mid_px) / mid_px * 10000.0
    if imbalance > imbalance_threshold and premium_bps >= microprice_premium_bps:
        return "long"
    if imbalance < -imbalance_threshold and premium_bps <= -microprice_premium_bps:
        return "short"
    return None


# ---------------------------------------------------------------------------
# Basis arbitrage signals (used by basis_arb strategy)
# ---------------------------------------------------------------------------


def annualized_basis(
    futures_px: float,
    spot_px: float,
    days_to_expiry: float,
) -> float:
    """计算交割合约 vs 现货的年化 basis。

    formula::

        ann_basis = ((F - S) / S) × (365 / days)

    正值 = contango（期货溢价 → 做 spot long + futures short 锁定 carry）；
    负值 = backwardation。

    边界：``spot_px ≤ 0`` 或 ``days_to_expiry ≤ 0`` 返回 0.0（参数错或已到期）。
    """
    if spot_px <= 0 or days_to_expiry <= 0:
        return 0.0
    return ((futures_px - spot_px) / spot_px) * (365.0 / days_to_expiry)


class BasisAction(str, Enum):
    HOLD = "hold"
    ENTER = "enter"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class BasisArbParams:
    """basis_arb 决策参数。

    Attributes:
        entry_basis_apr: 年化 basis ≥ 此阈值才入场（默认 12% APR）。
        exit_basis_apr: 跌破此阈值平仓（默认 2% APR）。
        force_close_days_before_expiry: 距到期 < 此天数强制平仓，防结算日强平。
    """

    entry_basis_apr: float = 0.12
    exit_basis_apr: float = 0.02
    force_close_days_before_expiry: int = 2


def basis_decision(
    ann_basis: float,
    days_to_expiry: float,
    has_position: bool,
    params: BasisArbParams,
) -> BasisAction:
    """basis_arb 状态机决策。

    优先级：
    1. 已持仓 + 临近到期 → EXIT（防结算）
    2. 已持仓 + basis 跌破 exit → EXIT
    3. 空仓 + basis ≥ entry → ENTER
    4. 其余 → HOLD
    """
    if has_position:
        if days_to_expiry < params.force_close_days_before_expiry:
            return BasisAction.EXIT
        if ann_basis <= params.exit_basis_apr:
            return BasisAction.EXIT
        return BasisAction.HOLD
    if ann_basis >= params.entry_basis_apr:
        return BasisAction.ENTER
    return BasisAction.HOLD


__all__ = [
    "BasisAction",
    "BasisArbParams",
    "annualized_basis",
    "basis_decision",
    "book_imbalance",
    "microprice",
    "ob_signal",
]
