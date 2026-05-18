"""Walk-forward 切分 + 评估工具（ml_fusion 用，但任何策略也可调）。

为什么自己写不用 sklearn？
- 项目主依赖避免 sklearn（与 NT 一样能跑就不引）
- 切分逻辑只需 ~30 行
"""
from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass


def walk_forward_splits(
    n_samples: int,
    train_size: int,
    test_size: int,
    step: int | None = None,
) -> Iterator[tuple[range, range]]:
    """生成 ``(train_idx_range, test_idx_range)`` 序列。

    经典 walk-forward：
        | train (60) | test (7) |
                    | train (60) | test (7) |
                                | train (60) | test (7) |
                                            ...
    Args:
        n_samples: 数据总长度。
        train_size: 每折训练窗口（要求 >= 1）。
        test_size: 每折测试窗口（要求 >= 1）。
        step: 滚动步长（默认 = test_size，即非重叠 test）。

    Yields:
        ``(train_range, test_range)``，indices 是 0-based 半开。
    """
    if train_size <= 0 or test_size <= 0 or n_samples <= 0:
        return
    if step is None:
        step = test_size
    start = 0
    while start + train_size + test_size <= n_samples:
        train = range(start, start + train_size)
        test = range(start + train_size, start + train_size + test_size)
        yield train, test
        start += step


@dataclass(frozen=True, slots=True)
class ClassificationMetrics:
    """二分类评估结果。"""

    accuracy: float
    precision: float
    recall: float
    f1: float
    n_samples: int
    n_positive_true: int
    n_positive_pred: int


def evaluate_binary(
    y_true: Sequence[int],
    y_pred: Sequence[int],
) -> ClassificationMetrics:
    """y_true / y_pred 0 或 1（不 robust to bad input；只用于自家 ML 评估）。"""
    n = len(y_true)
    if n == 0 or n != len(y_pred):
        return ClassificationMetrics(0, 0, 0, 0, 0, 0, 0)
    tp = sum(1 for i in range(n) if y_true[i] == 1 and y_pred[i] == 1)
    fp = sum(1 for i in range(n) if y_true[i] == 0 and y_pred[i] == 1)
    fn = sum(1 for i in range(n) if y_true[i] == 1 and y_pred[i] == 0)
    tn = sum(1 for i in range(n) if y_true[i] == 0 and y_pred[i] == 0)
    acc = (tp + tn) / n
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    n_pos_t = tp + fn
    n_pos_p = tp + fp
    return ClassificationMetrics(acc, prec, rec, f1, n, n_pos_t, n_pos_p)


def evaluate_regression(
    y_true: Sequence[float],
    y_pred: Sequence[float],
) -> dict[str, float]:
    """RMSE + MAE + R²。"""
    n = len(y_true)
    if n == 0 or n != len(y_pred):
        return {"rmse": 0.0, "mae": 0.0, "r2": 0.0}
    errors = [y_pred[i] - y_true[i] for i in range(n)]
    mse = sum(e * e for e in errors) / n
    mae = sum(abs(e) for e in errors) / n
    mean_y = sum(y_true) / n
    ss_tot = sum((y - mean_y) ** 2 for y in y_true)
    ss_res = sum(e * e for e in errors)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {"rmse": math.sqrt(mse), "mae": mae, "r2": r2}


__all__ = [
    "ClassificationMetrics",
    "evaluate_binary",
    "evaluate_regression",
    "walk_forward_splits",
]
