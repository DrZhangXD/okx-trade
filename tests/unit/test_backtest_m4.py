"""M4 策略 backtest / NT 集成层冲烟（纯 Python 部分）。

**重要**：NT ``BacktestEngine`` 在同一 pytest 进程内只能跑 1 次（Cython 全局状态
不能在 ``BacktestNode`` 之间清理），现有 ``test_backtest_runner.py`` 已经占用了
单测套件里那 1 次。所以 M4 的 NT 端到端冲烟放在 ``scripts/backtest_m4_smoke.py``，
单独进程手动跑::

    python scripts/backtest_m4_smoke.py xs_momentum
    python scripts/backtest_m4_smoke.py liq_reversal

本模块只做**纯 Python 层**的 trigger pipeline 冲烟（zscore + decision），不依赖
NT engine。Strategy class 与 NT 的集成靠 ``scripts/backtest_m4_smoke.py`` 与 M4
之后的 paper trading 阶段（``scripts/live.py``）验证。
"""
from __future__ import annotations

from okx_trade.strategies.liq_reversal import (
    LiqEvent,
    liq_reversal_decision,
    liq_zscore,
)


class TestLiqReversalLogicSmoke:
    """直接喂合成 spike 验证 trigger pipeline，跳过 NT engine。"""

    def test_spike_triggers_long_decision(self) -> None:
        # 24h 历史每分钟 100 USD，当前窗口 60 秒爆出 50000 USD（多头被强平）
        events: list[LiqEvent] = []
        now_s = 86400
        for sec in range(60, 86400, 60):
            events.append(LiqEvent(
                ts_ms=(now_s - sec) * 1000,
                inst_id="BTC-USDT-SWAP",
                side="buy",   # 历史 buy/sell 不影响 z 计算（只看总额）
                sz_usd=100.0,
            ))
        events.append(LiqEvent(
            ts_ms=(now_s - 30) * 1000,
            inst_id="BTC-USDT-SWAP",
            side="sell",
            sz_usd=50000.0,
        ))
        events.sort(key=lambda e: e.ts_ms)

        z, dom = liq_zscore(
            events, window_sec=60, history_sec=86400, now_ms=now_s * 1000,
        )
        assert z > 3.0
        assert dom == "sell"
        assert liq_reversal_decision(z, dom, threshold=3.0) == "long"

    def test_no_trigger_below_threshold(self) -> None:
        # n_buckets = (86400-60)/60 = 1439；每 bucket 中点放 1 条 100 USD → std=0
        now_ms = 86_400_000
        cur_cutoff_s = 86340
        events: list[LiqEvent] = []
        for k in range(1439):
            ts_s = cur_cutoff_s - k * 60 - 30  # bucket k 中点
            events.append(LiqEvent(
                ts_ms=ts_s * 1000, inst_id="BTC-USDT-SWAP",
                side="buy", sz_usd=100.0,
            ))
        # 当前窗口 1 条普通数据
        events.append(LiqEvent(
            ts_ms=86_370_000, inst_id="BTC-USDT-SWAP",
            side="sell", sz_usd=120.0,
        ))
        events.sort(key=lambda e: e.ts_ms)
        z, dom = liq_zscore(
            events, window_sec=60, history_sec=86400, now_ms=now_ms,
        )
        # std=0 → impl 返回 z=0
        assert z == 0.0
        assert liq_reversal_decision(z, dom, threshold=3.0) is None
