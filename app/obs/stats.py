"""小样本统计口径（app 与 eval 共用一份，避免两套 P50/P95 算法吵架）。"""

from __future__ import annotations

import math
from typing import Sequence


def percentile(values: Sequence[float], q: float) -> float | None:
    """最近秩百分位：样本为空返回 None，否则返回真实存在的某个值。

    与 numpy 的 linear 插值不同——小样本（评测集 40 条、metrics 窗口 200 次）
    用最近秩更诚实：报出来的数一定真的出现过，不构造一个不存在的中间值。
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    idx = max(0, min(len(ordered) - 1, math.ceil(q / 100.0 * len(ordered)) - 1))
    return round(ordered[idx], 1)


def describe(values: Sequence[float]) -> dict[str, float | None]:
    """一次给出 P50 / P95 / max，报告里常用三件套。"""
    return {
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": round(max(values), 1) if values else None,
    }
