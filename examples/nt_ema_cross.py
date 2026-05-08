"""NT EMA crossover sample strategy on OKX.

跑法（demo 实盘 5 分钟，BTC-USDT-SWAP 1 分钟 K 线）::

    cd /Users/zhangxudong/okx-trade
    pip install -e ".[strategy,dev]"
    python examples/nt_ema_cross.py

要求 ``.env`` 已配置 ``OKX_API_KEY/SECRET_KEY/PASSPHRASE`` + ``OKX_IS_DEMO=true`` +
（国内）``OKX_HTTP_PROXY``。

预期日志：每分钟收到 1 个 1m bar；快线 fast_ema 上穿 slow_ema 触发 buy，
反之触发 sell。Demo 账户应能看到订单填表 → fill 事件。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from nautilus_trader.config import (
    InstrumentProviderConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.indicators.averages import ExponentialMovingAverage
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.orders import MarketOrder
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

from okx_trade.adapter import (
    OKX_VENUE,
    OKXDataClientConfig,
    OKXExecClientConfig,
    OKXLiveDataClientFactory,
    OKXLiveExecClientFactory,
)


class EMACrossConfig(StrategyConfig, frozen=True):
    instrument_id: str = "BTC-USDT-SWAP.OKX"
    bar_type: str = "BTC-USDT-SWAP.OKX-1-MINUTE-LAST-EXTERNAL"
    fast_period: int = 5
    slow_period: int = 20
    trade_size: Decimal = Decimal("1")  # 1 张合约（cVal=0.01 BTC ≈ $670）


class EMACross(Strategy):
    """快慢 EMA 金叉/死叉。简化版，不带 stop / sizing —— 仅 demo adapter 联通。"""

    def __init__(self, config: EMACrossConfig) -> None:
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.fast_ema = ExponentialMovingAverage(config.fast_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_period)
        self._last_position: int = 0  # -1 short, 0 flat, 1 long

    def on_start(self) -> None:
        self.subscribe_bars(self.bar_type)
        self.log.info(f"subscribed to {self.bar_type}")

    def on_bar(self, bar: Bar) -> None:
        self.fast_ema.update_raw(bar.close.as_double())
        self.slow_ema.update_raw(bar.close.as_double())
        if not (self.fast_ema.initialized and self.slow_ema.initialized):
            return

        fast = self.fast_ema.value
        slow = self.slow_ema.value
        self.log.info(f"bar close={bar.close} fast={fast:.2f} slow={slow:.2f}")

        if fast > slow and self._last_position <= 0:
            self._enter(OrderSide.BUY)
            self._last_position = 1
        elif fast < slow and self._last_position >= 0:
            self._enter(OrderSide.SELL)
            self._last_position = -1

    def _enter(self, side: OrderSide) -> None:
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            self.log.warning(f"instrument {self.instrument_id} not in cache; skipping")
            return
        order = MarketOrder(
            trader_id=self.trader_id,
            strategy_id=self.id,
            instrument_id=self.instrument_id,
            client_order_id=self.generate_order_id(),
            order_side=side,
            quantity=instrument.make_qty(self.config.trade_size),
            init_id=self.generate_command_id(),
            ts_init=self.clock.timestamp_ns(),
        )
        self.submit_order(order)


def main() -> None:
    """启动 NT TradingNode，跑 5 分钟即停。"""
    instrument_id = "BTC-USDT-SWAP.OKX"

    node_config = TradingNodeConfig(
        trader_id="OKX-EMA-001",
        logging=LoggingConfig(log_level="INFO"),
        data_clients={
            "OKX": OKXDataClientConfig(
                instrument_provider=InstrumentProviderConfig(
                    load_ids=frozenset([instrument_id]),
                ),
                # api_key 等留空 → 自动从 .env 读取
            ),
        },
        exec_clients={
            "OKX": OKXExecClientConfig(
                instrument_provider=InstrumentProviderConfig(
                    load_ids=frozenset([instrument_id]),
                ),
                td_mode="cross",
                pos_side_mode="net",
            ),
        },
        timeout_connection=30.0,
        timeout_reconciliation=10.0,
        timeout_portfolio=10.0,
        timeout_disconnection=10.0,
    )

    node = TradingNode(config=node_config)
    node.add_data_client_factory("OKX", OKXLiveDataClientFactory)
    node.add_exec_client_factory("OKX", OKXLiveExecClientFactory)

    strategy_config = EMACrossConfig()
    strategy = EMACross(config=strategy_config)
    node.trader.add_strategy(strategy)

    node.build()

    async def _run_for(seconds: float) -> None:
        await node.run_async()
        await asyncio.sleep(seconds)
        await node.stop_async()

    try:
        asyncio.run(_run_for(seconds=300.0))
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
