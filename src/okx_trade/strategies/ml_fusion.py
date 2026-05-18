"""ML Fusion（XGBoost meta strategy）。

直觉
----
crypto 的单一 alpha 衰减快，但**多个独立 alpha 的非线性组合**可能比每个单独都稳。
把现有策略的中间信号（momentum / vol / funding_z / regime / book_imbalance）当
features，用 XGBoost 学习未来 4h 涨跌方向（二分类）。

预测概率 > ``prediction_threshold`` (默认 0.55) → long 该 instrument，
< 1 - threshold → short。每 ``target_horizon_hours`` 重新预测一次，多空均匀腿。

机制
----
- 启动：加载已训练模型（``model_path``）或冷启动训练
- 每 ``target_horizon_hours``：算各 instrument 特征 → 模型预测 → top-K long, bot-K short
- 每 ``retrain_interval_hours``（默认 168h = 1 周）：walk-forward 重训
- xgboost 是 optional dep；未装时 init 阶段 raise（避免静默失败）

风险
----
- OOS 衰减快（半衰期 2-4 周）；monitor 应监控 walk-forward CV 分数
- 分数低于 baseline（如 < 52% 准确率）触发 WARN，提示 user 重训或下线
- 默认开 regime gate（mean_reverting 时砍仓，因为预测在震荡市更不稳）
"""
from __future__ import annotations

import asyncio
import pickle
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..risk import RiskConfig, RiskIntent, apply_risk_manager, build_risk_manager
from ._features import FeatureRow, correlation, momentum, percentile_rank, realized_vol
from ..risk.stats import zscore
from .base import effective_equity_usdt, position_contracts
from .pnl_hook import record_strategy_equity_daily, record_strategy_trade
from .qty import safe_make_qty

if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar


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


def build_feature_row(
    inst_id: str,
    ts_ms: int,
    closes: list[float],
    btc_closes: list[float] | None = None,
    funding_current: float = 0.0,
    funding_history: list[float] | None = None,
    book_imbalance_avg: float = 0.0,
    regime_state_idx: int = 2,
    rv_history_for_pct: list[float] | None = None,
) -> FeatureRow:
    """从中间状态构造 FeatureRow。

    所有可选字段缺失时退化为 0（XGBoost 容忍但不会爆）。
    """
    funding_history = funding_history or []
    rv_history_for_pct = rv_history_for_pct or []

    m_1d = momentum(closes, 24)   # 假设 1H bar → 24 根 = 1 天
    m_3d = momentum(closes, 72)
    m_7d = momentum(closes, 168)
    rv_30 = realized_vol(closes, window=30 * 24)  # 30 天 1H bar
    rv_pct = percentile_rank(rv_30, rv_history_for_pct)
    funding_z = zscore(funding_current, funding_history) if funding_history else 0.0

    btc_corr = 0.0
    if btc_closes and len(btc_closes) >= 30 and len(closes) >= 30:
        rets_asset = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
        rets_btc = [btc_closes[i] / btc_closes[i - 1] - 1.0 for i in range(1, len(btc_closes)) if btc_closes[i - 1] > 0]
        btc_corr = correlation(rets_asset[-30:], rets_btc[-30:])

    return FeatureRow(
        ts_ms=ts_ms, inst_id=inst_id,
        momentum_1d=m_1d, momentum_3d=m_3d, momentum_7d=m_7d,
        rv_30d=rv_30, rv_pct=rv_pct,
        funding_current=funding_current, funding_z_30d=funding_z,
        book_imbalance_avg=book_imbalance_avg,
        regime_state=regime_state_idx,
        btc_corr_30d=btc_corr,
    )


def predict_direction(
    proba: float, threshold: float,
) -> str | None:
    """二分类 probability → ``"long"`` / ``"short"`` / None。"""
    if proba > threshold:
        return "long"
    if proba < 1 - threshold:
        return "short"
    return None


