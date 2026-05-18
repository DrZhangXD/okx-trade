"""Regime 检测器 + RegimeGate 风控插件（M5.X）。

直觉
----
不同策略在不同市场状态下的边际差异巨大：
- ``xs_momentum`` 等 momentum 类策略在 trending（强趋势）市场赚钱；
  在 mean-reverting（震荡）市场亏钱。
- ``liq_reversal`` / ``ob_imbalance`` 等 reversal 类策略反过来：震荡市赚，趋势市亏。

给所有策略加一个共享的 ``RegimeDetector``，下单前由 ``RegimeGateCheck`` 按
``(state, strategy_kind)`` 查映射表缩仓，可显著降低错环境下的暴露。

两种实现：
- **RuleBasedRegimeDetector**（默认）：BTC 200d 均线 + 30d 实现波动率分位数判定。
  无外部依赖，逻辑透明，可解释。
- **HMMRegimeDetector**（可选）：2-state Gaussian HMM，用 hmmlearn。学术正统但
  状态语义不直观、易过拟合最近数据；需要 ``pip install hmmlearn``（optional dep）。

两者共享 ``RegimeDetectorProtocol``。``build_risk_manager`` 通过 ``regime_detector``
参数注入；多个策略共享同一个 detector 实例（``live_node`` 单点构造）。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from statistics import mean, stdev
from typing import Literal, Protocol, runtime_checkable

from .base import RiskCheck, RiskCheckResult, RiskIntent

RegimeState = Literal["trending", "mean_reverting", "neutral"]
StrategyKind = Literal["momentum", "reversal", "neutral"]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RegimeDetectorProtocol(Protocol):
    """所有 detector 必须实现的接口。"""

    def update(self, ts_ms: int, close: float) -> None:
        """喂一个新 BTC 1d bar 收盘价。``ts_ms`` 应单调递增。"""
        ...

    def current_state(self) -> RegimeState:
        """返回当前 regime。样本不足时返回 ``"neutral"``。"""
        ...


# ---------------------------------------------------------------------------
# Rule-based detector
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleBasedRegimeParams:
    sma_window: int = 200          # 200d 均线
    rv_window: int = 30            # 30d 实现波动率
    rv_history_days: int = 365     # rv 分位数样本窗口
    trending_rv_pct: float = 0.60  # rv < 60th percentile 才算 trending
    mean_reverting_rv_pct: float = 0.70  # rv > 70th percentile 算 mean-reverting
    min_history: int = 60          # 不到 60 根 → neutral


class RuleBasedRegimeDetector:
    """规则版 regime 判定。

    state = ``"trending"``       if ``close > SMA(200)`` AND ``rv_30d <= rv_60th_pct``
    state = ``"mean_reverting"`` if ``close < SMA(200)`` OR  ``rv_30d  > rv_70th_pct``
    state = ``"neutral"``        其他

    rv = annualized realized volatility = stdev(log returns over window) × sqrt(365)。
    """

    def __init__(self, params: RuleBasedRegimeParams | None = None) -> None:
        self.params = params or RuleBasedRegimeParams()
        self._closes: deque[float] = deque(maxlen=self.params.sma_window + self.params.rv_history_days + 5)
        self._last_ts_ms: int = 0
        # rv 历史样本，按时间序滚动
        self._rv_history: deque[float] = deque(maxlen=self.params.rv_history_days)

    def update(self, ts_ms: int, close: float) -> None:
        if close <= 0:
            return
        if ts_ms <= self._last_ts_ms:
            return
        self._closes.append(close)
        self._last_ts_ms = ts_ms
        # 增量算 rv：用最近 rv_window+1 个收盘价
        if len(self._closes) >= self.params.rv_window + 1:
            tail = list(self._closes)[-(self.params.rv_window + 1):]
            log_rets = [math.log(tail[i] / tail[i - 1]) for i in range(1, len(tail))]
            try:
                sd = stdev(log_rets)
            except Exception:
                sd = 0.0
            rv = sd * math.sqrt(365.0)
            self._rv_history.append(rv)

    def current_state(self) -> RegimeState:
        p = self.params
        if len(self._closes) < p.min_history:
            return "neutral"
        closes = list(self._closes)
        close_now = closes[-1]
        # SMA(min(sma_window, len))：样本不足时用全部
        sma_n = min(p.sma_window, len(closes))
        sma = sum(closes[-sma_n:]) / sma_n
        above_sma = close_now > sma

        rv_now = self._rv_history[-1] if self._rv_history else None
        if rv_now is None or len(self._rv_history) < 30:
            # rv 样本太少，只用 SMA 简单判
            return "trending" if above_sma else "mean_reverting"

        sorted_rv = sorted(self._rv_history)
        n = len(sorted_rv)
        rv_low = sorted_rv[int(n * p.trending_rv_pct)]
        rv_high = sorted_rv[int(n * p.mean_reverting_rv_pct)]

        if above_sma and rv_now <= rv_low:
            return "trending"
        if (not above_sma) or rv_now > rv_high:
            return "mean_reverting"
        return "neutral"


# ---------------------------------------------------------------------------
# HMM detector (optional)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HMMRegimeParams:
    min_history: int = 90
    retrain_interval_days: int = 30


class HMMRegimeDetector:
    """2-state Gaussian HMM detector（需要 hmmlearn）。

    feature 向量 = ``[log_return, log(realized_vol)]``。两个 hidden state，
    用 mean return 大者为 ``"trending"``、小者为 ``"mean_reverting"``。
    """

    def __init__(self, params: HMMRegimeParams | None = None) -> None:
        self.params = params or HMMRegimeParams()
        self._closes: deque[float] = deque(maxlen=400)
        self._last_ts_ms: int = 0
        self._model = None  # type: ignore[var-annotated]
        self._trending_state_idx: int | None = None
        self._last_state: RegimeState = "neutral"
        self._last_train_ms: int = 0

    def update(self, ts_ms: int, close: float) -> None:
        if close <= 0 or ts_ms <= self._last_ts_ms:
            return
        self._closes.append(close)
        self._last_ts_ms = ts_ms
        # 月度 retrain
        if (ts_ms - self._last_train_ms) >= self.params.retrain_interval_days * 86400_000:
            self._maybe_retrain()
            self._last_train_ms = ts_ms
        # 即时推断
        self._infer()

    def _features(self) -> list[list[float]] | None:
        closes = list(self._closes)
        if len(closes) < self.params.min_history:
            return None
        log_rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        # 滚动 30d 波动率
        feats: list[list[float]] = []
        window = 30
        for i in range(window, len(log_rets)):
            r = log_rets[i]
            sub = log_rets[i - window:i]
            try:
                rv = stdev(sub) * math.sqrt(365.0)
            except Exception:
                rv = 1e-4
            feats.append([r, math.log(max(rv, 1e-6))])
        return feats if feats else None

    def _maybe_retrain(self) -> None:
        try:
            from hmmlearn import hmm  # type: ignore[import-not-found]
        except ImportError:
            return
        feats = self._features()
        if not feats or len(feats) < self.params.min_history:
            return
        try:
            import numpy as np
            X = np.asarray(feats, dtype=float)
            model = hmm.GaussianHMM(n_components=2, covariance_type="diag", n_iter=50, random_state=42)
            model.fit(X)
            means = model.means_[:, 0]
            self._trending_state_idx = int(means.argmax())
            self._model = model
        except Exception:
            # HMM 训练失败不阻塞策略，回退到 neutral
            self._model = None
            self._trending_state_idx = None

    def _infer(self) -> None:
        if self._model is None or self._trending_state_idx is None:
            self._last_state = "neutral"
            return
        feats = self._features()
        if not feats:
            self._last_state = "neutral"
            return
        try:
            import numpy as np
            X = np.asarray(feats, dtype=float)
            states = self._model.predict(X)
            cur = int(states[-1])
            self._last_state = "trending" if cur == self._trending_state_idx else "mean_reverting"
        except Exception:
            self._last_state = "neutral"

    def current_state(self) -> RegimeState:
        return self._last_state


# ---------------------------------------------------------------------------
# Risk check
# ---------------------------------------------------------------------------


# 默认映射表（plan 同款）：
#   state \ kind   momentum  reversal  neutral
#   trending       1.0       0.4       1.0
#   mean_reverting 0.4       1.0       1.0
#   neutral        0.7       0.7       1.0
DEFAULT_SCALE_TABLE: dict[tuple[RegimeState, StrategyKind], float] = {
    ("trending", "momentum"): 1.0,
    ("trending", "reversal"): 0.4,
    ("trending", "neutral"): 1.0,
    ("mean_reverting", "momentum"): 0.4,
    ("mean_reverting", "reversal"): 1.0,
    ("mean_reverting", "neutral"): 1.0,
    ("neutral", "momentum"): 0.7,
    ("neutral", "reversal"): 0.7,
    ("neutral", "neutral"): 1.0,
}


class RegimeGateCheck(RiskCheck):
    """按 (regime_state, strategy_kind) 缩仓。"""

    name = "regime_gate"

    def __init__(
        self,
        detector: RegimeDetectorProtocol,
        strategy_kind: StrategyKind,
        *,
        scale_table: dict[tuple[RegimeState, StrategyKind], float] | None = None,
    ) -> None:
        self.detector = detector
        self.strategy_kind: StrategyKind = strategy_kind
        self.scale_table = scale_table or DEFAULT_SCALE_TABLE

    def check(self, intent: RiskIntent) -> RiskCheckResult:
        state = self.detector.current_state()
        scale = self.scale_table.get((state, self.strategy_kind), 1.0)
        if scale >= 1.0:
            return RiskCheckResult.approve(intent.size, check_name=self.name)
        if scale <= 0.0:
            return RiskCheckResult.reject(
                f"regime={state} kind={self.strategy_kind} scale=0", check_name=self.name,
            )
        new_size = intent.size * scale
        return RiskCheckResult.scale(
            new_size,
            reason=f"regime={state} kind={self.strategy_kind} scale={scale:.2f}",
            check_name=self.name,
        )


__all__ = [
    "DEFAULT_SCALE_TABLE",
    "HMMRegimeDetector",
    "HMMRegimeParams",
    "RegimeDetectorProtocol",
    "RegimeGateCheck",
    "RegimeState",
    "RuleBasedRegimeDetector",
    "RuleBasedRegimeParams",
    "StrategyKind",
]
