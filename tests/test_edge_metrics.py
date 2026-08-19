"""Tests для app.analytics.edge_metrics."""

from __future__ import annotations

import pytest

from app.analytics.edge_metrics import WindowMetrics, compute_window_metrics, synthetic_pnl


def test_synthetic_pnl_win_returns_one_minus_entry_price():
    assert synthetic_pnl(0.30, "WIN") == pytest.approx(0.70)


def test_synthetic_pnl_loss_returns_negative_entry_price():
    assert synthetic_pnl(0.30, "LOSS") == pytest.approx(-0.30)


def test_synthetic_pnl_invalid_outcome_raises():
    with pytest.raises(ValueError, match="unresolved outcome"):
        synthetic_pnl(0.30, "EXPIRED")


def test_compute_window_metrics_empty_returns_zero_n():
    result = compute_window_metrics([], [])

    assert isinstance(result, WindowMetrics)
    assert result.n == 0
    assert result.wins == 0
    assert result.wilson_lower == pytest.approx(0.0)
    assert result.wilson_upper == pytest.approx(1.0)
    assert result.sortino is None


def test_compute_window_metrics_all_wins_positive_sortino():
    outcomes = ["WIN"] * 10
    pnls = [0.5] * 10

    result = compute_window_metrics(outcomes, pnls)

    assert result.n == 10
    assert result.wins == 10
    assert result.wilson_lower > 0.5
    # Нет downside (все pnl положительны) → +inf, как compute_sortino в detector_metrics.py
    assert result.sortino == float("inf")


def test_compute_window_metrics_all_losses_gives_negative_sortino():
    outcomes = ["LOSS"] * 10
    pnls = [-0.3] * 10

    result = compute_window_metrics(outcomes, pnls)

    assert result.n == 10
    assert result.wins == 0
    assert result.wilson_upper < 0.5
    # mean(R)=-0.3, sum_neg_sq=10*0.09=0.9>0 → downside_vol=sqrt(0.9/10)=0.3
    # sortino = -0.3 / 0.3 = -1.0 (не None — min(p,0) не зануляет уже-отрицательные pnl)
    assert result.sortino == pytest.approx(-1.0)


def test_compute_window_metrics_all_zero_pnl_gives_none_sortino():
    outcomes = ["LOSS"] * 10
    pnls = [0.0] * 10

    result = compute_window_metrics(outcomes, pnls)

    # mean(R)=0, sum_neg_sq=0 → degenerate: mean not > 0 → None (не +inf)
    assert result.sortino is None


def test_compute_window_metrics_mixed_computes_sortino_from_downside():
    outcomes = ["WIN", "LOSS", "WIN", "LOSS"]
    pnls = [0.6, -0.4, 0.5, -0.2]

    result = compute_window_metrics(outcomes, pnls)

    assert result.n == 4
    assert result.wins == 2
    mean_r = sum(pnls) / len(pnls)
    sum_neg_sq = sum(min(p, 0.0) ** 2 for p in pnls)
    downside_vol = (sum_neg_sq / len(pnls)) ** 0.5
    expected_sortino = mean_r / downside_vol
    assert result.sortino == pytest.approx(expected_sortino, rel=1e-6)


def test_compute_window_metrics_mismatched_lengths_raises():
    with pytest.raises(ValueError, match="same length"):
        compute_window_metrics(["WIN", "LOSS"], [0.5])
