"""风控层（M3）。

四个独立的纯函数 / 状态机模块，由顶层 ``RiskManager`` 串联：

| 模块 | 职责 |
|---|---|
| ``vol_target`` | 按 N 日 realized vol 反推目标仓位（年化波动率目标） |
| ``kelly``      | 基于策略历史 win_rate / avg_R 算 Kelly fraction（× 0.25 保守倍率） |
| ``drawdown``   | 日 / 周 PnL 监控状态机；触发熝断自动暂停下单 |
| ``correlation``| 滚动 N 日策略 PnL 相关性矩阵；> 阈值的对降权 |

设计原则：
- **每个模块都是纯计算 / 纯状态机**：接受输入数据 → 返回 ``RiskCheckResult``，
  不依赖 NT 运行时；
- **顶层 ``RiskManager``** 把多个 check 串成 pipeline；下单前调
  ``RiskManager.check(intent) -> RiskCheckResult``，根据 result.action 决定放行 /
  缩仓 / 拒绝；
- **NT 集成方式**（M3.6）：策略基类 ``RiskAwareStrategy`` 在 ``submit_order`` 之前
  插入 ``risk_manager.check(...)`` 调用，避免侵入 NT RiskEngine 内部 API。

详见 ``RiskManager`` docstring 的端到端示例。
"""
from __future__ import annotations

from .base import RiskAction, RiskCheck, RiskCheckResult, RiskIntent, RiskManager
from .correlation import (
    CorrelationCheck,
    correlation_matrix,
    correlation_weight_adjustment,
)
from .drawdown import (
    AccountDrawdownCheck,
    AccountDrawdownTracker,
    DrawdownCheck,
    DrawdownState,
    DrawdownTracker,
)
from .integration import (
    RiskConfig,
    RiskHandles,
    apply_risk_manager,
    build_risk_manager,
)
from .kelly import KellyCheck, kelly_fraction, quarter_kelly_size
from .trade_rate import TradeRateCheck
from .vol_target import (
    VolTargetCheck,
    realized_vol_annualized,
    vol_target_position_size,
)

__all__ = [
    # 共用
    "RiskAction",
    "RiskCheck",
    "RiskCheckResult",
    "RiskIntent",
    "RiskManager",
    # vol target
    "VolTargetCheck",
    "realized_vol_annualized",
    "vol_target_position_size",
    # kelly
    "KellyCheck",
    "kelly_fraction",
    "quarter_kelly_size",
    # trade-rate circuit breaker
    "TradeRateCheck",
    # drawdown
    "AccountDrawdownCheck",
    "AccountDrawdownTracker",
    "DrawdownCheck",
    "DrawdownState",
    "DrawdownTracker",
    # correlation
    "CorrelationCheck",
    "correlation_matrix",
    "correlation_weight_adjustment",
    # integration（M3.6）
    "RiskConfig",
    "RiskHandles",
    "apply_risk_manager",
    "build_risk_manager",
]
