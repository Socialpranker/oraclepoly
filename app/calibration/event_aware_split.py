"""Event-aware splits для предотвращения data leakage между train/holdout.

Stage 1 / Two-Stage ML Pipeline.

Markets одного event'а ВСЕГДА должны быть в одной части — иначе калибровка
видит почти-дубликаты (одинаковые outcomes, потому что events резолвятся одним
фактом для всех markets) и даёт инфлированный holdout Brier.

Используется в:
- ml_honest_baseline_fit (Stage 1) — переобучение монолита с правильным split
- ml_market_picker_fit (Stage 3) — Layer B обучение с GroupKFold по event_id
"""

from __future__ import annotations

import random
from collections.abc import Sequence


def event_aware_holdout_split(
    event_ids: Sequence[str],
    holdout_fraction: float = 0.20,
    seed: int = 42,
) -> tuple[list[int], list[int]]:
    """Random-style split, но по events.

    Все markets одного event'а → в один bucket (train или holdout).

    Args:
        event_ids: per-row event_id (длина = N rows). Один event может встречаться
            много раз (по одному разу на каждый market внутри event'а).
        holdout_fraction: доля events в holdout (0.20 = 20%). Доля rows в holdout
            может слегка отличаться, потому что events имеют разный размер.
        seed: для детерминизма.

    Returns:
        (train_idx, holdout_idx) — индексы в исходном массиве event_ids.
    """
    if not event_ids:
        return [], []
    unique_events = sorted(set(event_ids))
    rng = random.Random(seed)
    rng.shuffle(unique_events)
    cut = int(len(unique_events) * (1.0 - holdout_fraction))
    train_events = set(unique_events[:cut])
    holdout_events = set(unique_events[cut:])

    train_idx = [i for i, ev in enumerate(event_ids) if ev in train_events]
    holdout_idx = [i for i, ev in enumerate(event_ids) if ev in holdout_events]
    return train_idx, holdout_idx


def time_based_event_holdout_split(
    event_ids: Sequence[str],
    event_timestamps: Sequence[float],
    holdout_fraction: float = 0.20,
) -> tuple[list[int], list[int]]:
    """Time-based split с boundary по events (не по rows).

    Per-row timestamp агрегируется в per-event min(timestamp). Затем events
    сортируются по этому min, последние `holdout_fraction` идут в holdout.

    Цель: walk-forward валидация без data leakage. Старые events → train,
    новые → holdout.

    Args:
        event_ids: per-row event_id.
        event_timestamps: per-row timestamp (например epoch seconds).
        holdout_fraction: доля events в holdout (по counts, не по rows).

    Returns:
        (train_idx, holdout_idx).
    """
    if not event_ids:
        return [], []
    if len(event_ids) != len(event_timestamps):
        raise ValueError(f"len mismatch: events={len(event_ids)} ts={len(event_timestamps)}")

    event_min_ts: dict[str, float] = {}
    for ev, ts in zip(event_ids, event_timestamps, strict=True):
        if ev not in event_min_ts or ts < event_min_ts[ev]:
            event_min_ts[ev] = ts

    sorted_events = sorted(event_min_ts.keys(), key=lambda e: event_min_ts[e])
    cut = int(len(sorted_events) * (1.0 - holdout_fraction))
    train_events = set(sorted_events[:cut])
    holdout_events = set(sorted_events[cut:])

    train_idx = [i for i, ev in enumerate(event_ids) if ev in train_events]
    holdout_idx = [i for i, ev in enumerate(event_ids) if ev in holdout_events]
    return train_idx, holdout_idx
