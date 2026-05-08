"""Adapter parsing 层单测：纯函数翻译，不依赖 NT live runtime。"""
from __future__ import annotations

import pytest
from nautilus_trader.model.enums import (
    AggressorSide,
    BarAggregation,
    OrderSide,
    OrderType,
    TimeInForce,
)
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import CryptoPerpetual, CurrencyPair

from okx_trade.adapter.constants import OKX_VENUE
from okx_trade.adapter.parsing import (
    bar_type_to_okx_period,
    from_instrument_id,
    make_bar_type,
    okx_side_to_nt,
    order_side_to_okx,
    order_type_and_tif_to_okx,
    parse_okx_book5_to_quote_tick,
    parse_okx_candle_to_bar,
    parse_okx_instrument,
    parse_okx_trade_to_trade_tick,
    to_instrument_id,
)
from okx_trade.enums import OrdType, Side
from okx_trade.models.common import Instrument
from okx_trade.models.market import Candle

# 共用 ts
TS = 1_700_000_000_000_000_000


# ---------------------------------------------------------------------------
# InstrumentId 双向
# ---------------------------------------------------------------------------


class TestInstrumentId:
    def test_to_instrument_id(self) -> None:
        iid = to_instrument_id("BTC-USDT-SWAP")
        assert isinstance(iid, InstrumentId)
        assert iid.symbol.value == "BTC-USDT-SWAP"
        assert iid.venue == OKX_VENUE

    def test_roundtrip(self) -> None:
        for s in ("BTC-USDT-SWAP", "ETH-USDT", "SOL-USDC-SWAP"):
            assert from_instrument_id(to_instrument_id(s)) == s


# ---------------------------------------------------------------------------
# Instrument 翻译
# ---------------------------------------------------------------------------


class TestParseInstrument:
    def test_swap_to_crypto_perpetual(self) -> None:
        okx = Instrument.model_validate({
            "instId": "BTC-USDT-SWAP", "instType": "SWAP",
            "baseCcy": "BTC", "quoteCcy": "USDT", "settleCcy": "USDT",
            "ctVal": "0.01", "ctValCcy": "BTC",
            "tickSz": "0.1", "lotSz": "1", "minSz": "1", "state": "live",
        })
        nt = parse_okx_instrument(okx, ts_init=TS)
        assert isinstance(nt, CryptoPerpetual)
        assert str(nt.id) == "BTC-USDT-SWAP.OKX"
        assert nt.is_inverse is False  # USDT 计价不是反向
        assert nt.price_precision == 1  # tickSz=0.1 → 1 位小数
        assert nt.size_precision == 0   # lotSz=1 → 0 位
        assert str(nt.quote_currency) == "USDT"
        assert str(nt.settlement_currency) == "USDT"
        # info 透传 OKX 的 state
        assert nt.info["okx_state"] == "live"

    def test_inverse_swap(self) -> None:
        # BTC-USD-SWAP：用 BTC 结算，settleCcy == baseCcy → inverse
        okx = Instrument.model_validate({
            "instId": "BTC-USD-SWAP", "instType": "SWAP",
            "baseCcy": "BTC", "quoteCcy": "USD", "settleCcy": "BTC",
            "ctVal": "100", "ctValCcy": "USD",
            "tickSz": "0.1", "lotSz": "1", "minSz": "1", "state": "live",
        })
        nt = parse_okx_instrument(okx, ts_init=TS)
        assert isinstance(nt, CryptoPerpetual)
        assert nt.is_inverse is True

    def test_spot_to_currency_pair(self) -> None:
        okx = Instrument.model_validate({
            "instId": "BTC-USDT", "instType": "SPOT",
            "baseCcy": "BTC", "quoteCcy": "USDT",
            "tickSz": "0.01", "lotSz": "0.0001", "minSz": "0.0001", "state": "live",
        })
        nt = parse_okx_instrument(okx, ts_init=TS)
        assert isinstance(nt, CurrencyPair)
        assert nt.price_precision == 2
        assert nt.size_precision == 4

    def test_swap_missing_settle_raises(self) -> None:
        okx = Instrument.model_validate({
            "instId": "X-Y-SWAP", "instType": "SWAP",
            "baseCcy": "X", "quoteCcy": "Y", "settleCcy": "",  # 空
            "ctVal": "1", "tickSz": "0.1", "lotSz": "1", "minSz": "1", "state": "live",
        })
        with pytest.raises(ValueError, match="missing settleCcy"):
            parse_okx_instrument(okx, ts_init=TS)


# ---------------------------------------------------------------------------
# QuoteTick / TradeTick
# ---------------------------------------------------------------------------


