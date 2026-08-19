"""Tests для app.analytics.changepoint — формальный CUSUM changepoint detector."""

from __future__ import annotations

from app.analytics.changepoint import ChangepointResult, CusumDetector


def test_no_shift_does_not_trigger():
    baseline = [1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 1.0, 1.02, 0.98, 1.0] * 3
    recent = [1.0, 0.98, 1.02, 1.0, 0.99]

    result = CusumDetector().detect(baseline, recent)

    assert isinstance(result, ChangepointResult)
    assert result.triggered is False
    assert result.direction is None


def test_clear_downward_shift_triggers_below():
    baseline = [1.0] * 30
    # baseline std=0 → CusumDetector fallback std=1.0 (см. Step 3 реализации)
    recent = [-5.0] * 10

    result = CusumDetector().detect(baseline, recent)

    assert result.triggered is True
    assert result.direction == "below"
    assert result.cusum_statistic > result.threshold


def test_clear_upward_shift_triggers_above():
    baseline = [1.0] * 30
    recent = [7.0] * 10

    result = CusumDetector().detect(baseline, recent)

    assert result.triggered is True
    assert result.direction == "above"


def test_empty_baseline_does_not_crash():
    result = CusumDetector().detect([], [1.0, 2.0, 3.0])

    assert result.triggered is False
    assert result.direction is None


def test_empty_recent_does_not_crash():
    result = CusumDetector().detect([1.0, 2.0, 3.0], [])

    assert result.triggered is False
    assert result.direction is None


def test_custom_k_and_threshold_respected():
    baseline = [1.0] * 30
    recent = [1.3] * 10  # small shift — default threshold=5.0 не триггерит

    default_result = CusumDetector().detect(baseline, recent)
    sensitive_result = CusumDetector(k=0.1, threshold=0.5).detect(baseline, recent)

    assert default_result.triggered is False
    assert sensitive_result.triggered is True
