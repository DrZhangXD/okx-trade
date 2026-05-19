"""``OKXLiveExecutionClient``：把 NT 订单命令翻译为 OKX 下单调用，回填订单事件。

下单路径（M1 用 REST；M2 切换 WS 以降低延迟）::

    NT Strategy.submit_order(MarketOrder)
        ↓
    LiveExecutionEngine 路由到 OKXLiveExecutionClient._submit_order(SubmitOrder)
        ↓
    构造 OrderRequest → client.trade.place_order(req)  # REST
        ↓
    sCode=="0" → generate_order_submitted(...)  → NT 引擎
    sCode!="0" → generate_order_rejected(reason=sMsg, ...)

成交回报路径（异步）::

    OKX `orders` 私有频道推送
        ↓
    本类启动时 ws.on("orders", _on_order_update)
        ↓
    解析 frame['data'][i]，按 state 路由：
        live          → generate_order_accepted(...)
        partially_filled / filled → generate_order_filled(...)
        canceled      → generate_order_canceled(...)

账户状态::

    OKX `account` 频道推送 → generate_account_state(...)

未实现（M2+）：bracket order / position reports / mass status / fill reports。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import (
    FillReport,
    OrderStatusReport,
    PositionStatusReport,
)
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OmsType,
    OrderStatus,
    OrderType,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.objects import (
    AccountBalance,
    Currency,
    Money,
    Price,
    Quantity,
)

from ..config import OKXSettings
from ..enums import InstType, PosSide, Side, TdMode
from ..models.account import Balance
from ..models.trade import OrderRequest
from ..rest.client import OKXRestClient
from ..utils.logging import get_logger
from ..ws.client import OKXWSClient
from .parsing import (
    from_instrument_id,
    okx_exec_type_to_liquidity,
    okx_ord_type_to_nt,
    okx_pos_to_position_side,
    okx_side_to_nt,
    okx_state_to_order_status,
    order_side_to_okx,
    order_type_and_tif_to_okx,
    to_instrument_id,
)

if TYPE_CHECKING:
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import LiveClock, MessageBus
    from nautilus_trader.execution.messages import (
        CancelAllOrders,
        CancelOrder,
        GenerateFillReports,
        GenerateOrderStatusReport,
        GenerateOrderStatusReports,
        GeneratePositionStatusReports,
        ModifyOrder,
        QueryOrder,
        SubmitOrder,
    )

    from .instrument_provider import OKXInstrumentProvider


log = get_logger(__name__)


def _ms_to_nanos(ms: int) -> int:
    return int(ms) * 1_000_000


def _build_account_balances(balance: Balance) -> list[AccountBalance]:
    """把 OKX :class:`Balance` 的 ``details[]`` 翻译成 NT ``AccountBalance``。

    NT 约束：``total = locked + free`` 且三个值都非负。OKX 的 ``eq`` 偶尔会因
    UPL 浮亏跌到 0 以下 / ``availBal`` 大于 ``eq``，这里全部夹回 [0, total] 避免
    构造时 ValueError。``eq`` 与 ``availBal`` 都为 0（也无 ``cashBal``）的条目直接
    丢弃，否则 NT Portfolio 视图会被几十条零余额币种刷屏。
    """
    out: list[AccountBalance] = []
    for d in balance.details:
        try:
            currency = Currency.from_str(d.ccy, strict=False)
        except Exception:
            continue
        eq_raw = d.eq if d.eq != 0 else d.cash_bal
        total = eq_raw if eq_raw > 0 else Decimal("0")
        avail = d.avail_bal if d.avail_bal > 0 else d.avail_eq
        free = avail if avail > 0 else Decimal("0")
        if total <= 0 and free <= 0:
            continue
        if free > total:
            total = free
        # 关键：``locked`` 必须从 quantize 后的 total/free 派生，不能用未量化的
        # Decimal 减法。NT ``AccountBalance`` 在 ``objects.pyx:1925`` 严格校验
        # ``total.raw_int - locked.raw_int == free.raw_int``；OKX 返回的 eq /
        # availBal 经常在 8 位小数处差 1 unit，直接用裸 Decimal 减法 + Money
        # 三次量化会让等式偏 1 unit，构造失败 → AccountState 永不 emit →
        # Portfolio 报 'no account registered' → 所有 fill 被丢。
        m_total = Money(total, currency)
        m_free = Money(free, currency)
        m_locked = Money(m_total.as_decimal() - m_free.as_decimal(), currency)
        out.append(AccountBalance(m_total, m_locked, m_free))
    return out


def resolve_td_mode(inst_id: str, default_mode: TdMode) -> TdMode:
    """按 instrument 类型自动选 trade mode。

    - **现货**（``BTC-USDT`` / ``ETH-USDT`` 等）→ ``cash``：OKX 现货账户必须用 cash，
      否则报 51005；
    - **永续 / 交割 / 期权** → ``default_mode``（一般 ``cross`` 或 ``isolated``）。

    判断启发式（按 OKX instId 命名规则）：
      - ``BTC-USDT-SWAP``        → SWAP（permanent suffix）
      - ``BTC-USDT-260626``      → FUTURES（3 段，末段全数字 YYMMDD）
      - ``BTC-USD-260626-100000-C`` → OPTION（5+ 段，末段 C/P）
      - ``BTC-USDT``             → SPOT

    M6+.X bug fix：原版只识别 -SWAP，FUTURES 被误判为 SPOT → td_mode=cash +
    posSide=None → OKX 51000 'Parameter posSide error'（2026-05-19 basis_arb
    入场被拒的根因）。

    模块级纯函数：便于单测，不依赖 LiveExecutionClient 实例。
    """
    if inst_id.endswith("-SWAP"):
        return default_mode
    if inst_id.endswith("-C") or inst_id.endswith("-P"):
        # OPTION
        return default_mode
    parts = inst_id.split("-")
    # FUTURES：3 段，末段全数字（YYMMDD）
    if len(parts) == 3 and parts[-1].isdigit() and len(parts[-1]) == 6:
        return default_mode
    # 其余按现货
    return TdMode.CASH


def resolve_pos_side(
    side: Side,
    *,
    reduce_only: bool,
    pos_side_mode: str,
    td_mode: TdMode,
) -> PosSide | None:
    """计算下单时要发给 OKX 的 ``posSide``。

    OKX hedged 模式（``long_short``）下，``posSide`` 必须和持仓方向匹配：

    - **开多**：``side=BUY``, ``posSide=LONG``
    - **平多**：``side=SELL``, ``posSide=LONG``  ← reduce-only 时方向不翻
    - **开空**：``side=SELL``, ``posSide=SHORT``
    - **平空**：``side=BUY``, ``posSide=SHORT``  ← reduce-only 时方向不翻

    即 reduce-only 单的 ``posSide`` 等于被减仓位的方向（和 side 反向），不是
    side 推出来的"开仓方向"。早期实现用过 ``posSide = LONG if BUY else SHORT``
    的简化，在保证金充裕时 OKX 会容错（reduceOnly=true 时按持仓 net 掉），
    但保证金紧张时 OKX 会先按 "open long" 校验，触发 sCode=51008 拒单。

    Net 模式与现货账户：``posSide`` 不传（OKX 要求 net mode 不带这个字段）。
    """
    if td_mode == TdMode.CASH:
        return None
    if pos_side_mode != "long_short":
        return None
    if reduce_only:
        # 平仓单：posSide 等于被减仓位方向（与 side 反向）
        return PosSide.SHORT if side == Side.BUY else PosSide.LONG
    # 开仓单：posSide 与 side 同向
    return PosSide.LONG if side == Side.BUY else PosSide.SHORT


class OKXLiveExecutionClient(LiveExecutionClient):
    """OKX 执行客户端。

    Args:
        settings: ``OKXSettings``，必须包含凭证（otherwise WS private 连接会失败）。
        td_mode: 全局 trade mode，默认 ``cross``；现货账户应改为 ``cash``。
        pos_side_mode: ``long_short``（OKX 双向持仓）或 ``net``（单向）。
            决定下单时 ``posSide`` 字段怎么填。
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id,
        venue,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: OKXInstrumentProvider,
        settings: OKXSettings,
        *,
        td_mode: TdMode = TdMode.CROSS,
        pos_side_mode: str = "net",
        config=None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=venue,
            oms_type=OmsType.NETTING if pos_side_mode == "net" else OmsType.HEDGING,
            account_type=AccountType.MARGIN,
            base_currency=None,  # OKX 是多币种保证金，不固定 base
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._settings = settings
        self._td_mode = td_mode
        self._pos_side_mode = pos_side_mode
        self._rest: OKXRestClient | None = None
        self._ws: OKXWSClient | None = None
        # 下单时 NT ClientOrderId → 我们传给 OKX 的 clOrdId（OKX 要求字母数字 ≤32 字符）
        # 大多数情况下两者可以相同；策略要么自定义 ClientOrderId、要么 NT 自动生成 UUID
        # OKX 不接受 UUID 里的 `-`，所以这里做剥离

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        if not self._settings.has_credentials():
            raise RuntimeError(
                "OKXLiveExecutionClient requires API credentials in OKXSettings"
            )

        # NT 要求 ``account_id.get_issuer() == client_id``。OKX 一个 UID 一个账户，
        # 所以这里固定 ``OKX-master``——多账户场景将来按 UID 区分时再调整。
        # 必须在 ``generate_account_state`` 之前设：``AccountState`` 用 self.account_id 构造，
        # 没设的话 NT 会抛 ``account_id`` not None 校验。
        self._set_account_id(AccountId(f"{self.id.value}-master"))

        self._rest = OKXRestClient(self._settings)
        await self._rest.__aenter__()
        self._ws = OKXWSClient(self._settings)

        # 订阅 orders 私有频道（接收成交回报）
        async def on_order_update(frame: dict[str, Any]) -> None:
            for data in frame.get("data", []):
                self._handle_order_update(data)

        async def on_account_update(frame: dict[str, Any]) -> None:
            for data in frame.get("data", []):
                self._handle_account_update(data)

        self._ws.on("orders", on_order_update, instType="ANY")
        self._ws.on("account", on_account_update)
        await self._ws.activate("orders", instType="ANY")
        await self._ws.activate("account")

        # 拉一次账户快照并 emit AccountState，否则 NT Portfolio 在第一笔
        # account WS push 到达之前查 venue 账户会拿到 None，导致
        # `Cannot get account: no account registered for venue=...` 报错。
        try:
            balance = await self._rest.account.get_balance()
            self._emit_account_state(balance, ts_event=self._clock.timestamp_ns())
        except Exception as exc:
            log.warning(
                "okx.exec_client.initial_account_snapshot_failed",
                extra={"err": str(exc)},
            )

        log.info("okx.exec_client.connected")

    async def _disconnect(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._rest is not None:
            await self._rest.__aexit__(None, None, None)
            self._rest = None
        log.info("okx.exec_client.disconnected")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_cl_ord_id(client_order_id: ClientOrderId) -> str:
        """OKX clOrdId 要求字母数字（不含 ``-``），最长 32 字符。

        NT ``ClientOrderId`` 通常是 UUID4 或 strategy 自定义字符串，需要剥离 ``-``。
        """
        return client_order_id.value.replace("-", "")[:32]

    def _build_order_request(self, command: SubmitOrder) -> OrderRequest:
        order = command.order
        inst_id = from_instrument_id(order.instrument_id)
        side = order_side_to_okx(order.side)
        ord_type = order_type_and_tif_to_okx(order.order_type, order.time_in_force)
        td_mode = resolve_td_mode(inst_id, self._td_mode)

        # 提交前 min-lot 防线：低于 instrument.min_quantity 直接 raise，避免浪费
        # OKX 配额（每条都会被 sCode=51020 拒）+ 污染 monitor reject_per_hour 计数。
        instrument = self._instrument_provider.find(order.instrument_id)
        if instrument is not None and instrument.min_quantity is not None:
            if order.quantity < instrument.min_quantity:
                raise ValueError(
                    f"order qty {order.quantity} below min_quantity "
                    f"{instrument.min_quantity} for {inst_id}"
                )

        # 数量：从 NT Quantity 转字符串（已经按 size_precision 量化好了）
        sz = str(order.quantity)
        # 价格：限价单才有 price；市价单 None
        px = str(order.price) if order.has_price else None

        # reduceOnly：现货不支持（OKX 会报参数错）
        is_reduce_only = bool(getattr(order, "is_reduce_only", False))
        reduce_only_val: bool | None = None
        if td_mode != TdMode.CASH:
            reduce_only_val = is_reduce_only or None

        # posSide：hedged 模式下，开 / 平仓的 posSide 不同（见 resolve_pos_side docstring）
        pos_side = resolve_pos_side(
            side,
            reduce_only=is_reduce_only,
            pos_side_mode=self._pos_side_mode,
            td_mode=td_mode,
        )

        return OrderRequest(
            inst_id=inst_id,
            td_mode=td_mode,
            side=side,
            ord_type=ord_type,
            sz=sz,
            pos_side=pos_side,
            px=px,
            cl_ord_id=self._sanitize_cl_ord_id(order.client_order_id),
            reduce_only=reduce_only_val,
        )

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    async def _submit_order(self, command: SubmitOrder) -> None:
        if self._rest is None:
            raise RuntimeError("OKX REST not connected")
        order = command.order
        ts_now = self._clock.timestamp_ns()

        # 立即先 generate_order_submitted（让 NT 进入 SUBMITTED 状态）
        self.generate_order_submitted(
            strategy_id=command.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=ts_now,
        )

        try:
            req = self._build_order_request(command)
            result = await self._rest.trade.place_order(req)
        except Exception as exc:
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=str(exc),
                ts_event=self._clock.timestamp_ns(),
            )
            return

        if not result.ok:
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=f"OKX sCode={result.s_code}: {result.s_msg}",
                ts_event=self._clock.timestamp_ns(),
            )
            return

        # 下单成功 → 立刻基于 REST 同步响应 emit OrderAccepted。不能等 WS
        # `orders` 频道推送 state="live"：OKX 偶发 30-60s 才推，期间 NT 的
        # in-flight resolution（execution_engine.py:778）会强制把 SUBMITTED
        # 状态超时的订单标记为 OrderRejected(reason="UNKNOWN")，导致 NT 内
        # 部状态与 OKX 实际状态分裂。WS handler 那边带 OrderStatus 守卫
        # （见 ``_handle_order_update``），不会重复 emit。
        if result.ord_id:
            self.generate_order_accepted(
                strategy_id=command.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=VenueOrderId(result.ord_id),
                ts_event=self._clock.timestamp_ns(),
            )

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    async def _cancel_order(self, command: CancelOrder) -> None:
        if self._rest is None:
            raise RuntimeError("OKX REST not connected")
        inst_id = from_instrument_id(command.instrument_id)
        ord_id = command.venue_order_id.value if command.venue_order_id else None
        cl_ord_id = self._sanitize_cl_ord_id(command.client_order_id) if not ord_id else None
        try:
            await self._rest.trade.cancel_order(inst_id, ord_id=ord_id, cl_ord_id=cl_ord_id)
        except Exception as exc:
            log.warning(
                "okx.exec_client.cancel_failed",
                extra={"inst_id": inst_id, "err": str(exc)},
            )
            # NT 没有 generate_order_cancel_failed，但有 cancel_rejected
            self.generate_order_cancel_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.client_order_id,
                venue_order_id=command.venue_order_id,
                reason=str(exc),
                ts_event=self._clock.timestamp_ns(),
            )

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        # OKX 没有「批量按 instrument 撤单」端点；实现是先 query 再逐个撤
        # M1 暂留 no-op，M2 补
        log.warning(
            "okx.exec_client.cancel_all_not_implemented",
            extra={"instrument_id": str(command.instrument_id)},
        )

    # ------------------------------------------------------------------
    # Modify
    # ------------------------------------------------------------------

    async def _modify_order(self, command: ModifyOrder) -> None:
        if self._rest is None:
            raise RuntimeError("OKX REST not connected")
        inst_id = from_instrument_id(command.instrument_id)
        ord_id = command.venue_order_id.value if command.venue_order_id else None
        cl_ord_id = self._sanitize_cl_ord_id(command.client_order_id) if not ord_id else None
        try:
            await self._rest.trade.amend_order(
                inst_id,
                ord_id=ord_id,
                cl_ord_id=cl_ord_id,
                new_sz=str(command.quantity) if command.quantity else None,
                new_px=str(command.price) if command.price else None,
            )
        except Exception as exc:
            self.generate_order_modify_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.client_order_id,
                venue_order_id=command.venue_order_id,
                reason=str(exc),
                ts_event=self._clock.timestamp_ns(),
            )

    # ------------------------------------------------------------------
    # Query (M2)
    # ------------------------------------------------------------------

    async def _query_order(self, command: QueryOrder) -> None:
        log.warning("okx.exec_client.query_order_not_implemented")

    # ------------------------------------------------------------------
    # Reconciliation reports（NT 启动 / 周期对账时调用）
    # ------------------------------------------------------------------

    @staticmethod
    def _ms_or_none(value: object) -> int | None:
        """OKX dict 字段→毫秒整数；空串/None/0 一律视作没有。"""
        if value in (None, "", "0"):
            return None
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _datetime_to_ms(self, dt: Any) -> int | None:
        """``GenerateOrderStatusReports.start`` 是 pandas Timestamp（带 UTC tz）。
        OKX 要毫秒 int。``None`` 透传。"""
        if dt is None:
            return None
        # pandas.Timestamp / datetime 都支持 ``timestamp()``
        try:
            return int(dt.timestamp() * 1000)
        except Exception:
            return None

    def _build_order_status_report(
        self,
        data: dict[str, Any],
        ts_init: int,
    ) -> OrderStatusReport | None:
        """OKX 订单 dict（来自 orders-pending / orders-history-archive / order）→
        NT :class:`OrderStatusReport`。

        instrument 不在 cache 时返回 ``None``——precision 取不到没法构造 Quantity；
        provider 已加载齐就不会发生（reconciliation 在 NT 启动后才跑）。
        """
        inst_id_str = data.get("instId") or ""
        ord_id_str = data.get("ordId") or ""
        if not inst_id_str or not ord_id_str:
            return None

        instrument_id = to_instrument_id(inst_id_str)
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            log.debug(
                "okx.exec_client.report_skipped_no_instrument",
                extra={"instId": inst_id_str},
            )
            return None

        try:
            order_status = okx_state_to_order_status(data.get("state", ""))
            order_type, tif, post_only = okx_ord_type_to_nt(data.get("ordType", ""))
        except ValueError as exc:
            log.warning(
                "okx.exec_client.report_skipped_unsupported",
                extra={"instId": inst_id_str, "err": str(exc)},
            )
            return None

        side = okx_side_to_nt(data.get("side", "buy"))
        # OKX sz / accFillSz 都是字符串。空串当 0 处理。
        sz_str = data.get("sz") or "0"
        filled_str = data.get("accFillSz") or "0"
        try:
            quantity = Quantity(float(sz_str), precision=instrument.size_precision)
            filled_qty = Quantity(float(filled_str), precision=instrument.size_precision)
        except Exception as exc:
            log.warning(
                "okx.exec_client.report_skipped_qty_parse",
                extra={"instId": inst_id_str, "sz": sz_str, "filled": filled_str, "err": str(exc)},
            )
            return None
        if quantity == 0:
            # OKX 偶尔返回 sz=0 的占位（如算法子单未触发）；NT 要求 quantity>0。
            return None

        # 价格字段：``px`` 为限价；市价单为空。``avgPx`` 是成交均价。
        px_str = data.get("px") or ""
        price = Price(float(px_str), precision=instrument.price_precision) if px_str else None
        avg_px_str = data.get("avgPx") or ""
        avg_px = Decimal(avg_px_str) if avg_px_str else None

        # 时间戳：cTime = 创建（accepted）；uTime = 最后更新。空串/0 兜底到 ts_init。
        c_time_ms = self._ms_or_none(data.get("cTime")) or 0
        u_time_ms = self._ms_or_none(data.get("uTime")) or c_time_ms
        ts_accepted = _ms_to_nanos(c_time_ms) if c_time_ms else ts_init
        ts_last = _ms_to_nanos(u_time_ms) if u_time_ms else ts_init

        # OKX clOrdId 可能为空（手动下的单或外部）。reduceOnly 是 "true"/"false" 字符串。
        cl_ord_id_str = data.get("clOrdId") or ""
        client_order_id = ClientOrderId(cl_ord_id_str) if cl_ord_id_str else None
        reduce_only = str(data.get("reduceOnly", "")).lower() == "true"

        return OrderStatusReport(
            account_id=self.account_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=VenueOrderId(ord_id_str),
            order_side=side,
            order_type=order_type,
            time_in_force=tif,
            order_status=order_status,
            quantity=quantity,
            filled_qty=filled_qty,
            price=price,
            avg_px=avg_px,
            post_only=post_only,
            reduce_only=reduce_only,
            report_id=UUID4(),
            ts_accepted=ts_accepted,
            ts_last=ts_last,
            ts_init=ts_init,
        )

    def _build_fill_report(
        self,
        data: dict[str, Any],
        ts_init: int,
    ) -> FillReport | None:
        """OKX fill dict（``/trade/fills``）→ NT :class:`FillReport`。"""
        inst_id_str = data.get("instId") or ""
        ord_id_str = data.get("ordId") or ""
        if not inst_id_str or not ord_id_str:
            return None

        instrument_id = to_instrument_id(inst_id_str)
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            return None

        try:
            last_qty = Quantity(float(data.get("fillSz") or "0"), precision=instrument.size_precision)
            last_px = Price(float(data.get("fillPx") or "0"), precision=instrument.price_precision)
        except Exception as exc:
            log.warning(
                "okx.exec_client.fill_report_skipped",
                extra={"instId": inst_id_str, "err": str(exc)},
            )
            return None
        if last_qty == 0:
            return None

        # OKX fee 通常为负（被收取）。NT Money 不接受负数：取绝对值。
        try:
            fee = Decimal(data.get("fee") or "0")
        except Exception:
            fee = Decimal("0")
        fee_ccy = data.get("feeCcy") or "USDT"
        try:
            commission = Money(abs(fee), Currency.from_str(fee_ccy, strict=False))
        except Exception:
            commission = Money(Decimal("0"), Currency.from_str("USDT", strict=False))

        ts_event_ms = self._ms_or_none(data.get("ts")) or 0
        ts_event = _ms_to_nanos(ts_event_ms) if ts_event_ms else ts_init

        cl_ord_id_str = data.get("clOrdId") or ""
        trade_id_str = data.get("tradeId") or data.get("billId") or ""
        if not trade_id_str:
            # 没 tradeId 的 fill 是异常数据；丢弃。
            return None

        return FillReport(
            account_id=self.account_id,
            instrument_id=instrument_id,
            client_order_id=ClientOrderId(cl_ord_id_str) if cl_ord_id_str else None,
            venue_order_id=VenueOrderId(ord_id_str),
            trade_id=TradeId(str(trade_id_str)),
            order_side=okx_side_to_nt(data.get("side", "buy")),
            last_qty=last_qty,
            last_px=last_px,
            commission=commission,
            liquidity_side=okx_exec_type_to_liquidity(data.get("execType", "")),
            report_id=UUID4(),
            ts_event=ts_event,
            ts_init=ts_init,
        )

    def _build_position_status_report(
        self,
        data: dict[str, Any] | Any,
        ts_init: int,
    ) -> PositionStatusReport | None:
        """OKX 持仓行（``Position`` 模型或 dict）→ NT :class:`PositionStatusReport`。"""
        # 支持 Pydantic Position 或原始 dict
        if hasattr(data, "model_dump"):
            inst_id_str = data.inst_id
            pos = data.pos
            avg_px = data.avg_px
            pos_side = data.pos_side
            ts_ms = data.ts
            pos_id_str = data.pos_id
        else:
            inst_id_str = data.get("instId") or ""
            try:
                pos = Decimal(data.get("pos") or "0")
            except Exception:
                pos = Decimal("0")
            try:
                avg_px = Decimal(data.get("avgPx") or "0")
            except Exception:
                avg_px = Decimal("0")
            pos_side = data.get("posSide") or "net"
            ts_ms = self._ms_or_none(data.get("uTime") or data.get("ts")) or 0
            pos_id_str = data.get("posId") or ""

        if not inst_id_str:
            return None

        instrument_id = to_instrument_id(inst_id_str)
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            return None

        position_side = okx_pos_to_position_side(pos, str(pos_side))
        ts_last = _ms_to_nanos(int(ts_ms)) if ts_ms else ts_init

        if position_side.name == "FLAT" or pos == 0:
            # FLAT position 不参与 venue_position_id 比对（NT create_flat 也不接收该参数）
            return PositionStatusReport.create_flat(
                account_id=self.account_id,
                instrument_id=instrument_id,
                size_precision=instrument.size_precision,
                ts_init=ts_init,
                report_id=UUID4(),
            )

        try:
            quantity = Quantity(float(abs(pos)), precision=instrument.size_precision)
        except Exception:
            return None

        # venue_position_id：优先用 OKX posId（snowflake 唯一）；空时退到稳定 fallback
        # ``{inst}-{side}``，避免 NT 自动 fallback 到 ``{inst}-EXTERNAL`` 占位 ID 与
        # 内部 order-driven ID 撞车。posId 在 demo / 实盘均有返回（已实测）。
        venue_position_id = (
            PositionId(pos_id_str)
            if pos_id_str
            else PositionId(f"{instrument_id.value}-{position_side.name}")
        )

        return PositionStatusReport(
            account_id=self.account_id,
            instrument_id=instrument_id,
            venue_position_id=venue_position_id,
            position_side=position_side,
            quantity=quantity,
            avg_px_open=avg_px if avg_px and avg_px > 0 else None,
            report_id=UUID4(),
            ts_last=ts_last,
            ts_init=ts_init,
        )

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        """单笔订单查询。``client_order_id`` 与 ``venue_order_id`` 至少一个非空。"""
        if self._rest is None:
            log.warning("okx.exec_client.report_rest_not_connected")
            return None
        if command.client_order_id is None and command.venue_order_id is None:
            raise ValueError("either client_order_id or venue_order_id must be provided")
        if command.instrument_id is None:
            log.warning("okx.exec_client.order_status_report_missing_instrument")
            return None

        inst_id = from_instrument_id(command.instrument_id)
        ord_id = command.venue_order_id.value if command.venue_order_id else None
        cl_ord_id = (
            self._sanitize_cl_ord_id(command.client_order_id)
            if command.client_order_id and not ord_id
            else None
        )
        try:
            data = await self._rest.trade.get_order(
                inst_id, ord_id=ord_id, cl_ord_id=cl_ord_id,
            )
        except Exception as exc:
            log.warning(
                "okx.exec_client.order_status_report_failed",
                extra={"inst_id": inst_id, "err": str(exc)},
            )
            return None
        if data is None:
            return None
        return self._build_order_status_report(data, ts_init=self._clock.timestamp_ns())

    async def generate_order_status_reports(
        self,
        command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        """启动 reconciliation：拉所有未结订单 + （非 open_only 时）历史已结。

        OKX 的两个端点：
        - ``/trade/orders-pending``：``live`` / ``partially_filled`` 的当前挂单；
        - ``/trade/orders-history-archive``：最近 3 月已结订单（按 ``begin``/``end`` ms 过滤）。

        ``command.start`` 是 NT 算的 lookback 时间（``utc_now - lookback_mins``）；
        我们翻译成 OKX 的 ``begin`` 毫秒。``open_only=True`` 时跳过 history。
        """
        if self._rest is None:
            log.warning("okx.exec_client.report_rest_not_connected")
            return []

        ts_init = self._clock.timestamp_ns()
        reports: list[OrderStatusReport] = []
        inst_id_filter = (
            from_instrument_id(command.instrument_id) if command.instrument_id else None
        )
        # 默认覆盖永续 + 现货；FUTURES/MARGIN/OPTION 暂不实现。
        inst_types = [InstType.SWAP.value, InstType.SPOT.value]

        # ---- pending ----
        for inst_type in inst_types:
            try:
                rows = await self._rest.trade.get_pending_orders(
                    inst_type=inst_type, inst_id=inst_id_filter,
                )
            except Exception as exc:
                log.warning(
                    "okx.exec_client.orders_pending_failed",
                    extra={"inst_type": inst_type, "err": str(exc)},
                )
                continue
            for row in rows:
                report = self._build_order_status_report(row, ts_init=ts_init)
                if report is not None:
                    reports.append(report)

        # ---- history（open_only=True 时不需要） ----
        if not command.open_only:
            begin_ms = self._datetime_to_ms(command.start)
            end_ms = self._datetime_to_ms(command.end)
            for inst_type in inst_types:
                try:
                    rows = await self._rest.trade.get_orders_history(
                        inst_type, inst_id=inst_id_filter,
                        begin_ms=begin_ms, end_ms=end_ms,
                    )
                except Exception as exc:
                    log.warning(
                        "okx.exec_client.orders_history_failed",
                        extra={"inst_type": inst_type, "err": str(exc)},
                    )
                    continue
                for row in rows:
                    report = self._build_order_status_report(row, ts_init=ts_init)
                    if report is not None:
                        reports.append(report)

        log.info(
            "okx.exec_client.order_status_reports_generated",
            extra={"count": len(reports), "open_only": command.open_only},
        )
        return reports

    async def generate_fill_reports(
        self,
        command: GenerateFillReports,
    ) -> list[FillReport]:
        """拉成交明细。OKX ``/trade/fills`` 默认覆盖最近 3 天；更久远的用户得用
        ``fills-history``——M5 不实现。"""
        if self._rest is None:
            log.warning("okx.exec_client.report_rest_not_connected")
            return []

        ts_init = self._clock.timestamp_ns()
        reports: list[FillReport] = []
        inst_id_filter = (
            from_instrument_id(command.instrument_id) if command.instrument_id else None
        )
        ord_id = command.venue_order_id.value if command.venue_order_id else None
        begin_ms = self._datetime_to_ms(command.start)
        end_ms = self._datetime_to_ms(command.end)

        # 单订单查询时不需要遍历 instType；通用查询时按 SWAP / SPOT 拉两次。
        if ord_id or inst_id_filter:
            inst_types: list[str | None] = [None]
        else:
            inst_types = [InstType.SWAP.value, InstType.SPOT.value]

        for inst_type in inst_types:
            try:
                rows = await self._rest.trade.get_fills(
                    inst_type=inst_type, inst_id=inst_id_filter,
                    ord_id=ord_id, begin_ms=begin_ms, end_ms=end_ms,
                )
            except Exception as exc:
                log.warning(
                    "okx.exec_client.fills_failed",
                    extra={"inst_type": inst_type, "err": str(exc)},
                )
                continue
            for row in rows:
                report = self._build_fill_report(row, ts_init=ts_init)
                if report is not None:
                    reports.append(report)

        log.info(
            "okx.exec_client.fill_reports_generated",
            extra={"count": len(reports)},
        )
        return reports

    async def generate_position_status_reports(
        self,
        command: GeneratePositionStatusReports,
    ) -> list[PositionStatusReport]:
        """拉持仓快照。``net`` / ``long_short`` 模式都靠 OKX ``pos`` 字段判定方向。

        现货账户走 ``positions`` 端点会返回空——NT 在 OmsType=NETTING 下不需要现货
        position report（资产以 AccountBalance 形式表达）。所以这里只查 SWAP。
        """
        if self._rest is None:
            log.warning("okx.exec_client.report_rest_not_connected")
            return []

        ts_init = self._clock.timestamp_ns()
        reports: list[PositionStatusReport] = []
        inst_id_filter = (
            from_instrument_id(command.instrument_id) if command.instrument_id else None
        )

        try:
            positions = await self._rest.account.get_positions(
                inst_type=InstType.SWAP, inst_id=inst_id_filter,
            )
        except Exception as exc:
            log.warning(
                "okx.exec_client.positions_failed",
                extra={"err": str(exc)},
            )
            return []

        for p in positions:
            report = self._build_position_status_report(p, ts_init=ts_init)
            if report is not None:
                reports.append(report)

        log.info(
            "okx.exec_client.position_reports_generated",
            extra={"count": len(reports)},
        )
        return reports

    # ------------------------------------------------------------------
    # Order/account update routing (from WS push)
    # ------------------------------------------------------------------

    def _handle_order_update(self, data: dict[str, Any]) -> None:
        """``orders`` 频道推送 → 路由到 generate_order_*。

        OKX 字段：``state`` ∈ {live, partially_filled, filled, canceled, mmp_canceled}。
        """
        cl_ord_id_str = data.get("clOrdId", "")
        ord_id_str = data.get("ordId", "")
        inst_id_str = data.get("instId", "")
        state = data.get("state", "")
        ts_event = _ms_to_nanos(int(data.get("uTime", "0") or "0")) or self._clock.timestamp_ns()

        if not inst_id_str:
            return

        from .parsing import to_instrument_id  # 局部 import 避免顶层循环
        instrument_id = to_instrument_id(inst_id_str)
        client_order_id = ClientOrderId(cl_ord_id_str) if cl_ord_id_str else None
        venue_order_id = VenueOrderId(ord_id_str) if ord_id_str else None

        # NT 需要 strategy_id：从 cache 反查
        strategy_id = None
        if client_order_id is not None:
            order = self._cache.order(client_order_id)
            if order is not None:
                strategy_id = order.strategy_id

        if strategy_id is None or client_order_id is None or venue_order_id is None:
            log.debug(
                "okx.exec_client.order_update_unrouted",
                extra={"data": data, "reason": "missing strategy/cl_ord_id/venue_id"},
            )
            return

        if state == "live":
            # Dedup：``_submit_order`` 在 REST 成功后已经 emit 过 OrderAccepted。
            # 只有在 NT 缓存里订单仍在 SUBMITTED（race：WS 早于 REST 响应、或
            # REST 没拿到 ord_id）才再 emit，避免 NT 状态机抱怨重复 transition。
            if order is None or order.status == OrderStatus.SUBMITTED:
                self.generate_order_accepted(
                    strategy_id=strategy_id,
                    instrument_id=instrument_id,
                    client_order_id=client_order_id,
                    venue_order_id=venue_order_id,
                    ts_event=ts_event,
                )
        elif state in ("partially_filled", "filled"):
            self._emit_fill(data, strategy_id, instrument_id, client_order_id, venue_order_id, ts_event)
        elif state in ("canceled", "mmp_canceled"):
            self.generate_order_canceled(
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                ts_event=ts_event,
            )

    def _emit_fill(
        self,
        data: dict[str, Any],
        strategy_id,
        instrument_id,
        client_order_id: ClientOrderId,
        venue_order_id: VenueOrderId,
        ts_event: int,
    ) -> None:
        """从 OKX orders 推送的 fill 部分构造 NT OrderFilled 事件。"""
        fill_px = data.get("fillPx", "0")
        fill_sz = data.get("fillSz", "0")
        trade_id_str = data.get("tradeId", "") or data.get("billId", "") or "unknown"
        fee = Decimal(data.get("fee", "0"))
        fee_ccy = data.get("feeCcy", "USDT") or "USDT"
        side = okx_side_to_nt(data.get("side", "buy"))
        ord_type_str = data.get("ordType", "limit")
        order_type = OrderType.MARKET if ord_type_str == "market" else OrderType.LIMIT
        # OKX fee 通常是负数（表示扣除），转成 NT Money 时取绝对值
        commission = Money(abs(fee), Currency.from_str(fee_ccy, strict=False))

        inst = self._cache.instrument(instrument_id)
        if inst is None:
            return  # instrument 还没注册——异常情况，丢弃
        last_px = Price(float(fill_px), precision=inst.price_precision)
        last_qty = Quantity(float(fill_sz), precision=inst.size_precision)
        liq = LiquiditySide.MAKER if data.get("execType") == "M" else LiquiditySide.TAKER

        self.generate_order_filled(
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            venue_position_id=None,
            trade_id=TradeId(str(trade_id_str)),
            order_side=side,
            order_type=order_type,
            last_qty=last_qty,
            last_px=last_px,
            quote_currency=inst.quote_currency,
            commission=commission,
            liquidity_side=liq,
            ts_event=ts_event,
        )

    def _handle_account_update(self, data: dict[str, Any]) -> None:
        """OKX ``account`` 推送 → :meth:`generate_account_state`。

        OKX 在私有账户频道推送的 JSON 与 ``GET /api/v5/account/balance`` 单条数据
        同形（``totalEq`` + ``details[]``），所以直接走 ``Balance.model_validate``。
        ``uTime`` 是毫秒时间戳，缺失时回退到本地时钟。
        """
        try:
            balance = Balance.model_validate(data)
        except Exception as exc:
            log.warning(
                "okx.exec_client.account_update_parse_failed",
                extra={"err": str(exc)},
            )
            return

        u_time = data.get("uTime", "0") or "0"
        ts_event = _ms_to_nanos(int(u_time)) if u_time != "0" else self._clock.timestamp_ns()
        self._emit_account_state(balance, ts_event=ts_event)

    def _emit_account_state(self, balance: Balance, *, ts_event: int) -> None:
        """把 :class:`Balance` 翻译成 NT ``AccountBalance`` 列表并发事件。

        每个 ``BalanceDetail`` 翻译成一条 NT ``AccountBalance``：

        - ``total = max(0, eq)``
        - ``free  = max(0, availBal)``，但不超过 ``total``（避免负 ``locked``）
        - ``locked = total - free``

        OKX 的多币种保证金账户里 ``details[]`` 会塞许多零余额条目，全部丢弃可以
        让 NT Portfolio 视图保持干净。NT 至少需要一条 balance 才会注册 venue 账户，
        所以如果 ``details[]`` 全部被过滤掉，回退发送一条 USDT 0 余额条目。
        """
        balances = _build_account_balances(balance)
        if not balances:
            balances = [
                AccountBalance(
                    Money(Decimal("0"), Currency.from_str("USDT", strict=False)),
                    Money(Decimal("0"), Currency.from_str("USDT", strict=False)),
                    Money(Decimal("0"), Currency.from_str("USDT", strict=False)),
                ),
            ]
        self.generate_account_state(
            balances=balances,
            margins=[],
            reported=True,
            ts_event=ts_event,
        )


__all__ = ["OKXLiveExecutionClient"]