class TestParseQuoteTick:
    def test_books5_first_level(self) -> None:
        qt = parse_okx_book5_to_quote_tick(
            {
                "bids": [["67000.5", "10", "0", "1"], ["67000.4", "20", "0", "2"]],
                "asks": [["67000.6", "5", "0", "1"]],
                "ts": "1700000000000",
            },
            inst_id="BTC-USDT-SWAP",
            price_precision=1,
            size_precision=0,
            ts_init=TS,
        )
        assert qt is not None
        assert str(qt.bid_price) == "67000.5"
        assert str(qt.ask_price) == "67000.6"
        assert qt.bid_size.as_double() == 10.0
        assert qt.ask_size.as_double() == 5.0
        # ts 取自 OKX（毫秒 → 纳秒）
        assert qt.ts_event == 1_700_000_000_000_000_000

    def test_empty_book_returns_none(self) -> None:
        qt = parse_okx_book5_to_quote_tick(
            {"bids": [], "asks": [], "ts": "1700000000000"},
            inst_id="X-Y", price_precision=2, size_precision=2, ts_init=TS,
        )
        assert qt is None


class TestParseTradeTick:
    @pytest.mark.parametrize("side,expected", [
        ("buy", AggressorSide.BUYER),
        ("sell", AggressorSide.SELLER),
    ])
    def test_aggressor_side(self, side: str, expected: AggressorSide) -> None:
        tt = parse_okx_trade_to_trade_tick(
            {"px": "100.5", "sz": "1", "side": side, "tradeId": "abc",
             "ts": "1700000000000"},
            inst_id="BTC-USDT-SWAP", price_precision=1, size_precision=0, ts_init=TS,
        )
        assert tt.aggressor_side == expected
        assert str(tt.price) == "100.5"


# ---------------------------------------------------------------------------
# Bar
# ---------------------------------------------------------------------------


class TestBarType:
    @pytest.mark.parametrize("period,step,agg", [
        ("1m", 1, BarAggregation.MINUTE),
        ("5m", 5, BarAggregation.MINUTE),
        ("1H", 1, BarAggregation.HOUR),
        ("4H", 4, BarAggregation.HOUR),
        ("1D", 1, BarAggregation.DAY),
        ("1W", 1, BarAggregation.WEEK),
    ])
    def test_make_and_back(self, period: str, step: int, agg: BarAggregation) -> None:
        bt = make_bar_type("BTC-USDT-SWAP", period)
        assert bt.spec.step == step
        assert bt.spec.aggregation == agg
        # 反向回到 OKX 字符串
        assert bar_type_to_okx_period(bt) == period

    def test_unsupported_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported"):
            make_bar_type("BTC-USDT-SWAP", "1Y")


class TestParseCandle:
    def test_candle_to_bar(self) -> None:
        # OKX REST get_candles 数组顺序：[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        candle = Candle.from_array([
            "1700000000000", "67000", "67100", "66950", "67050",
            "12.5", "838125.0", "838125.0", "1",
        ])
        bt = make_bar_type("BTC-USDT-SWAP", "1m")
        bar = parse_okx_candle_to_bar(candle, bar_type=bt,
                                      price_precision=0, size_precision=2, ts_init=TS)
        assert str(bar.open) == "67000"
        assert str(bar.high) == "67100"
        assert str(bar.low) == "66950"
        assert str(bar.close) == "67050"
        # volume 按 size_precision 量化
        assert bar.volume.as_double() == 12.5
        # ts_event = 收盘时间 = candle.ts(open) + bar_period（1m = 60_000 ms）
        assert bar.ts_event == 1_700_000_060_000_000_000
        # ts_init 由调用方传入（实盘是 wall-clock，回测是收盘时间）
        assert bar.ts_init == TS


# ---------------------------------------------------------------------------
# Order side / type
# ---------------------------------------------------------------------------


class TestOrderConversion:
    def test_side_to_okx(self) -> None:
        assert order_side_to_okx(OrderSide.BUY) == Side.BUY
        assert order_side_to_okx(OrderSide.SELL) == Side.SELL

    def test_okx_side_to_nt(self) -> None:
        assert okx_side_to_nt("buy") == OrderSide.BUY
        assert okx_side_to_nt("sell") == OrderSide.SELL

    @pytest.mark.parametrize("ot,tif,expected", [
        (OrderType.MARKET, TimeInForce.GTC, OrdType.MARKET),
        (OrderType.LIMIT, TimeInForce.GTC, OrdType.LIMIT),
        (OrderType.LIMIT, TimeInForce.IOC, OrdType.IOC),
        (OrderType.LIMIT, TimeInForce.FOK, OrdType.FOK),
    ])
    def test_type_and_tif(self, ot, tif, expected: OrdType) -> None:
        assert order_type_and_tif_to_okx(ot, tif) == expected

    def test_unsupported_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported"):
            order_type_and_tif_to_okx(OrderType.STOP_MARKET, TimeInForce.GTC)
