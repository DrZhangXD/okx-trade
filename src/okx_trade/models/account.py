"""账户与持仓模型。"""
from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from .common import OKXModel


class BalanceDetail(OKXModel):
    """单币种余额（``balance.details[]``）。"""

    ccy: str
    eq: Decimal = Decimal("0")            # 总权益（含未实现盈亏）
    cash_bal: Decimal = Field(alias="cashBal", default=Decimal("0"))
    iso_eq: Decimal = Field(alias="isoEq", default=Decimal("0"))
    avail_eq: Decimal = Field(alias="availEq", default=Decimal("0"))
    avail_bal: Decimal = Field(alias="availBal", default=Decimal("0"))
    frozen_bal: Decimal = Field(alias="frozenBal", default=Decimal("0"))
    upl: Decimal = Decimal("0")           # 未实现盈亏


class Balance(OKXModel):
    """账户余额（``GET /api/v5/account/balance``）。"""

    total_eq: Decimal = Field(alias="totalEq", default=Decimal("0"))
    iso_eq: Decimal = Field(alias="isoEq", default=Decimal("0"))
    adj_eq: Decimal = Field(alias="adjEq", default=Decimal("0"))
    details: list[BalanceDetail] = Field(default_factory=list)

    def get(self, ccy: str) -> BalanceDetail | None:
        """快捷取某币种的明细。"""
        for d in self.details:
            if d.ccy == ccy:
                return d
        return None


class Position(OKXModel):
    """持仓（``GET /api/v5/account/positions``）。"""

    inst_id: str = Field(alias="instId")
    inst_type: str = Field(alias="instType")
    mgn_mode: str = Field(alias="mgnMode")           # cross / isolated
    pos_side: str = Field(alias="posSide")           # long / short / net
    pos: Decimal                                      # 持仓张数（>0 多 <0 空 0 无仓）
    avg_px: Decimal = Field(alias="avgPx", default=Decimal("0"))
    upl: Decimal = Decimal("0")                       # 未实现盈亏
    upl_ratio: Decimal = Field(alias="uplRatio", default=Decimal("0"))
    lever: Decimal = Decimal("1")
    liq_px: Decimal = Field(alias="liqPx", default=Decimal("0"))
    margin: Decimal = Decimal("0")
    # OKX 持仓 ID（long_short 模式下同 inst 的 long/short 各自唯一；net 模式下 inst 唯一）。
    # 之前 ``OKXModel.extra="ignore"`` 把这个字段静默吞了，导致 adapter 构造
    # ``PositionStatusReport`` 时拿不到稳定 venue_position_id —— NT 1.226 reconcile
    # 阶段会 fallback 到 ``{instrument}-EXTERNAL`` 占位 ID，与 NT 内部 order-driven
    # 的 ``P-...`` 序列对不上，触发 "Incorrect position ID" / "Residual Position" WARN。
    pos_id: str = Field(alias="posId", default="")
    ts: int = 0

    @property
    def is_open(self) -> bool:
        return self.pos != 0


__all__ = ["Balance", "BalanceDetail", "Position"]
