"""策略库（NautilusTrader Strategy 的具体实现）。

每个策略一个 module。共用辅助（BarBuffer / Signal dataclass / position sizing）放 ``base.py``。
策略本身是 ``nautilus_trader.trading.strategy.Strategy`` 子类，回测和实盘共享同一份代码。

可用策略：

- ``FundingCarryStrategy``  (M2): spot+perp delta-neutral 资金费率套利
- ``XSMomentumStrategy``    (M4): 横截面动量（vol-managed），每日 UTC 0 点 rebalance
- ``LiqReversalStrategy``   (M4): 强平瀑布反转（订阅 ``liquidation-orders`` z-score）
- ``BasisArbStrategy``      (M5): 交割合约 vs 现货期现套利（spot long + futures short）
- ``OBImbalanceStrategy``   (M5): 订单流 microprice / book imbalance 微观结构反转

确认门控（不独立交易，作为 Strategy 的 helper）：

- ``confirmation.OFIFilter`` (M4): trades 频道 OFI 反向时降仓 50% / 跳过

已下线：

- ``RangeBreakoutStrategy``（M5.X 下线）：crypto 区间突破 alpha 弱，与 xs_momentum 高度同向但噪音
  更大；多次事故性 fix（commit 34666fc、a6a3c8b）说明实现层不稳定。
"""
from __future__ import annotations

from ._signals import (
    BasisAction,
    BasisArbParams,
    annualized_basis,
    basis_decision,
    book_imbalance,
    microprice,
    ob_signal,
)
from .base import BarBuffer, BarSnapshot, ScalpSignal, position_contracts
from .confirmation import OFIDecision, OFIFilter, TradeFlow, ofi_filter_decision, ofi_score
from .funding_carry import (
    CarryAction,
    FundingCarryConfig,
    FundingCarryParams,
    FundingCarryStrategy,
    carry_position_size,
    funding_carry_decision,
)
from .liq_reversal import (
    LiqEvent,
    LiqReversalConfig,
    LiqReversalStrategy,
    liq_reversal_decision,
    liq_zscore,
    parse_okx_liq_frame,
)
from .xs_momentum import (
    XSMomentumConfig,
    XSMomentumStrategy,
    cross_section_rank,
    momentum_score,
    rebalance_orders,
    vol_managed_weight,
)

__all__ = [
    # Base
    "BarBuffer",
    "BarSnapshot",
    "ScalpSignal",
    "position_contracts",
    # Shared pure signals (M5)
    "BasisAction",
    "BasisArbParams",
    "annualized_basis",
    "basis_decision",
    "book_imbalance",
    "microprice",
    "ob_signal",
    # Funding Carry (M2)
    "CarryAction",
    "FundingCarryConfig",
    "FundingCarryParams",
    "FundingCarryStrategy",
    "carry_position_size",
    "funding_carry_decision",
    # XS Momentum (M4)
    "XSMomentumConfig",
    "XSMomentumStrategy",
    "cross_section_rank",
    "momentum_score",
    "rebalance_orders",
    "vol_managed_weight",
    # Liq Reversal (M4)
    "LiqEvent",
    "LiqReversalConfig",
    "LiqReversalStrategy",
    "liq_reversal_decision",
    "liq_zscore",
    "parse_okx_liq_frame",
    # OFI confirmation (M4)
    "OFIDecision",
    "OFIFilter",
    "TradeFlow",
    "ofi_filter_decision",
    "ofi_score",
]
