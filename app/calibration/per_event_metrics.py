"""Per-event Brier — метрика, честная к event-correlation.

Stage 1 / Two-Stage ML Pipeline.

Per-row Brier инфлируется когда у event'а N markets с одинаковым outcome:
все N rows вносят похожий вклад → метрика выглядит "хорошей", но это один факт.
Per-event Brier агрегирует mean(p), mean(y) внутри event'а и считает Brier
на N_events парах (вместо N_rows).

Используется во всех Stages 1-5.5 для honest evaluation.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def per_event_brier(
    event_ids: Sequence[str],
    pred_prob: Sequence[float],
    outcome: Sequence[int],
) -> float:
    """Per-event Brier score.

    Args:
        event_ids: per-row event_id (длина = N rows).
        pred_prob: per-row P[WIN] от модели.
        outcome: per-row 1=WIN, 0=LOSS.

    Returns:
        Mean squared error на агрегированных per-event парах (mean(p), mean(y)).
        0.0 при пустом input.
    """
    if not event_ids:
        return 0.0
    n = len(event_ids)
    if n != len(pred_prob) or n != len(outcome):
        raise ValueError(f"len mismatch: events={n} preds={len(pred_prob)} outcomes={len(outcome)}")

    p_arr = np.asarray(list(pred_prob), dtype=float)
    y_arr = np.asarray(list(outcome), dtype=float)

    sum_p: dict[str, float] = {}
    sum_y: dict[str, float] = {}
    n_per: dict[str, int] = {}
    for i, ev in enumerate(event_ids):
        sum_p[ev] = sum_p.get(ev, 0.0) + float(p_arr[i])
        sum_y[ev] = sum_y.get(ev, 0.0) + float(y_arr[i])
        n_per[ev] = n_per.get(ev, 0) + 1

    mean_p = np.array([sum_p[ev] / n_per[ev] for ev in sum_p])
    mean_y = np.array([sum_y[ev] / n_per[ev] for ev in sum_p])
    return float(np.mean((mean_p - mean_y) ** 2))


def count_events(event_ids: Sequence[str]) -> int:
    """Количество уникальных events (= честный n для per-event метрик)."""
    return len(set(event_ids))
