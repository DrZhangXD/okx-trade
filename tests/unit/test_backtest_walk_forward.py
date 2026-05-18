"""walk_forward 切分 + 评估单测。"""
from __future__ import annotations

import pytest

from okx_trade.backtest.walk_forward import (
    ClassificationMetrics,
    evaluate_binary,
    evaluate_regression,
    walk_forward_splits,
)


class TestWalkForwardSplits:
    def test_basic_splits(self) -> None:
        # n=100, train=60, test=10 → 应该有 4 个 fold（开始位置 0, 10, 20, 30）
        splits = list(walk_forward_splits(100, 60, 10))
        assert len(splits) == 4
        assert splits[0][0] == range(0, 60)
        assert splits[0][1] == range(60, 70)
        assert splits[1][0] == range(10, 70)
        assert splits[1][1] == range(70, 80)

    def test_custom_step(self) -> None:
        # step=5 → 更密 fold
        splits = list(walk_forward_splits(100, 60, 10, step=5))
        # 起始: 0, 5, 10, 15, 20, 25, 30 (30+60+10=100)
        assert len(splits) == 7

    def test_no_splits_when_data_too_small(self) -> None:
        # n=50 < train(60)+test(10)
        assert list(walk_forward_splits(50, 60, 10)) == []

    def test_zero_sized(self) -> None:
        assert list(walk_forward_splits(0, 1, 1)) == []
        assert list(walk_forward_splits(100, 0, 1)) == []
        assert list(walk_forward_splits(100, 1, 0)) == []

    def test_no_overlap_in_default_step(self) -> None:
        # 默认 step=test_size，连续两个 fold 的 test ranges 不重叠
        splits = list(walk_forward_splits(200, 50, 10))
        for i in range(len(splits) - 1):
            test_a = splits[i][1]
            test_b = splits[i + 1][1]
            # test_b 起点 == test_a 终点
            assert test_b.start == test_a.stop


class TestEvaluateBinary:
    def test_perfect_prediction(self) -> None:
        m = evaluate_binary([1, 0, 1, 1, 0], [1, 0, 1, 1, 0])
        assert m.accuracy == 1.0
        assert m.precision == 1.0
        assert m.recall == 1.0
        assert m.f1 == 1.0

    def test_all_wrong(self) -> None:
        m = evaluate_binary([1, 0, 1, 0], [0, 1, 0, 1])
        assert m.accuracy == 0.0

    def test_mixed(self) -> None:
        # 4 个 1，预测对 3 个；1 个 false positive
        y_true = [1, 1, 1, 1, 0, 0]
        y_pred = [1, 1, 1, 0, 1, 0]
        m = evaluate_binary(y_true, y_pred)
        # tp=3, fp=1, fn=1, tn=1
        assert m.accuracy == pytest.approx(4 / 6)
        assert m.precision == pytest.approx(3 / 4)
        assert m.recall == pytest.approx(3 / 4)

    def test_empty_returns_zero(self) -> None:
        m = evaluate_binary([], [])
        assert m.accuracy == 0
        assert m.f1 == 0


class TestEvaluateRegression:
    def test_perfect_prediction(self) -> None:
        r = evaluate_regression([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        assert r["rmse"] == 0.0
        assert r["mae"] == 0.0
        assert r["r2"] == pytest.approx(1.0)

    def test_constant_prediction(self) -> None:
        # 预测一直是均值 → r2 = 0
        r = evaluate_regression([1.0, 2.0, 3.0], [2.0, 2.0, 2.0])
        assert r["r2"] == pytest.approx(0.0)

    def test_mae_vs_rmse(self) -> None:
        # 大误差时 RMSE > MAE
        r = evaluate_regression([0.0, 0.0, 0.0], [1.0, 1.0, 10.0])
        assert r["rmse"] > r["mae"]
