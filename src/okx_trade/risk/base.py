"""风控共用骨架：``RiskIntent`` / ``RiskCheckResult`` / ``RiskCheck`` / ``RiskManager``。

设计原则
--------
- ``RiskIntent``：策略下单前向 RiskManager 提交的「意图」，包含 instrument / side /
  原始仓位 / 入场价 / 止损价等元数据。
- ``RiskCheck``：单一职责的检查器抽象（vol_target / kelly / drawdown / correlation
  各实现一个）；输入 intent + 上下文 → 返回 ``RiskCheckResult``。
- ``RiskCheckResult``：决策结果 enum + 调整后的仓位 + 拒绝原因。RiskManager 把多个
  check 的结果合并：任意一个 ``REJECT`` → 整体拒绝；多个 ``SCALE`` 取最严的。
- ``RiskManager``：linear pipeline，``check_all(intent) -> RiskCheckResult``。

为什么不直接接 NT RiskEngine？
---------------------------
NT RiskEngine 的 PreTradeRiskCheck API 在 1.225 仍未稳定（PreTradeRiskCheck 接口在
``execution.engine`` 里），且其语义偏向「拒绝 / 通过」二元，没有「自动缩仓」。
我们用应用层 RiskManager 包一层，由 ``RiskAwareStrategy`` 在 ``submit_order`` 前
显式调用，行为可控且回测/实盘一致。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

Direction = Literal["long", "short"]


class RiskAction(str, Enum):
    """单个 RiskCheck 的输出动作。"""

    APPROVE = "approve"   # 通过，按 intent 原仓位下单
    SCALE = "scale"       # 通过但缩仓，``RiskCheckResult.scaled_size`` 是新仓位
    REJECT = "reject"     # 拒绝下单


@dataclass(frozen=True, slots=True)
class RiskIntent:
    """策略下单前的意图。

    Attributes:
        strategy_id: 策略实例 id（用于按策略追踪 PnL / 相关性）。
        instrument_id: 标的 id（如 ``"BTC-USDT-SWAP.OKX"``）。
        direction: ``"long"`` / ``"short"``。
        size: 策略原始计算出的仓位大小（合约张数 / 现货数量）。
        entry_price / stop_price: 入场价 / 止损价（vol_target 等需要）。
        account_equity_usdt: 当前账户净值，用于 vol_target 反推。
        meta: 自由扩展字段（未来策略需要传额外信息时使用）。
    """

    strategy_id: str
    instrument_id: str
    direction: Direction
    size: float
    entry_price: float
    stop_price: float
    account_equity_usdt: float
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RiskCheckResult:
    """单个 RiskCheck 或 RiskManager 整体的决策。

    Attributes:
        action: APPROVE / SCALE / REJECT。
        scaled_size: 当 action=SCALE 时的新仓位（floating contracts）；其他情况
            等于 intent.size。
        reason: 人类可读的解释（用于日志 / 告警）。
        check_name: 触发此结果的 check 名（"vol_target" / "drawdown" 等）。多个
            check 合并时，REJECT 优先 → 取第一个 REJECT 的；全 APPROVE/SCALE 则
            取最小 scaled_size。
    """

    action: RiskAction
    scaled_size: float
    reason: str
    check_name: str = "unknown"

    @classmethod
    def approve(cls, size: float, *, check_name: str = "unknown") -> RiskCheckResult:
        return cls(action=RiskAction.APPROVE, scaled_size=size,
                   reason="approved", check_name=check_name)

    @classmethod
    def scale(cls, new_size: float, reason: str, *, check_name: str) -> RiskCheckResult:
        return cls(action=RiskAction.SCALE, scaled_size=new_size,
                   reason=reason, check_name=check_name)

    @classmethod
    def reject(cls, reason: str, *, check_name: str) -> RiskCheckResult:
        return cls(action=RiskAction.REJECT, scaled_size=0.0,
                   reason=reason, check_name=check_name)


class RiskCheck(ABC):
    """所有风控检查器的抽象基类。"""

    name: str = "abstract"

    @abstractmethod
    def check(self, intent: RiskIntent) -> RiskCheckResult:
        """同步评估 intent 并返回决策。

        必须是同步方法（在 NT bar 回调里调用，asyncio 不友好）。如果 check
        需要异步数据，先在外部提前喂给 check 的状态。
        """
        raise NotImplementedError


class RiskManager:
    """串联多个 ``RiskCheck`` 的顶层管理器。

    用法::

        manager = RiskManager(checks=[
            VolTargetCheck(target_vol=0.15),
            KellyCheck(win_rate=0.55, avg_r=1.5),
            DrawdownCheck(tracker=tracker),
        ])
        result = manager.check_all(intent)
        if result.action == RiskAction.APPROVE:
            strategy.submit_order(size=result.scaled_size, ...)
        elif result.action == RiskAction.SCALE:
            strategy.submit_order(size=result.scaled_size, ...)
            log.info(f"scaled by {result.check_name}: {result.reason}")
        else:
            log.warning(f"REJECTED by {result.check_name}: {result.reason}")
    """

    def __init__(self, checks: list[RiskCheck]) -> None:
        self.checks = checks

    def check_all(self, intent: RiskIntent) -> RiskCheckResult:
        """按顺序跑所有 check，合并结果。

        合并规则：
        - 任意一个 ``REJECT`` → 整体 REJECT（取第一个）；
        - 否则取所有 SCALE 中最小的 scaled_size（最严格的）；
        - 全 APPROVE → 整体 APPROVE。
        """
        if not self.checks:
            return RiskCheckResult.approve(intent.size, check_name="empty_pipeline")

        results: list[RiskCheckResult] = []
        for check in self.checks:
            res = check.check(intent)
            if res.action == RiskAction.REJECT:
                # 早退
                return res
            results.append(res)

        # 取最严格 SCALE
        min_size = intent.size
        culprit: RiskCheckResult | None = None
        for r in results:
            if r.scaled_size < min_size:
                min_size = r.scaled_size
                culprit = r

        if culprit is None or min_size >= intent.size:
            return RiskCheckResult.approve(intent.size, check_name="all_passed")
        return RiskCheckResult.scale(
            min_size, reason=f"min size from {culprit.check_name}: {culprit.reason}",
            check_name=culprit.check_name,
        )


__all__ = [
    "Direction",
    "RiskAction",
    "RiskCheck",
    "RiskCheckResult",
    "RiskIntent",
    "RiskManager",
]
