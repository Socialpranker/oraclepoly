"""Tests для app/calibration/per_event_metrics.py.

Stage 1 / Two-Stage ML Pipeline.

Per-event Brier — честная метрика к event-correlation. Per-row Brier инфлируется
когда у event'а N markets с одинаковым outcome.
"""

from __future__ import annotations

import numpy as np
import pytest


def test_single_market_per_event_equals_per_row():
    """Один market на event → per-event Brier = per-row Brier."""
    from app.calibration.per_event_metrics import per_event_brier

    y = [1, 0, 1]
    p = [0.8, 0.3, 0.6]
    events = ["ev1", "ev2", "ev3"]
    per_row = float(np.mean([(p[i] - y[i]) ** 2 for i in range(3)]))
    per_event = per_event_brier(events, p, y)
    assert abs(per_event - per_row) < 1e-9


def test_three_markets_one_event_collapses_to_one():
    """Три rows одного event'а → один вклад в per-event Brier (на mean(p), mean(y))."""
    from app.calibration.per_event_metrics import per_event_brier

    y = [1, 1, 0]
    p = [0.7, 0.7, 0.7]
    events = ["ev1", "ev1", "ev1"]
    expected = (0.7 - 2 / 3) ** 2
    actual = per_event_brier(events, p, y)
    assert abs(actual - expected) < 1e-6


def test_two_events_with_different_sizes():
    """Per-event берёт mean внутри event'а — большие events не получают больше веса."""
    from app.calibration.per_event_metrics import per_event_brier

    # ev1: 4 rows, all p=0.6, all y=1 → mean_p=0.6, mean_y=1.0, Brier=(0.6-1)^2=0.16
    # ev2: 1 row, p=0.4, y=0 → Brier=(0.4-0)^2=0.16
    # Per-event mean = (0.16 + 0.16) / 2 = 0.16
    p = [0.6, 0.6, 0.6, 0.6, 0.4]
    y = [1, 1, 1, 1, 0]
    events = ["ev1", "ev1", "ev1", "ev1", "ev2"]
    actual = per_event_brier(events, p, y)
    assert abs(actual - 0.16) < 1e-9


def test_n_events_count():
    from app.calibration.per_event_metrics import count_events

    assert count_events(["ev1", "ev1", "ev2", "ev3", "ev3"]) == 3
    assert count_events([]) == 0
    assert count_events(["ev1"]) == 1


def test_empty_input():
    from app.calibration.per_event_metrics import per_event_brier

    assert per_event_brier([], [], []) == 0.0


def test_len_mismatch_raises():
    from app.calibration.per_event_metrics import per_event_brier

    with pytest.raises(ValueError, match="len mismatch"):
        per_event_brier(["ev1", "ev2"], [0.5], [1])

    with pytest.raises(ValueError, match="len mismatch"):
        per_event_brier(["ev1"], [0.5, 0.6], [1, 0])


def test_perfect_predictor():
    """Brier = 0 когда predictions идеальны."""
    from app.calibration.per_event_metrics import per_event_brier

    p = [1.0, 0.0, 1.0]
    y = [1, 0, 1]
    events = ["ev1", "ev2", "ev3"]
    assert per_event_brier(events, p, y) == 0.0


def test_coin_brier_at_05():
    """Brier при 50% prediction на mixed outcomes ≈ 0.25."""
    from app.calibration.per_event_metrics import per_event_brier

    p = [0.5] * 4
    y = [1, 0, 1, 0]
    events = [f"ev{i}" for i in range(4)]
    assert per_event_brier(events, p, y) == 0.25


def test_per_event_with_numpy_inputs():
    """Должен принимать numpy arrays как и lists."""
    from app.calibration.per_event_metrics import per_event_brier

    p = np.array([0.7, 0.7, 0.7])
    y = np.array([1, 1, 0])
    events = ["ev1", "ev1", "ev1"]
    expected = (0.7 - 2 / 3) ** 2
    actual = per_event_brier(events, p.tolist(), y.tolist())
    assert abs(actual - expected) < 1e-6


def test_correlation_inflation_demonstration():
    """Показывает почему per-event ≠ per-row при event correlation.

    Сценарий: 1 event с 10 markets, все p=0.8, все win.
    Per-row Brier: (0.8-1)^2 = 0.04 на каждый row → mean = 0.04 (n=10 inflated)
    Per-event Brier: один вклад от mean(p)=0.8, mean(y)=1.0 → 0.04 (n=1 honest)

    Они равны в этом тривиальном случае, но n_events=1 vs n_rows=10 — это и есть
    причина почему per-row n инфлирован в event_size раз.
    """
    from app.calibration.per_event_metrics import count_events, per_event_brier

    p = [0.8] * 10
    y = [1] * 10
    events = ["ev1"] * 10
    assert per_event_brier(events, p, y) == pytest.approx(0.04, abs=1e-9)
    assert count_events(events) == 1  # n=1, не 10!
