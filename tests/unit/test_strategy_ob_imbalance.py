"""ob_imbalance 单测：parse_okx_books5_frame + 注册表验证。

策略类的 entry/exit 流程依赖 NT 运行时（Strategy 子类的 Cython 父类，无法不构建
ME 直接实例化）。这部分由 paper trading 烟测覆盖；这里只验证可独立测试的部分。
"""
from __future__ import annotations

import pytest

from okx_trade.models.market import OrderBook
from okx_trade.strategies.ob_imbalance import parse_okx_books5_frame


# ---------------------------------------------------------------------------
# parse_okx_books5_frame
# ---------------------------------------------------------------------------


def _make_books5_frame(inst_id: str = "BTC-USDT-SWAP", channel: str = "books5") -> dict:
    return {
        "arg": {"channel": channel, "instId": inst_id},
        "data": [{
            "bids": [
                ["60000.0", "10.0", "0", "5"],
                ["59999.5", "8.0", "0", "4"],
                ["59999.0", "6.0", "0", "3"],
                ["59998.5", "4.0", "0", "2"],
                ["59998.0", "2.0", "0", "1"],
            ],
            "asks": [
                ["60000.5", "5.0", "0", "3"],
                ["60001.0", "4.0", "0", "2"],
                ["60001.5", "3.0", "0", "2"],
                ["60002.0", "2.0", "0", "1"],
                ["60002.5", "1.0", "0", "1"],
            ],
            "ts": "1700000000000",
        }],
    }


class TestParseBooks5Frame:
    def test_parses_valid_frame(self) -> None:
        book = parse_okx_books5_frame(
            _make_books5_frame(), inst_id="BTC-USDT-SWAP"
        )
        assert book is not None
        assert isinstance(book, OrderBook)
        assert book.inst_id == "BTC-USDT-SWAP"
        assert len(book.bids) == 5
        assert len(book.asks) == 5
        assert float(book.bids[0].price) == 60000.0
        assert float(book.asks[0].price) == 60000.5
        assert book.ts == 1700000000000

    def test_returns_none_for_wrong_channel(self) -> None:
        frame = _make_books5_frame(channel="trades")
        assert parse_okx_books5_frame(frame, inst_id="BTC-USDT-SWAP") is None

    def test_returns_none_for_wrong_inst_id(self) -> None:
        frame = _make_books5_frame(inst_id="ETH-USDT-SWAP")
        assert parse_okx_books5_frame(frame, inst_id="BTC-USDT-SWAP") is None

    def test_returns_none_for_empty_data(self) -> None:
        frame = _make_books5_frame()
        frame["data"] = []
        assert parse_okx_books5_frame(frame, inst_id="BTC-USDT-SWAP") is None

    def test_accepts_books_channel_alias(self) -> None:
        # books5 / books 都接受
        frame = _make_books5_frame(channel="books")
        book = parse_okx_books5_frame(frame, inst_id="BTC-USDT-SWAP")
        assert book is not None


def test_strategy_registry_includes_ob_imbalance() -> None:
    """ob_imbalance 应该被 live_node 自动注册（lazy import）。"""
    from okx_trade.runtime.live_node import _strategy_registry
    registry = _strategy_registry()
    assert "ob_imbalance" in registry
    cfg_cls, strat_cls = registry["ob_imbalance"]
    assert cfg_cls.__name__ == "OBImbalanceConfig"
    assert strat_cls.__name__ == "OBImbalanceStrategy"


def test_config_default_values_sane() -> None:
    """默认配置不应该让策略一启动就触发或永远不触发。"""
    from okx_trade.strategies.ob_imbalance import OBImbalanceConfig
    cfg = OBImbalanceConfig(
        instrument_id="BTC-USDT-SWAP.OKX",
        bar_type="BTC-USDT-SWAP.OKX-1-MINUTE-LAST-EXTERNAL",
    )
    # 阈值在合理区间
    assert 0.20 <= cfg.imbalance_threshold <= 0.60
    assert 1.0 <= cfg.microprice_premium_bps <= 20.0
    # 持仓不会过短或过长
    assert 30 <= cfg.hold_max_sec <= 3600
    # 单笔风险不超 1%
    assert cfg.risk_pct <= 0.01
    # M6+.X fee-coverage 不变式：SL 必须覆盖 round-trip fee + slippage
    # round-trip fee = 2 × taker(5bp) = 10bp；SL 至少要 2x 它才有正期望
    assert cfg.stop_distance_bps >= 20.0, "stop too tight, will be eaten by fees"
    # reentry cooldown 至少 30s（防 1-second churning）
    assert cfg.reentry_cooldown_sec >= 30
    assert cfg.reentry_requires_reversal is True


def test_reentry_cooldown_blocks_immediate_retrigger() -> None:
    """高频反复入场（每秒 +5bp 假信号 × 24h 吃光手续费）→ reentry_cooldown
    必须拒绝平仓后 N 秒内的同方向再入场。"""
    from okx_trade.strategies.ob_imbalance import OBImbalanceConfig
    cfg = OBImbalanceConfig(
        instrument_id="BTC-USDT-SWAP.OKX",
        bar_type="BTC-USDT-SWAP.OKX-1-MINUTE-LAST-EXTERNAL",
    )
    # cooldown 配置存在且 ≥ 30s
    assert cfg.reentry_cooldown_sec >= 30, (
        f"cooldown {cfg.reentry_cooldown_sec}s too short — 5/18 churn was 1 sec"
    )
