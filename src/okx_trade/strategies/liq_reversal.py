"""强平瀑布反转（Liquidation Cascade Reversal）。

直觉
----
crypto 永续合约存在显著的「强平瀑布」现象：单根 K 线内多头（或空头）批量被强平，
触发瀑布式平仓 → 价格短暂超调 → 几分钟内反向回归。

机制：
- 多头被批量强平 = 大量市价卖单冲击订单簿 → 价格超跌 → 反弹做多机会；
- 空头被批量强平 = 大量市价买单冲击 → 价格超涨 → 做空机会。

OKX 在 ``liquidation-orders`` 公共频道实时推送强平 fill。我们对最近 ``window_sec``
内的强平名义额做 z-score 分析（vs 过去 ``history_sec`` 的滚动均值/标准差），
当 ``z > threshold`` 且主导方向明确时，立即下市价反向单。

设计
----
- **数据通路**：策略持有自己的 ``OKXWSClient``（与 ``funding_carry`` 自持
  ``OKXRestClient`` 同套路），在 ``on_start`` 启动后台 asyncio task 订阅
  ``liquidation-orders`` 频道；
- **触发**：每收到 1+ 条 liq event，重算 z-score；命中阈值 + 过冷却即入场；
- **退出**：synthetic bracket，``on_bar`` 用 1m K 线检查 SL/TP 触发；
- **回测**：禁用 WS（``subscribe_liquidations=False``），由测试 / backtest fixture
  直接调 ``process_liq_event(liq)`` 喂数据。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from statistics import StatisticsError, mean, stdev
from typing import TYPE_CHECKING, Literal

from ..risk import (
    RiskConfig,
    RiskIntent,
    apply_risk_manager,
    build_risk_manager,
)
from .base import BarSnapshot, position_contracts, to_bar_snapshots
from .pnl_hook import record_strategy_equity_daily, record_strategy_trade

LiqSide = Literal["buy", "sell"]
TradeDir = Literal["long", "short"]


# ---------------------------------------------------------------------------
# Pure data structures + functions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiqEvent:
    """单条强平成交。

    Attributes:
        ts_ms: 毫秒时间戳。
        inst_id: OKX 原始 instId（如 ``BTC-USDT-SWAP``）。
        side: 强平**对手方向**——``"sell"`` 表示市场出现卖单（多头被强平）；
            ``"buy"`` 表示市场出现买单（空头被强平）。
            （直接采用 OKX 推送中 ``details[].side`` 的语义。）
        sz_usd: 名义额（USD），由调用方在 parse 时算好（``bkPx × sz × ct_val``）。
    """

    ts_ms: int
    inst_id: str
    side: LiqSide
    sz_usd: float


def liq_zscore(
    events: deque[LiqEvent] | list[LiqEvent],
    *,
    window_sec: int,
    history_sec: int,
    now_ms: int,
) -> tuple[float, LiqSide | None]:
    """对最近 ``window_sec`` 内的强平名义额做 z-score 检验，并返回主导方向。

    历史分布：把 ``[now - history_sec, now - window_sec]`` 区间切成
    ``(history_sec - window_sec) // window_sec`` 个长度为 ``window_sec`` 的桶，
    每个桶累计 ``sum(sz_usd)``，作为历史采样。当前值 = 最近 ``window_sec`` 的
    名义额累计。z = (current - mean(history)) / stdev(history)。

    Args:
        events: 升序时间序列。
        window_sec / history_sec: 窗口与历史长度（秒）。``history_sec`` 必须 > ``2 * window_sec``。
        now_ms: 当前时间戳（毫秒）。

    Returns:
        ``(zscore, dominant_side)``：
        - ``zscore`` 为 0 表示样本不足或 std 为 0；
        - ``dominant_side`` 是当前窗口内 ``sz_usd`` 加权多数方（``"buy"`` 或
          ``"sell"``），无事件时为 None。
    """
    if window_sec <= 0 or history_sec <= 2 * window_sec:
        return 0.0, None
    window_ms = window_sec * 1000
    history_ms = history_sec * 1000
    cur_cutoff = now_ms - window_ms
    hist_cutoff = now_ms - history_ms

    # 1) 当前窗口
    cur_buy = 0.0
    cur_sell = 0.0
    for ev in events:
        if ev.ts_ms < cur_cutoff or ev.ts_ms > now_ms:
            continue
        if ev.side == "buy":
            cur_buy += ev.sz_usd
        else:
            cur_sell += ev.sz_usd
    cur_total = cur_buy + cur_sell
    if cur_total <= 0:
        return 0.0, None

    dominant: LiqSide = "buy" if cur_buy >= cur_sell else "sell"

    # 2) 历史 buckets（不含当前窗口）
    n_buckets = max(1, (history_sec - window_sec) // window_sec)
    bucket_sums = [0.0] * n_buckets
    for ev in events:
        if ev.ts_ms < hist_cutoff or ev.ts_ms >= cur_cutoff:
            continue
        offset_ms = cur_cutoff - ev.ts_ms  # >0
        idx = int(offset_ms // window_ms)
        if 0 <= idx < n_buckets:
            bucket_sums[idx] += ev.sz_usd

    # 没有任何历史 → 退化为 0
    if all(b == 0.0 for b in bucket_sums) or len(bucket_sums) < 2:
        return 0.0, dominant
    try:
        m = mean(bucket_sums)
        sd = stdev(bucket_sums)
    except StatisticsError:
        return 0.0, dominant
    if sd <= 0:
        return 0.0, dominant
    return (cur_total - m) / sd, dominant


def liq_reversal_decision(
    zscore: float,
    dominant: LiqSide | None,
    threshold: float = 3.0,
) -> TradeDir | None:
    """根据 z-score + 主导方向决定反向交易。

    - dominant = ``"sell"`` → 多头被强平 → **做多反弹**
    - dominant = ``"buy"``  → 空头被强平 → **做空回归**
    - z 不足或 dominant=None → 不交易

    Args:
        zscore: ``liq_zscore`` 输出的标量。
        dominant: 主导被强平方向。
        threshold: z 阈值（默认 3.0 = 3σ 异常）。

    Returns:
        ``"long"`` / ``"short"`` / None。
    """
    if dominant is None or zscore < threshold:
        return None
    return "long" if dominant == "sell" else "short"


def parse_okx_liq_frame(
    frame: dict,
    perp_ct_val: dict[str, float] | None = None,
) -> list[LiqEvent]:
    """从 OKX ``liquidation-orders`` 推送 frame 解出 LiqEvent 列表。

    OKX V5 推送结构::

        {"arg": {"channel": "liquidation-orders", "instType": "SWAP"},
         "data": [{"instId": "BTC-USDT-SWAP",
                   "details": [{"side": "sell", "bkPx": "60000.0",
                                "sz": "10", "ts": "1697..."}]}]}

    每个 ``details`` 是一笔强平 fill。``sz`` 是合约张数，需要 ``bkPx × sz ×
    ct_val`` 算名义 USD。``ct_val`` 由调用方传入（OKX SWAP 各 instrument
    不同，BTC-USDT-SWAP=0.01）。

    Args:
        frame: 原始 OKX 推送。
        perp_ct_val: ``{inst_id: ct_val}``；缺失则 fallback 到 ct_val=1（粗略
            按 sz × bkPx 估算）。

    Returns:
        升序按 ``ts_ms`` 排好的 LiqEvent 列表。
    """
    out: list[LiqEvent] = []
    if frame.get("arg", {}).get("channel") != "liquidation-orders":
        return out
    perp_ct_val = perp_ct_val or {}
    for entry in frame.get("data", []) or []:
        inst_id = entry.get("instId", "")
        if not inst_id:
            continue
        ct_val = float(perp_ct_val.get(inst_id, 1.0))
        for d in entry.get("details", []) or []:
            try:
                side = d["side"]
                if side not in ("buy", "sell"):
                    continue
                bk_px = float(d.get("bkPx", "0"))
                sz = float(d.get("sz", "0"))
                ts = int(d.get("ts", "0"))
            except (KeyError, ValueError, TypeError):
                continue
            if bk_px <= 0 or sz <= 0 or ts <= 0:
                continue
            out.append(LiqEvent(
                ts_ms=ts, inst_id=inst_id, side=side,
                sz_usd=bk_px * sz * ct_val,
            ))
    out.sort(key=lambda e: e.ts_ms)
    return out


# ---------------------------------------------------------------------------
# NautilusTrader Strategy wrapper
# ---------------------------------------------------------------------------


if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar


try:
    import asyncio

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


if _NT_AVAILABLE:

    class LiqReversalConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """Liq Reversal NT Strategy 配置。

        Attributes:
            instrument_id: 目标合约 NT id（如 ``"BTC-USDT-SWAP.OKX"``）。
            bar_type: 1m K 线 BarType（用于 SL/TP 检查与喂风控）。
            liq_window_sec: 当前强平窗口（默认 60s）。
            liq_history_sec: 历史 z-score 计算窗口（默认 86400s = 24h）。
            zscore_threshold: 触发阈值（默认 3.0σ）。
            cooldown_sec: 触发后冷却期（默认 300s）。
            entry_stop_distance_pct: 入场止损距离（默认 0.5%）。
            entry_tp_rr_ratio: TP / SL ratio（默认 2.0）。
            risk_pct: 单笔风险占净值（默认 0.5%）。
            account_equity_usdt: 回测虚拟净值。
            buffer_size: 强平 event deque maxlen（默认 50000，足够覆盖 24h 高峰）。
            subscribe_liquidations: 是否在 ``on_start`` 自动启动后台 WS task
                订阅 ``liquidation-orders``。回测中应设 False，由 fixture 直接喂。
            risk_config: 可选风控；推荐 ``enable_drawdown=True``。
        """

        instrument_id: str
        bar_type: str
        liq_window_sec: int = 60
        liq_history_sec: int = 86400
        zscore_threshold: float = 3.0
        cooldown_sec: int = 300
        entry_stop_distance_pct: float = 0.005
        entry_tp_rr_ratio: float = 2.0
        risk_pct: float = 0.005
        account_equity_usdt: float = 10000.0
        buffer_size: int = 50000
        subscribe_liquidations: bool = True
        risk_config: RiskConfig | None = None


    class LiqReversalStrategy(Strategy):  # type: ignore[misc]
        """订阅 OKX ``liquidation-orders`` 公共频道，z-score 触发反向 entry。

        持仓状态：
        - ``self._active_direction`` ∈ ``{None, "long", "short"}``；
        - ``self._entry_price`` / ``self._stop_price`` / ``self._tp_price``：
          synthetic bracket 的价位；
        - ``on_bar`` 每根 1m bar 检查 high/low 是否击穿 SL/TP，触发反向平仓。
        """

        def __init__(self, config: LiqReversalConfig) -> None:
            super().__init__(config)
            self._inst_id = InstrumentId.from_str(config.instrument_id)
            self._bar_type = BarType.from_str(config.bar_type)

            self._liq_buffer: deque[LiqEvent] = deque(maxlen=config.buffer_size)
            self._latest_close: float = 0.0
            self._latest_ts_ms: int = 0
            self._last_trigger_ms: int = 0

            self._active_direction: TradeDir | None = None
            self._entry_price: float = 0.0
            self._stop_price: float = 0.0
            self._tp_price: float = 0.0
            self._position_contracts: float = 0.0

            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._prev_close: float | None = None

            # M5: PnL tracker（外部注入）
            self._pnl_tracker = None  # type: ignore[var-annotated]
            self._last_equity_day: str | None = None

            self._okx_ws = None  # type: ignore[var-annotated]
            self._liq_task: asyncio.Task | None = None

        # ------------------------------------------------------------------
        # NT lifecycle
        # ------------------------------------------------------------------

        def on_start(self) -> None:
            self.subscribe_bars(self._bar_type)
            self.log.info(
                f"liq_reversal start: inst={self._inst_id.value} "
                f"window={self.config.liq_window_sec}s threshold={self.config.zscore_threshold}σ"
            )
            if self.config.subscribe_liquidations:
                try:
                    loop = asyncio.get_running_loop()
                    self._liq_task = loop.create_task(self._liq_loop())
                except RuntimeError:
                    self.log.warning(
                        "liq_reversal: no running loop; falling back to manual feed mode"
                    )

        def on_stop(self) -> None:
            if self._liq_task is not None:
                self._liq_task.cancel()
            if self._okx_ws is not None:
                try:
                    loop = asyncio.get_running_loop()
                    # 保留引用以便 GC 不回收（fire-and-forget 关闭 ws 连接）
                    self._close_task = loop.create_task(self._okx_ws.close())
                except RuntimeError:
                    pass
            self.log.info(f"liq_reversal stop; active={self._active_direction}")

        def on_bar(self, bar: Bar) -> None:
            if bar.bar_type != self._bar_type:
                return
            snap = to_bar_snapshots([bar])[0]
            self._latest_close = snap.close
            self._latest_ts_ms = snap.ts
            self._feed_risk_data(snap)
            if self._active_direction is not None:
                self._check_exit_on_bar(snap)
                return
            # 每根 bar 末尾也尝试触发一次（保险，避免 WS 推送间隔过大错过窗口）
            self.maybe_trigger()

        # ------------------------------------------------------------------
        # 数据接入（被 WS task 或测试 fixture 调用）
        # ------------------------------------------------------------------

        def process_liq_event(self, liq: LiqEvent) -> None:
            """喂一条 LiqEvent；自动触发判定。

            供 ``_liq_loop`` 与回测 fixture 共用。
            """
            if liq.inst_id != self._inst_id.symbol.value:
                return
            self._liq_buffer.append(liq)
            if liq.ts_ms > self._latest_ts_ms:
                self._latest_ts_ms = liq.ts_ms
            self.maybe_trigger()

        def maybe_trigger(self) -> None:
            """若满足触发条件则下入场单。无副作用（已持仓 / 冷却中直接 return）。"""
            if self._active_direction is not None:
                return
            if self._latest_ts_ms <= 0 or self._latest_close <= 0:
                return
            if self._latest_ts_ms - self._last_trigger_ms < self.config.cooldown_sec * 1000:
                return

            z, dominant = liq_zscore(
                self._liq_buffer,
                window_sec=self.config.liq_window_sec,
                history_sec=self.config.liq_history_sec,
                now_ms=self._latest_ts_ms,
            )
            decision = liq_reversal_decision(z, dominant, threshold=self.config.zscore_threshold)
            if decision is None:
                return

            self.log.info(
                f"liq_reversal trigger: z={z:.2f}σ dominant={dominant} → {decision} "
                f"@ {self._latest_close:.4f}"
            )
            self._enter(decision)
            self._last_trigger_ms = self._latest_ts_ms

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
                if handles.drawdown_tracker is not None:
                    handles.drawdown_tracker.record_equity(ts_ms=snap.ts, equity=equity_usdt)
                # M5: 每个 UTC 日写一条 equity snapshot 给 PnL tracker
                self._last_equity_day = record_strategy_equity_daily(
                    self._pnl_tracker,
                    strategy_id=str(self.id),
                    ts_ms=snap.ts,
                    equity_usdt=equity_usdt,
                    last_day=self._last_equity_day,
                )

        # ------------------------------------------------------------------
        # Entry / Exit
        # ------------------------------------------------------------------

        def _enter(self, direction: TradeDir) -> None:
            cfg: LiqReversalConfig = self.config  # type: ignore[assignment]
            inst = self.cache.instrument(self._inst_id)
            if inst is None:
                self.log.warning(f"liq_reversal: instrument missing {self._inst_id}")
                return
            ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
            lot_sz = float(inst.size_increment) if float(inst.size_increment) > 0 else 1.0

            entry = self._latest_close
            if direction == "long":
                stop = entry * (1.0 - cfg.entry_stop_distance_pct)
                tp = entry + (entry - stop) * cfg.entry_tp_rr_ratio
            else:
                stop = entry * (1.0 + cfg.entry_stop_distance_pct)
                tp = entry - (stop - entry) * cfg.entry_tp_rr_ratio

            contracts = position_contracts(
                account_equity_usdt=cfg.account_equity_usdt,
                risk_pct=cfg.risk_pct,
                entry_price=entry,
                stop_price=stop,
                ct_val=ct_val,
                min_sz=lot_sz,
                lot_sz=lot_sz,
            )
            if contracts <= 0:
                self.log.info("liq_reversal: contracts=0; skip enter")
                return

            intent = RiskIntent(
                strategy_id=str(self.id),
                instrument_id=self._inst_id.value,
                direction=direction,
                size=contracts,
                entry_price=entry,
                stop_price=stop,
                account_equity_usdt=cfg.account_equity_usdt,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None or adjusted <= 0:
                return
            contracts = adjusted

            side = OrderSide.BUY if direction == "long" else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=self._inst_id,
                order_side=side,
                quantity=inst.make_qty(Decimal(str(contracts))),
                time_in_force=TimeInForce.IOC,
            )
            self.submit_order(order)
            self._active_direction = direction
            self._entry_price = entry
            self._stop_price = stop
            self._tp_price = tp
            self._position_contracts = contracts
            self.log.info(
                f"ENTER {direction.upper()} {contracts} @ {entry:.4f} "
                f"sl={stop:.4f} tp={tp:.4f}"
            )

        def _check_exit_on_bar(self, bar: BarSnapshot) -> None:
            if self._active_direction is None:
                return
            if self._active_direction == "long":
                hit_sl = bar.low <= self._stop_price
                hit_tp = bar.high >= self._tp_price
            else:
                hit_sl = bar.high >= self._stop_price
                hit_tp = bar.low <= self._tp_price
            if not (hit_sl or hit_tp):
                return
            reason = "SL" if hit_sl else "TP"
            exit_price = self._stop_price if reason == "SL" else self._tp_price
            self._exit(reason, exit_price=exit_price, ts_ms=bar.ts)

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
            close_side = (
                OrderSide.SELL if direction == "long" else OrderSide.BUY
            )
            order = self.order_factory.market(
                instrument_id=self._inst_id,
                order_side=close_side,
                quantity=inst.make_qty(Decimal(str(contracts))),
                time_in_force=TimeInForce.IOC,
                reduce_only=True,
            )
            self.submit_order(order)
            self.log.info(
                f"EXIT {direction.upper()} reason={reason} qty={contracts}"
            )

            if exit_price is not None and ts_ms is not None:
                cfg: LiqReversalConfig = self.config  # type: ignore[assignment]
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
                    risk_usdt=cfg.account_equity_usdt * cfg.risk_pct,
                )

            self._active_direction = None
            self._position_contracts = 0.0

        # ------------------------------------------------------------------
        # Background WS task
        # ------------------------------------------------------------------

        async def _liq_loop(self) -> None:
            from ..config import OKXSettings
            from ..ws.client import OKXWSClient

            try:
                self._okx_ws = OKXWSClient(OKXSettings())
                await self._okx_ws.__aenter__()
                async with self._okx_ws.subscribe(
                    "liquidation-orders", instType="SWAP",
                ) as stream:
                    perp_ct_val = self._build_ct_val_lookup()
                    async for frame in stream:
                        events = parse_okx_liq_frame(frame, perp_ct_val)
                        for liq in events:
                            self.process_liq_event(liq)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error(f"liq_loop crashed: {exc}")

        def _build_ct_val_lookup(self) -> dict[str, float]:
            """从 NT cache 取目标 inst 的 ct_val（其余 inst 漏推时按 1.0 回退）。"""
            out: dict[str, float] = {}
            inst = self.cache.instrument(self._inst_id)
            if inst is not None:
                out[self._inst_id.symbol.value] = float(inst.multiplier) or 1.0
            return out

else:  # pragma: no cover

    class LiqReversalConfig:  # type: ignore[no-redef]
        pass

    class LiqReversalStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError("nautilus_trader not installed; run pip install -e '.[strategy]'")


__all__ = [
    "LiqEvent",
    "LiqReversalConfig",
    "LiqReversalStrategy",
    "liq_reversal_decision",
    "liq_zscore",
    "parse_okx_liq_frame",
]
