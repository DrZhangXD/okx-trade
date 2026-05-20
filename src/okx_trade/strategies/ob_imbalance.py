"""订单簿不平衡（OB Imbalance / Microprice）策略。

直觉
----
当订单簿一侧深度显著大于另一侧（bid 重 / ask 重），且 Stoikov microprice 也朝
同向偏离 mid 时，下一段时间价格倾向于朝深度厚的一侧移动（卖方稀缺 / 买方稀缺
是真实成交意愿，不像挂单可以撤）。

机制：
- 订阅 OKX ``books5`` 公共频道；
- 每秒聚合最新的 5 档 snapshot，计算 ``book_imbalance`` 与 ``microprice``；
- 维护 30 秒滚动平均 imbalance（过滤瞬时抖动）；
- 当 ``|imb| > threshold`` AND microprice 偏离 mid ≥ premium_bps 同号 → 入场；
- synthetic bracket（紧 SL ~8bp + tp_rr 1.5）+ 超时强平（默认 5min）。

频率
----
中频版本（plan 选项 A）：每秒 1 次决策、目标持仓 30s–5min、单日数十笔。
不做 sub-second tick-by-tick（手续费成本会吃掉边际）。
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING, Literal

from ..models.market import OrderBook
from ..risk import RiskConfig, RiskIntent, apply_risk_manager, build_risk_manager
from ._signals import book_imbalance, microprice, ob_signal
from .base import BarSnapshot, effective_equity_usdt, position_contracts, to_bar_snapshots
from .pnl_hook import record_strategy_equity_daily, record_strategy_trade
from .qty import safe_make_qty

if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar


TradeDir = Literal["long", "short"]


try:
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.trading.config import StrategyConfig
    from nautilus_trader.trading.strategy import Strategy

    _NT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NT_AVAILABLE = False
    StrategyConfig = object  # type: ignore[assignment,misc]
    Strategy = object  # type: ignore[assignment,misc]


def parse_okx_books5_frame(frame: dict, inst_id: str) -> OrderBook | None:
    """OKX books5 推送 → ``OrderBook``，或 None（数据缺失 / inst_id 不匹配）。

    OKX 推送形如 ``{"arg": {"channel": "books5", "instId": "..."},
    "data": [{"bids": [[px,sz,_,num], ...], "asks": [...], "ts": "..."}]}``。
    """
    if frame.get("arg", {}).get("channel") not in ("books5", "books"):
        return None
    if frame.get("arg", {}).get("instId") != inst_id:
        return None
    data = frame.get("data") or []
    if not data:
        return None
    try:
        return OrderBook.from_payload(inst_id, data[0])
    except (KeyError, ValueError, IndexError):
        return None


if _NT_AVAILABLE:

    class OBImbalanceConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """OB Imbalance NT Strategy 配置。

        Attributes:
            instrument_id: 永续合约 NT id（``"BTC-USDT-SWAP.OKX"``）。
            bar_type: 用来跟踪 close + 喂风控 + 触发节流的 bar（1m）。
            aggregation_sec: 多久聚合一次 imbalance（默认 1s）。
            imbalance_window_sec: 滚动平均窗口（默认 30s）。
            imbalance_threshold: |滚动平均 imb| > 此值才触发（默认 0.35）。
            microprice_premium_bps: microprice 偏离 mid 的 bp 量级阈值（默认 3）。
            hold_max_sec: 单笔持仓上限（默认 300s = 5min），超时强平。
            stop_distance_bps: SL 距离 bp（默认 8）。
            tp_rr_ratio: TP / SL 比（默认 1.5）。
            risk_pct: 单笔风险占净值（默认 0.3% — 因频率高，比其他策略小一档）。
            account_equity_usdt: 回测虚拟净值。
            subscribe_books5: 是否在 on_start 启动 WS task；回测时设 False。
            depth: 计算 imbalance 用的档数（默认 5，配合 books5）。
            risk_config: 可选风控。
        """

        instrument_id: str
        bar_type: str
        aggregation_sec: int = 1
        imbalance_window_sec: int = 30
        imbalance_threshold: float = 0.45
        microprice_premium_bps: float = 5.0
        hold_max_sec: int = 300
        # M6+.X 修：原 8bp SL 太紧（BTC tick 0.5 USDT，几秒必触发），SL 不
        # 够覆盖 round-trip fee（5bps × 2 legs = 10bps），等于每笔都净亏。
        # 改 30bp 让单笔 alpha 有空间覆盖 fee。
        stop_distance_bps: float = 30.0
        tp_rr_ratio: float = 1.5
        # M6+.X 修：reentry cooldown，平仓后 60s 内不能再入场
        reentry_cooldown_sec: int = 60
        # M6+.X 修：reentry 要求 imbalance 先反转（跨过 0）才能再入场，避免同方向连扫
        reentry_requires_reversal: bool = True
        risk_pct: float = 0.002
        account_equity_usdt: float = 10000.0
        subscribe_books5: bool = True
        depth: int = 5
        risk_config: RiskConfig | None = None


    class OBImbalanceStrategy(Strategy):  # type: ignore[misc]
        """订阅 books5 → 中频聚合 imbalance + microprice → 反转入场。

        持仓状态：
        - ``self._active_direction`` ∈ ``{None, "long", "short"}``；
        - ``self._stop_price`` / ``self._tp_price``：synthetic bracket；
        - ``self._entry_ts_ms``：用于 hold_max_sec 超时强平；
        - ``on_bar`` 每根 1m bar 检查 SL/TP/timeout。
        """

        def __init__(self, config: OBImbalanceConfig) -> None:
            super().__init__(config)
            self._inst_id = InstrumentId.from_str(config.instrument_id)
            self._bar_type = BarType.from_str(config.bar_type)
            self._latest_book: OrderBook | None = None
            self._imb_history: deque[tuple[int, float]] = deque(maxlen=2 * config.imbalance_window_sec)
            self._micro_history: deque[tuple[int, float]] = deque(maxlen=2 * config.imbalance_window_sec)
            self._latest_close: float = 0.0
            self._latest_ts_ms: int = 0
            self._last_aggregate_ms: int = 0

            self._active_direction: TradeDir | None = None
            self._entry_price: float = 0.0
            self._stop_price: float = 0.0
            self._tp_price: float = 0.0
            self._position_contracts: float = 0.0
            self._entry_ts_ms: int = 0
            self._allocated_equity_usdt: float | None = None

            # M6+.X reentry cooldown / reversal lock
            self._last_exit_ts_ms: int = 0
            self._last_exit_imb_sign: int = 0  # +1 long → 平仓时 imb 是正；-1 short
            self._imb_sign_flipped_since_exit: bool = False

            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._prev_close: float | None = None

            self._pnl_tracker = None  # type: ignore[var-annotated]
            self._last_equity_day: str | None = None

            self._okx_ws = None  # type: ignore[var-annotated]
            self._book_task: asyncio.Task | None = None

        def on_start(self) -> None:
            self.subscribe_bars(self._bar_type)
            self.log.info(
                f"ob_imbalance start: inst={self._inst_id.value} "
                f"imb_thr={self.config.imbalance_threshold} "
                f"micro_prem={self.config.microprice_premium_bps}bp "
                f"hold_max={self.config.hold_max_sec}s"
            )
            if self.config.subscribe_books5:
                try:
                    loop = asyncio.get_running_loop()
                    self._book_task = loop.create_task(self._books_loop())
                except RuntimeError:
                    self.log.warning(
                        "ob_imbalance: no running loop; falling back to manual feed mode"
                    )

        def on_stop(self) -> None:
            if self._book_task is not None:
                self._book_task.cancel()
            if self._okx_ws is not None:
                try:
                    loop = asyncio.get_running_loop()
                    self._close_task = loop.create_task(self._okx_ws.close())
                except RuntimeError:
                    pass
            self.log.info(f"ob_imbalance stop; active={self._active_direction}")

        def on_bar(self, bar: Bar) -> None:
            if bar.bar_type != self._bar_type:
                return
            snap = to_bar_snapshots([bar])[0]
            self._latest_close = snap.close
            self._latest_ts_ms = snap.ts
            self._feed_risk_data(snap)
            if self._active_direction is not None:
                self._check_exit_on_bar(snap)

        # ------------------------------------------------------------------
        # 数据接入
        # ------------------------------------------------------------------

        def process_orderbook(self, book: OrderBook) -> None:
            """喂一帧 OrderBook；按 aggregation_sec 节流后触发决策。回测 / 测试可直接调用。"""
            if book.inst_id != self._inst_id.symbol.value:
                return
            self._latest_book = book
            now_ms = book.ts
            if now_ms - self._last_aggregate_ms < self.config.aggregation_sec * 1000:
                return
            self._last_aggregate_ms = now_ms
            self._aggregate_and_maybe_trigger()

        def _aggregate_and_maybe_trigger(self) -> None:
            book = self._latest_book
            if book is None or not book.bids or not book.asks:
                return
            bid_qtys = [float(lvl.size) for lvl in book.bids]
            ask_qtys = [float(lvl.size) for lvl in book.asks]
            top_bid = book.bids[0]
            top_ask = book.asks[0]
            mid = (float(top_bid.price) + float(top_ask.price)) / 2.0
            micro = microprice(
                bid_px=float(top_bid.price), bid_qty=float(top_bid.size),
                ask_px=float(top_ask.price), ask_qty=float(top_ask.size),
            )
            imb = book_imbalance(bid_qtys, ask_qtys, depth=self.config.depth)

            now_ms = book.ts
            window_ms = self.config.imbalance_window_sec * 1000
            cutoff_ms = now_ms - window_ms
            self._imb_history.append((now_ms, imb))
            self._micro_history.append((now_ms, micro))
            while self._imb_history and self._imb_history[0][0] < cutoff_ms:
                self._imb_history.popleft()
            while self._micro_history and self._micro_history[0][0] < cutoff_ms:
                self._micro_history.popleft()

            if self._active_direction is not None:
                return

            # 滚动平均
            avg_imb = sum(v for _, v in self._imb_history) / max(1, len(self._imb_history))
            avg_micro = sum(v for _, v in self._micro_history) / max(1, len(self._micro_history))

            # M6+.X reentry gate：平仓后 cooldown + 要求 imbalance 反转
            # 才能再入场，避免同方向高频连扫（每次都吃 spread + fee）
            if self._last_exit_ts_ms > 0:
                cooldown_ms = self.config.reentry_cooldown_sec * 1000
                if now_ms - self._last_exit_ts_ms < cooldown_ms:
                    return  # 冷却期内
                if self.config.reentry_requires_reversal:
                    # imbalance 当前 sign 跟上次 exit 时反向 → 标记 flipped
                    cur_sign = 1 if avg_imb > 0 else (-1 if avg_imb < 0 else 0)
                    if cur_sign != 0 and cur_sign == -self._last_exit_imb_sign:
                        self._imb_sign_flipped_since_exit = True
                    if not self._imb_sign_flipped_since_exit:
                        return  # imbalance 还没反转过，禁止入场

            decision = ob_signal(
                imbalance=avg_imb,
                mid_px=mid,
                micro_px=avg_micro,
                imbalance_threshold=self.config.imbalance_threshold,
                microprice_premium_bps=self.config.microprice_premium_bps,
            )
            if decision is None:
                return
            self.log.info(
                f"ob_imbalance trigger: avg_imb={avg_imb:+.2f} mid={mid:.4f} "
                f"avg_micro={avg_micro:.4f} → {decision}"
            )
            self._enter(decision, entry_price=mid)

        # ------------------------------------------------------------------
        # 风控喂数据
        # ------------------------------------------------------------------

        def _feed_risk_data(self, snap: BarSnapshot) -> None:
            handles = self._risk_handles
            inst_id_str = self._inst_id.value
            if handles.vol_target is not None and self._prev_close is not None and self._prev_close > 0:
                ret = snap.close / self._prev_close - 1.0
                handles.vol_target.update_return(inst_id_str, ret)
            self._prev_close = snap.close

            equity_usdt: float | None = None
            try:
                account = self.portfolio.account(self._inst_id.venue)
                if account is not None:
                    from nautilus_trader.model.currencies import USDT
                    bal = account.balance_total(USDT)
                    if bal is not None:
                        equity_usdt = float(bal.as_decimal())
            except Exception:
                equity_usdt = None

            if equity_usdt is not None:
                now_ms = int(time.time() * 1000)
                self._last_equity_day = record_strategy_equity_daily(
                    self._pnl_tracker,
                    strategy_id=str(self.id),
                    ts_ms=now_ms,
                    equity_usdt=equity_usdt,
                    last_day=self._last_equity_day,
                )

        # ------------------------------------------------------------------
        # Entry / Exit
        # ------------------------------------------------------------------

        def _enter(self, direction: TradeDir, *, entry_price: float) -> None:
            cfg: OBImbalanceConfig = self.config  # type: ignore[assignment]
            inst = self.cache.instrument(self._inst_id)
            if inst is None:
                self.log.warning(f"ob_imbalance: instrument missing {self._inst_id}")
                return
            ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
            lot_sz = float(inst.size_increment) if float(inst.size_increment) > 0 else 1.0

            sl_dist_bps = cfg.stop_distance_bps
            sl_dist_pct = sl_dist_bps / 10000.0
            if direction == "long":
                stop = entry_price * (1.0 - sl_dist_pct)
                tp = entry_price + (entry_price - stop) * cfg.tp_rr_ratio
            else:
                stop = entry_price * (1.0 + sl_dist_pct)
                tp = entry_price - (stop - entry_price) * cfg.tp_rr_ratio

            equity = effective_equity_usdt(self._allocated_equity_usdt, cfg.account_equity_usdt)
            contracts = position_contracts(
                account_equity_usdt=equity,
                risk_pct=cfg.risk_pct,
                entry_price=entry_price,
                stop_price=stop,
                ct_val=ct_val,
                min_sz=lot_sz,
                lot_sz=lot_sz,
            )
            if contracts <= 0:
                self.log.info("ob_imbalance: contracts=0; skip enter")
                return

            intent = RiskIntent(
                strategy_id=str(self.id),
                instrument_id=self._inst_id.value,
                direction=direction,
                size=contracts,
                entry_price=entry_price,
                stop_price=stop,
                account_equity_usdt=equity,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None or adjusted <= 0:
                return
            contracts = adjusted

            qty_obj = safe_make_qty(
                inst, contracts, self.log, ctx=f"enter {self._inst_id.value}"
            )
            if qty_obj is None:
                return
            side = OrderSide.BUY if direction == "long" else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=self._inst_id,
                order_side=side,
                quantity=qty_obj,
                time_in_force=TimeInForce.IOC,
            )
            self.submit_order(order)
            self._active_direction = direction
            self._entry_price = entry_price
            self._stop_price = stop
            self._tp_price = tp
            self._position_contracts = contracts
            self._entry_ts_ms = self._latest_ts_ms or int(time.time() * 1000)
            self.log.info(
                f"ENTER {direction.upper()} {contracts} @ {entry_price:.4f} "
                f"sl={stop:.4f} tp={tp:.4f}"
            )

        def _check_exit_on_bar(self, bar: BarSnapshot) -> None:
            if self._active_direction is None:
                return
            # 1. SL / TP
            if self._active_direction == "long":
                hit_sl = bar.low <= self._stop_price
                hit_tp = bar.high >= self._tp_price
            else:
                hit_sl = bar.high >= self._stop_price
                hit_tp = bar.low <= self._tp_price
            if hit_sl or hit_tp:
                reason = "SL" if hit_sl else "TP"
                exit_price = self._stop_price if hit_sl else self._tp_price
                self._exit(reason, exit_price=exit_price, ts_ms=bar.ts)
                return
            # 2. Timeout
            if bar.ts - self._entry_ts_ms >= self.config.hold_max_sec * 1000:
                self._exit("TIMEOUT", exit_price=bar.close, ts_ms=bar.ts)

        def _exit(
            self,
            reason: str,
            *,
            exit_price: float | None = None,
            ts_ms: int | None = None,
        ) -> None:
            if self._active_direction is None or self._position_contracts <= 0:
                return
            inst = self.cache.instrument(self._inst_id)
            if inst is None:
                return
            ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
            direction = self._active_direction
            entry_price = self._entry_price
            contracts = self._position_contracts
            qty_obj = safe_make_qty(
                inst, contracts, ctx=f"exit {self._inst_id.value}"
            )
            if qty_obj is None:
                self.log.error(
                    f"EXIT skipped: position contracts {contracts} < lot_sz "
                    f"for {self._inst_id.value}; position remains open"
                )
                return
            close_side = (
                OrderSide.SELL if direction == "long" else OrderSide.BUY
            )
            order = self.order_factory.market(
                instrument_id=self._inst_id,
                order_side=close_side,
                quantity=qty_obj,
                time_in_force=TimeInForce.IOC,
                reduce_only=True,
            )
            self.submit_order(order)
            self.log.info(
                f"EXIT {direction.upper()} reason={reason} qty={contracts}"
            )

            if exit_price is not None and ts_ms is not None:
                cfg: OBImbalanceConfig = self.config  # type: ignore[assignment]
                record_strategy_trade(
                    self._pnl_tracker,
                    strategy_id=str(self.id),
                    instrument_id=self._inst_id.value,
                    closed_ts_ms=ts_ms,
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    contracts=contracts,
                    ct_val=ct_val,
                    risk_usdt=effective_equity_usdt(
                        self._allocated_equity_usdt, cfg.account_equity_usdt,
                    ) * cfg.risk_pct,
                )

            # M6+.X 记录 last_exit 状态供 reentry gate 用
            self._last_exit_ts_ms = ts_ms or int(time.time() * 1000)
            self._last_exit_imb_sign = 1 if self._active_direction == "long" else -1
            self._imb_sign_flipped_since_exit = False

            self._active_direction = None
            self._position_contracts = 0.0
            self._entry_ts_ms = 0

        def on_order_rejected(self, event) -> None:  # noqa: ANN001
            # 同 liq_reversal：被拒即清状态避免幻仓级联。
            if self._active_direction is None and self._position_contracts == 0.0:
                return
            self.log.warning(
                f"order rejected; clearing phantom state "
                f"(was active={self._active_direction} qty={self._position_contracts}): "
                f"{getattr(event, 'reason', '?')}"
            )
            self._active_direction = None
            self._position_contracts = 0.0
            self._entry_ts_ms = 0

        async def _books_loop(self) -> None:
            from ..config import OKXSettings
            from ..ws.client import OKXWSClient

            inst_id_str = self._inst_id.symbol.value
            try:
                self._okx_ws = OKXWSClient(OKXSettings())
                await self._okx_ws.__aenter__()
                async with self._okx_ws.subscribe(
                    "books5", instId=inst_id_str,
                ) as stream:
                    async for frame in stream:
                        book = parse_okx_books5_frame(frame, inst_id_str)
                        if book is not None:
                            self.process_orderbook(book)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error(f"books_loop crashed: {exc}")


else:  # pragma: no cover

    class OBImbalanceConfig:  # type: ignore[no-redef]
        pass

    class OBImbalanceStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError("nautilus_trader not installed; run pip install -e '.[strategy]'")


__all__ = [
    "OBImbalanceConfig",
    "OBImbalanceStrategy",
    "parse_okx_books5_frame",
]
