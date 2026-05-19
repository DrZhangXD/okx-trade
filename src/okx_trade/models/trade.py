"""下单与订单结果模型。"""
from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from ..enums import OrdType, PosSide, Side, TdMode, TriggerPxType
from .common import OKXModel


class AttachAlgoOrder(OKXModel):
    """市价单附加的止盈止损单。

    使用 ``-1`` 作为 ``ord_px`` 触发后市价平仓——这是 OKX 的特殊语义。
    """

    tp_trigger_px: Decimal | None = Field(default=None, alias="tpTriggerPx")
    tp_ord_px: str | None = Field(default=None, alias="tpOrdPx")  # "-1" = 市价
    tp_trigger_px_type: TriggerPxType | None = Field(default=None, alias="tpTriggerPxType")
    sl_trigger_px: Decimal | None = Field(default=None, alias="slTriggerPx")
    sl_ord_px: str | None = Field(default=None, alias="slOrdPx")
    sl_trigger_px_type: TriggerPxType | None = Field(default=None, alias="slTriggerPxType")


class OrderRequest(OKXModel):
    """``POST /api/v5/trade/order`` 请求体。

    构造时的字段名用 snake_case，序列化（``model_dump(by_alias=True, exclude_none=True)``）
    后变成 OKX 期望的 camelCase。
    """

    inst_id: str = Field(alias="instId")
    td_mode: TdMode = Field(alias="tdMode")
    side: Side
    ord_type: OrdType = Field(alias="ordType")
    sz: str                            # OKX 要求字符串；上层用 fmt_size 格式化
    pos_side: PosSide | None = Field(default=None, alias="posSide")
    px: str | None = None              # 限价；市价单不传
    ccy: str | None = None             # 保证金币种（cross + 单币种保证金时用）
    # SPOT MARKET BUY 时必须设 ``base_ccy``，否则 sz 被 OKX 解读为 quote 数量
    # （USDT）。我们传的是 base (BTC) 数量，不显式标会被当 USDT → sCode=51020。
    tgt_ccy: str | None = Field(default=None, alias="tgtCcy")
    cl_ord_id: str | None = Field(default=None, alias="clOrdId")
    tag: str | None = None
    reduce_only: bool | None = Field(default=None, alias="reduceOnly")
    attach_algo_ords: list[AttachAlgoOrder] | None = Field(default=None, alias="attachAlgoOrds")


class OrderResult(OKXModel):
    """OKX 下单响应（``data[0]``）。"""

    ord_id: str = Field(alias="ordId", default="")
    cl_ord_id: str = Field(alias="clOrdId", default="")
    tag: str = ""
    s_code: str = Field(alias="sCode", default="0")  # 单笔成功码（"0" 即成）
    s_msg: str = Field(alias="sMsg", default="")

    @property
    def ok(self) -> bool:
        return self.s_code == "0"


__all__ = [
    "AttachAlgoOrder",
    "OrderRequest",
    "OrderResult",
]
