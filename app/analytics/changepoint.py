"""Формальный CUSUM changepoint detector — app/jobs/edge_decay_monitor.py.

Policy-подобный протокол (ChangepointDetector) по аналогии с
app/analytics/counterfactual.py::Policy — на будущее, если появятся другие
алгоритмы (BOCPD и т.д.), без registry по имени (Variant A: Protocol,
меньше indirection пока реализация одна).

Двусторонний CUSUM (Page 1954, стандартное изложение — Montgomery, Introduction
to Statistical Quality Control): стандартизует recent относительно baseline
mean/std, аккумулирует S+/S- по recent-ряду, triggered если max отклонение
превышает threshold (в единицах std). k/threshold — дефолты k=0.5σ,
threshold=5.0σ — учебные значения (Montgomery), НЕ откалиброваны на
исторические decay-эпизоды проекта (нет данных для калибровки на 2026-07-21).

direction возвращает "above"/"below" (recent выше/ниже baseline в сыром
числовом смысле) — НЕ "degradation"/"improvement". Семантика "хуже/лучше"
зависит от того, что за ряд подан (Wilson win-rate: ниже=хуже; но для
какого-то будущего ряда могло быть наоборот) — интерпретация на стороне
вызывающего кода (app/jobs/edge_decay_monitor.py), не этого модуля.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ChangepointResult:
    triggered: bool
    cusum_statistic: float
    threshold: float
    direction: str | None  # "above" | "below" | None


class ChangepointDetector(Protocol):
    def detect(self, baseline: list[float], recent: list[float]) -> ChangepointResult: ...


class CusumDetector:
    """Двусторонний CUSUM на стандартизованном ряде recent относительно baseline.

    Пересчитывается с нуля на каждом вызове — без персистентного состояния
    между запусками (как решено в design doc).
    """

    def __init__(self, k: float = 0.5, threshold: float = 5.0) -> None:
        self._k = k
        self._threshold = threshold

    def detect(self, baseline: list[float], recent: list[float]) -> ChangepointResult:
        if not baseline or not recent:
            return ChangepointResult(triggered=False, cusum_statistic=0.0, threshold=self._threshold, direction=None)

        baseline_mean = statistics.fmean(baseline)
        baseline_std = statistics.pstdev(baseline) if len(baseline) > 1 else 0.0
        if baseline_std == 0.0:
            baseline_std = 1.0

        s_pos = 0.0
        s_neg = 0.0
        max_pos = 0.0
        min_neg = 0.0
        for value in recent:
            z = (value - baseline_mean) / baseline_std
            s_pos = max(0.0, s_pos + z - self._k)
            s_neg = min(0.0, s_neg + z + self._k)
            max_pos = max(max_pos, s_pos)
            min_neg = min(min_neg, s_neg)

        cusum_statistic = max(max_pos, abs(min_neg))
        triggered = cusum_statistic > self._threshold

        direction: str | None = None
        if triggered:
            direction = "above" if max_pos >= abs(min_neg) else "below"

        return ChangepointResult(
            triggered=triggered,
            cusum_statistic=cusum_statistic,
            threshold=self._threshold,
            direction=direction,
        )