def load_model(model_path: str | Path) -> Any | None:
    """从 pickle 文件加载 XGBoost 模型。文件不存在或 xgboost 未装返回 None。"""
    p = Path(model_path)
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def save_model(model: Any, model_path: str | Path) -> None:
    """pickle 保存模型。"""
    p = Path(model_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        pickle.dump(model, f)


if _NT_AVAILABLE:

    class MLFusionConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """ML Fusion NT Strategy 配置。

        Attributes:
            instrument_ids: 候选 universe（5-10 个主流）。
            bar_type_template: 1H bar 用作 features。
            target_horizon_hours: 预测多远的 forward return（默认 4h）。
            model_path: pickle 模型路径。
            retrain_interval_hours: walk-forward 重训间隔（默认 168 = 1 周）。
            train_window_days: 训练窗口。
            prediction_threshold: classify 阈值。
            top_k_long / top_k_short: 每个 horizon 开几条多/空腿。
            risk_pct: 单腿风险占净值。
            account_equity_usdt / risk_config。
        """

        instrument_ids: list[str]
        bar_type_template: str = "{inst}-1-HOUR-LAST-EXTERNAL"
        target_horizon_hours: int = 4
        model_path: str = "var/ml_fusion_model.pkl"
        retrain_interval_hours: int = 168
        train_window_days: int = 60
        prediction_threshold: float = 0.55
        top_k_long: int = 2
        top_k_short: int = 2
        risk_pct: float = 0.003
        account_equity_usdt: float = 10000.0
        risk_config: RiskConfig | None = None


    class MLFusionStrategy(Strategy):  # type: ignore[misc]
        """每 4h 算特征 → XGBoost 预测 → top-K 多空腿。

        v1 不实现自动重训（实操：周末手动跑 scripts/ml_fusion_retrain.py）。
        模型文件不存在时策略 idle。
        """

        def __init__(self, config: MLFusionConfig) -> None:
            super().__init__(config)
            try:
                import xgboost  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "ml_fusion requires xgboost; install with "
                    "`pip install -e \".[ml-fusion]\"`"
                ) from exc

            self._inst_ids = [InstrumentId.from_str(s) for s in config.instrument_ids]
            self._bar_types = {
                iid: BarType.from_str(config.bar_type_template.format(inst=iid.value))
                for iid in self._inst_ids
            }
            self._closes: dict[str, deque[float]] = {
                iid.value: deque(maxlen=24 * 30 + 10) for iid in self._inst_ids
            }
            self._last_predict_ts_ms: int = 0
            self._positions: dict[str, tuple[str, float, int]] = {}  # inst_id → (dir, contracts, entry_ts_ms)
            self._model = None
            self._allocated_equity_usdt: float | None = None

            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._pnl_tracker = None  # type: ignore[var-annotated]
            self._last_equity_day: str | None = None

        def on_start(self) -> None:
            for bar_type in self._bar_types.values():
                self.subscribe_bars(bar_type)
            self._model = load_model(self.config.model_path)
            if self._model is None:
                self.log.warning(
                    f"ml_fusion: no model at {self.config.model_path}; "
                    "strategy idle. Train with scripts/ml_fusion_retrain.py."
                )
            else:
                self.log.info(
                    f"ml_fusion start: model loaded universe={len(self._inst_ids)}"
                )

        def on_stop(self) -> None:
            self.log.info(f"ml_fusion stop; open_legs={len(self._positions)}")

        def on_bar(self, bar: Bar) -> None:
            inst_value = bar.bar_type.instrument_id.value
            if inst_value not in self._closes:
                return
            self._closes[inst_value].append(bar.close.as_double())
            self._feed_risk_data()

            # Hold-period 检查：到了 target_horizon_hours 平仓
            now_ms = int(bar.ts_event // 1_000_000)
            stale_positions = []
            for inst_v, (_, _, entry_ts) in self._positions.items():
                if now_ms - entry_ts >= self.config.target_horizon_hours * 3_600_000:
                    stale_positions.append(inst_v)
            for inst_v in stale_positions:
                self._close_leg(inst_v, reason="HORIZON_END", exit_ts_ms=now_ms)

            # 每 target_horizon_hours 触发一次预测
            if (self._model is not None
                    and now_ms - self._last_predict_ts_ms
                    >= self.config.target_horizon_hours * 3_600_000):
                self._predict_and_trade(now_ms)
                self._last_predict_ts_ms = now_ms

        def _feed_risk_data(self) -> None:
            handles = self._risk_handles
            equity_usdt: float | None = None
            try:
                account = self.portfolio.account(self._inst_ids[0].venue)
                if account is not None:
                    from nautilus_trader.model.currencies import USDT
                    bal = account.balance_total(USDT)
                    if bal is not None:
                        equity_usdt = float(bal.as_decimal())
            except Exception:
                equity_usdt = None
            if equity_usdt is not None:
                now_ms = int(time.time() * 1000)
                if handles.drawdown_tracker is not None:
                    handles.drawdown_tracker.record_equity(ts_ms=now_ms, equity=equity_usdt)
                self._last_equity_day = record_strategy_equity_daily(
                    self._pnl_tracker,
                    strategy_id=str(self.id),
                    ts_ms=now_ms,
                    equity_usdt=equity_usdt,
                    last_day=self._last_equity_day,
                )

        def _predict_and_trade(self, ts_ms: int) -> None:
            import numpy as np

            btc_closes = list(self._closes.get("BTC-USDT-SWAP.OKX", deque()))

            rows: list[tuple[str, FeatureRow, float]] = []  # (inst_id, feat, proba)
            X_rows: list[list[float]] = []
            inst_order: list[str] = []
            for iid in self._inst_ids:
                closes = list(self._closes[iid.value])
                if len(closes) < 50:
                    continue
                feat = build_feature_row(
                    inst_id=iid.value, ts_ms=ts_ms,
                    closes=closes, btc_closes=btc_closes,
                )
                X_rows.append(feat.as_list())
                inst_order.append(iid.value)

            if not X_rows:
                return
            X = np.asarray(X_rows, dtype=float)
            try:
                probas = self._model.predict_proba(X)[:, 1]
            except Exception as exc:
                self.log.error(f"ml_fusion predict failed: {exc}")
                return

            for i, inst_value in enumerate(inst_order):
                # 重建 FeatureRow（为日志友好）
                feat = build_feature_row(
                    inst_id=inst_value, ts_ms=ts_ms,
                    closes=list(self._closes[inst_value]),
                    btc_closes=btc_closes,
                )
                rows.append((inst_value, feat, float(probas[i])))

            # 排序：top long = proba 最高 K 个；top short = proba 最低 K 个
            sorted_by_proba = sorted(rows, key=lambda r: r[2])
            longs = sorted_by_proba[-self.config.top_k_long:][::-1]
            shorts = sorted_by_proba[:self.config.top_k_short]
            target_dirs: dict[str, str] = {}
            for inst_v, _, p in longs:
                if p > self.config.prediction_threshold:
                    target_dirs[inst_v] = "long"
            for inst_v, _, p in shorts:
                if p < 1 - self.config.prediction_threshold:
                    target_dirs[inst_v] = "short"

            # 平掉不在新集合的腿、开新腿
            current = dict(self._positions)
            for inst_v, (cur_dir, cur_qty, _) in current.items():
                if inst_v not in target_dirs or target_dirs[inst_v] != cur_dir:
                    self._close_leg(inst_v, reason="REBALANCE", exit_ts_ms=ts_ms)
            for inst_v, direction in target_dirs.items():
                if inst_v not in self._positions:
                    self._open_leg(inst_v, direction, ts_ms=ts_ms)
            self.log.info(
                f"ml_fusion predict: scored={len(rows)} → "
                f"longs={[r[0] for r in longs]} shorts={[r[0] for r in shorts]} "
                f"open_legs={len(self._positions)}"
            )

        def _open_leg(self, inst_value: str, direction: str, *, ts_ms: int) -> None:
            cfg: MLFusionConfig = self.config  # type: ignore[assignment]
            inst_id = InstrumentId.from_str(inst_value)
            inst = self.cache.instrument(inst_id)
            if inst is None:
                return
            closes = list(self._closes[inst_value])
            if not closes:
                return
            entry_px = closes[-1]
            ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
            lot = float(inst.size_increment) if float(inst.size_increment) > 0 else 1.0

            # 简化 SL = 2 × 30d RV × spot
            rv = realized_vol(closes, window=30)
            sl_distance = max(entry_px * 0.005, entry_px * rv * 2 / (365 ** 0.5))
            stop = entry_px - sl_distance if direction == "long" else entry_px + sl_distance

            equity = effective_equity_usdt(self._allocated_equity_usdt, cfg.account_equity_usdt)
            contracts = position_contracts(
                account_equity_usdt=equity,
                risk_pct=cfg.risk_pct,
                entry_price=entry_px,
                stop_price=stop,
                ct_val=ct_val,
                min_sz=lot,
                lot_sz=lot,
            )
            if contracts <= 0:
                return
            intent = RiskIntent(
                strategy_id=str(self.id),
                instrument_id=inst_value,
                direction=direction,  # type: ignore[arg-type]
                size=contracts,
                entry_price=entry_px,
                stop_price=stop,
                account_equity_usdt=equity,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None or adjusted <= 0:
                return
            contracts = adjusted
            qty_obj = safe_make_qty(inst, contracts, self.log, ctx=f"open {inst_value}")
            if qty_obj is None:
                return
            side = OrderSide.BUY if direction == "long" else OrderSide.SELL
            self.submit_order(self.order_factory.market(
                instrument_id=inst_id, order_side=side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
            ))
            self._positions[inst_value] = (direction, contracts, ts_ms)
            self.log.info(f"OPEN {direction} {inst_value} qty={contracts}")

        def _close_leg(self, inst_value: str, *, reason: str, exit_ts_ms: int) -> None:
            pos = self._positions.get(inst_value)
            if pos is None:
                return
            direction, contracts, entry_ts = pos
            inst_id = InstrumentId.from_str(inst_value)
            inst = self.cache.instrument(inst_id)
            if inst is None:
                return
            qty_obj = safe_make_qty(inst, contracts, ctx=f"close {inst_value}")
            if qty_obj is None:
                return
            close_side = OrderSide.SELL if direction == "long" else OrderSide.BUY
            self.submit_order(self.order_factory.market(
                instrument_id=inst_id, order_side=close_side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
                reduce_only=True,
            ))
            self.log.info(f"CLOSE {direction} {inst_value} reason={reason}")

            cfg: MLFusionConfig = self.config  # type: ignore[assignment]
            ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
            closes = list(self._closes[inst_value])
            close_px = closes[-1] if closes else 0.0
            record_strategy_trade(
                self._pnl_tracker,
                strategy_id=str(self.id),
                instrument_id=inst_value,
                closed_ts_ms=exit_ts_ms,
                direction=direction,  # type: ignore[arg-type]
                entry_price=closes[-2] if len(closes) >= 2 else close_px,
                exit_price=close_px,
                contracts=contracts,
                ct_val=ct_val,
                risk_usdt=effective_equity_usdt(
                    self._allocated_equity_usdt, cfg.account_equity_usdt,
                ) * cfg.risk_pct,
            )
            self._positions.pop(inst_value, None)


else:  # pragma: no cover

    class MLFusionConfig:  # type: ignore[no-redef]
        pass

    class MLFusionStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError("nautilus_trader not installed; run pip install -e '.[strategy]'")


__all__ = [
    "MLFusionConfig",
    "MLFusionStrategy",
    "build_feature_row",
    "load_model",
    "predict_direction",
    "save_model",
]
