"""Тесты для pre_resolution_drift_study.py (Phase 5.3)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "research"))

from pre_resolution_drift_study import (  # noqa: E402
    INTEGRATE_THRESHOLD,
    REJECT_THRESHOLD,
    assign_drift_tier,
    compute_drift_metrics,
    correlation_breakdown,
    render_verdict,
    tier_breakdown,
)


class TestComputeDriftMetrics:
    def test_below_min_trades_returns_none(self):
        df = pd.DataFrame({
            "timestamp": list(range(50)),
            "price": [0.5] * 50,
        })
        assert compute_drift_metrics(df, 100) is None

    def test_no_trades_before_window_returns_none(self):
        # Все trades в окне 24h перед resolve — нет mp_24h_before
        resolve_ts = 100000
        # Все trades > resolve - 24h
        df = pd.DataFrame({
            "timestamp": list(range(resolve_ts - 1000, resolve_ts - 900)),
            "price": [0.5] * 100,
        })
        # Pad
        df = pd.concat([df] * 2, ignore_index=True)
        assert compute_drift_metrics(df, resolve_ts) is None

    def test_normal_drift_up(self):
        resolve_ts = 100000
        # 100 старых trades по 0.4, 100 в окне по 0.6
        old_ts = list(range(0, 100))
        new_ts = list(range(resolve_ts - 1000, resolve_ts - 900))
        df = pd.DataFrame({
            "timestamp": old_ts + new_ts,
            "price": [0.4] * 100 + [0.6] * 100,
        })
        m = compute_drift_metrics(df, resolve_ts)
        assert m is not None
        # mp_before — последняя сделка ≤ window_start (= resolve - 86400 ≈ 13600)
        # из старых ts 0-99: last = 99, price=0.4
        assert m["mp_24h_before"] == 0.4
        # mp_at_resolve — последняя сделка ≤ resolve_ts (последняя из new_ts)
        assert m["mp_at_resolve"] == 0.6
        # drift = 0.6 - 0.4 = 0.2
        assert m["drift_24h"] == pytest.approx(0.2)

    def test_normal_drift_down(self):
        resolve_ts = 100000
        old_ts = list(range(0, 100))
        new_ts = list(range(resolve_ts - 1000, resolve_ts - 900))
        df = pd.DataFrame({
            "timestamp": old_ts + new_ts,
            "price": [0.7] * 100 + [0.3] * 100,
        })
        m = compute_drift_metrics(df, resolve_ts)
        assert m is not None
        assert m["drift_24h"] == pytest.approx(-0.4)


class TestAssignDriftTier:
    def test_strong_down(self):
        assert assign_drift_tier(-0.50) == "strong_down"
        assert assign_drift_tier(-0.11) == "strong_down"

    def test_mid_down(self):
        assert assign_drift_tier(-0.10) == "mid_down"
        assert assign_drift_tier(-0.04) == "mid_down"

    def test_flat(self):
        assert assign_drift_tier(-0.03) == "flat"
        assert assign_drift_tier(0.0) == "flat"
        assert assign_drift_tier(0.029) == "flat"

    def test_mid_up(self):
        assert assign_drift_tier(0.03) == "mid_up"
        assert assign_drift_tier(0.099) == "mid_up"

    def test_strong_up(self):
        assert assign_drift_tier(0.10) == "strong_up"
        assert assign_drift_tier(0.50) == "strong_up"

    def test_above_max(self):
        assert assign_drift_tier(2.0) is None


class TestCorrelationBreakdown:
    def test_few_samples_skipped(self):
        df = pd.DataFrame({"drift_24h": [0.1, 0.2], "outcome": [1, 0]})
        result = correlation_breakdown(df)
        assert "skip_reason" in result

    def test_perfect_positive_correlation(self):
        np.random.seed(42)
        n = 100
        x = np.random.uniform(-0.5, 0.5, n)
        # outcome = 1 if drift > 0 else 0 (perfect predictor)
        df = pd.DataFrame({
            "drift_24h": x,
            "outcome": (x > 0).astype(int),
        })
        result = correlation_breakdown(df)
        assert result["pearson"] > 0.7  # high but not 1 (binary outcome)


class TestTierBreakdown:
    def test_returns_5_tiers(self):
        np.random.seed(42)
        n = 500
        df = pd.DataFrame({
            "drift_24h": np.random.uniform(-0.5, 0.5, n),
            "outcome": np.random.randint(0, 2, n),
        })
        tiers = tier_breakdown(df)
        assert len(tiers) == 5
        tier_names = [t["tier"] for t in tiers]
        assert tier_names == ["strong_down", "mid_down", "flat", "mid_up", "strong_up"]


class TestRenderVerdict:
    def test_integrate_when_corr_above_threshold(self):
        corr = {"abs_pearson": INTEGRATE_THRESHOLD + 0.05, "n": 1000}
        tiers = []
        verdict = render_verdict(corr, tiers)
        assert "INTEGRATE" in verdict

    def test_reject_when_corr_below_and_no_tier_diff(self):
        corr = {"abs_pearson": REJECT_THRESHOLD - 0.01, "n": 1000}
        tiers = [
            {"tier": "strong_up", "n": 100, "outcome_yes_pct": 0.5},
            {"tier": "strong_down", "n": 100, "outcome_yes_pct": 0.55},  # diff < 0.10
        ]
        verdict = render_verdict(corr, tiers)
        assert "REJECT" in verdict

    def test_borderline_when_in_between(self):
        corr = {"abs_pearson": (INTEGRATE_THRESHOLD + REJECT_THRESHOLD) / 2, "n": 1000}
        tiers = []
        verdict = render_verdict(corr, tiers)
        assert "BORDERLINE" in verdict
