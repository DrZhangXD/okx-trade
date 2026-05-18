"""通用数据模型：交易品种规格 / 公共枚举包装。"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..enums import InstType


class OKXModel(BaseModel):
    """所有 OKX 模型的基类——配置 alias / 接受额外字段（OKX 偶尔加新字段不应破坏旧客户端）。"""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        frozen=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_empty_strings(cls, data: object) -> object:
        """OKX 经常对"无值"字段返回 ``""``（典型如 demo 账户 ``adjEq`` / ``upl``）。
        Decimal 字段拿到空串会校验失败；这里在校验前把所有空串字段从输入里剔掉，
        让 pydantic 退回字段默认值。对字符串字段无副作用——已声明的 ``str`` 默认值
        本身就是 ``""``。"""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v != ""}
        return data


# 向后兼容旧引用
_OKXBase = OKXModel


class Instrument(OKXModel):
    """交易品种规格（``GET /api/v5/public/instruments`` 单条响应）。

    字段挑选自 OKX 文档，覆盖下单时必需的精度信息（tickSz/lotSz/minSz/ctVal）。
    """

    inst_id: str = Field(alias="instId")
    inst_type: InstType = Field(alias="instType")
    base_ccy: str = Field(default="", alias="baseCcy")
    quote_ccy: str = Field(default="", alias="quoteCcy")
    settle_ccy: str = Field(default="", alias="settleCcy")
    ct_val: Decimal = Field(default=Decimal("1"), alias="ctVal")
    ct_val_ccy: str = Field(default="", alias="ctValCcy")
    # OKX 在 ``preopen`` 状态的 futures 合约（category=1）会**省略** tickSz/lotSz/minSz
    # 字段。这里给默认 0，下游策略层只用 ``state == "live"`` 的 instrument，所以
    # 0 值不会进交易热路径；但解析层不能因此崩，否则 ``load_all_async`` 会全军覆没。
    tick_sz: Decimal = Field(default=Decimal("0"), alias="tickSz")
    lot_sz: Decimal = Field(default=Decimal("0"), alias="lotSz")
    min_sz: Decimal = Field(default=Decimal("0"), alias="minSz")
    state: str = Field(default="", alias="state")  # live / suspend / preopen / settlement
    # 交割合约（FUTURES）专属：到期/上市时间（毫秒），SPOT/SWAP 缺省为 0
    exp_time: int = Field(default=0, alias="expTime")
    list_time: int = Field(default=0, alias="listTime")


class FundingRate(OKXModel):
    """``GET /api/v5/public/funding-rate`` 单条响应。

    ``funding_rate`` 是 8h 周期费率（不是年化）；做策略时按 ``× 3 × 365`` 估算 APR。
    """

    inst_type: str = Field(default="SWAP", alias="instType")
    inst_id: str = Field(alias="instId")
    funding_rate: Decimal = Field(alias="fundingRate")
    next_funding_rate: Decimal = Field(default=Decimal("0"), alias="nextFundingRate")
    funding_time: int = Field(alias="fundingTime")        # 当期结算毫秒时间戳
    # funding-rate-history 端点不返回 nextFundingTime
    next_funding_time: int = Field(default=0, alias="nextFundingTime")

    @property
    def apr(self) -> Decimal:
        """估算年化（funding_rate × 3 × 365）。仅供策略阈值比较，非精确计算。"""
        return self.funding_rate * Decimal(3 * 365)


__all__ = ["FundingRate", "Instrument", "OKXModel"]
