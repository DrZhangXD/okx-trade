"""ml_fusion 单测：特征聚合 + predict_direction + model save/load。"""
from __future__ import annotations

import pickle
import random
from pathlib import Path

import pytest

from okx_trade.strategies._features import (
    FeatureRow,
    correlation,
    momentum,
    percentile_rank,
    realized_vol,
)
from okx_trade.strategies.ml_fusion import (
    build_feature_row,
    load_model,
    predict_direction,
    save_model,
)


class TestFeatureFunctions:
    def test_momentum_positive(self) -> None:
        # closes 100 → 110，lookback=1 → 10%
        assert momentum([100.0, 110.0], 1) == pytest.approx(0.10)

    def test_momentum_negative(self) -> None:
        assert momentum([110.0, 100.0], 1) == pytest.approx(-0.0909, abs=1e-3)

    def test_momentum_insufficient(self) -> None:
        assert momentum([100.0], 1) == 0.0
        assert momentum([100.0, 110.0], 5) == 0.0

    def test_realized_vol(self) -> None:
        # 价格不变 → vol = 0
        assert realized_vol([100.0] * 60, window=30) == 0.0
        # 真实波动 → vol > 0
        rng = random.Random(42)
        closes = [100.0]
        for _ in range(60):
            closes.append(closes[-1] * (1 + rng.gauss(0, 0.01)))
        rv = realized_vol(closes, window=30)
        assert rv > 0

    def test_percentile_rank(self) -> None:
        history = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        assert percentile_rank(5.0, history) == 0.5
        assert percentile_rank(10.0, history) == 1.0
        assert percentile_rank(0.0, history) == 0.0

    def test_percentile_rank_insufficient(self) -> None:
        # history < 5 → 中位 0.5
        assert percentile_rank(100.0, [1.0]) == 0.5

    def test_correlation_perfect(self) -> None:
        assert correlation([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(1.0)

    def test_correlation_neg(self) -> None:
        assert correlation([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)

    def test_correlation_unequal(self) -> None:
        # 取后 3 个
        c = correlation([1, 2, 3, 4, 5], [99, 100, 1, 2, 3])
        assert -1.0 <= c <= 1.0


class TestFeatureRow:
    def test_serialization(self) -> None:
        f = FeatureRow(ts_ms=1, inst_id="BTC-USDT-SWAP.OKX", momentum_1d=0.05)
        lst = f.as_list()
        names = FeatureRow.feature_names()
        assert len(lst) == len(names)
        # 顺序：momentum_1d 是第一个
        assert lst[0] == 0.05
        assert names[0] == "momentum_1d"

    def test_defaults_zero(self) -> None:
        f = FeatureRow(ts_ms=1, inst_id="ETH-USDT-SWAP.OKX")
        # 所有可选特征默认 0
        assert all(x == 0 for x in f.as_list())


class TestBuildFeatureRow:
    def test_with_closes_only(self) -> None:
        # 给 200 个 close（足够算 1d/3d/7d momentum + 30d RV）
        closes = [100.0 + i * 0.1 for i in range(200)]
        feat = build_feature_row(
            inst_id="BTC-USDT-SWAP.OKX", ts_ms=1_000_000_000,
            closes=closes,
        )
        # 单调递增 → momentum_1d > 0
        assert feat.momentum_1d > 0
        assert feat.momentum_3d > 0
        assert feat.momentum_7d > 0
        # 单调递增低噪 → RV ≈ 0
        assert feat.rv_30d < 0.01

    def test_with_btc_corr(self) -> None:
        rng = random.Random(7)
        # asset 与 btc 同步：correlation 应 ≈ 1
        btc = [100.0]
        asset = [50.0]
        for _ in range(60):
            r = rng.gauss(0, 0.01)
            btc.append(btc[-1] * (1 + r))
            asset.append(asset[-1] * (1 + r))
        feat = build_feature_row(
            inst_id="ETH", ts_ms=1, closes=asset, btc_closes=btc,
        )
        assert feat.btc_corr_30d > 0.9


class TestPredictDirection:
    def test_long_above_threshold(self) -> None:
        assert predict_direction(0.7, 0.55) == "long"

    def test_short_below_one_minus_threshold(self) -> None:
        assert predict_direction(0.3, 0.55) == "short"

    def test_none_in_band(self) -> None:
        assert predict_direction(0.5, 0.55) is None
        assert predict_direction(0.54, 0.55) is None


class TestModelPersistence:
    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        # 简单 dict 当模型 stub
        model = {"weights": [1, 2, 3], "version": "v1"}
        path = tmp_path / "model.pkl"
        save_model(model, path)
        loaded = load_model(path)
        assert loaded == model

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_model(tmp_path / "nonexistent.pkl") is None
