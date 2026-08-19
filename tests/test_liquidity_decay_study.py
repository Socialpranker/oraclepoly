"""Тесты для liquidity_decay_study.py (Phase 5.2)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "research"))

from liquidity_decay_study import (  # noqa: E402
    INTEGRATE_THRESHOLD,
    REJECT_THRESHOLD,
    assign_decay_tier,
    compute_decay_metrics,
    correlation_breakdown,
    render_verdict,
    tier_breakdown,
)


class TestComputeDecayMetrics:
    def test_below_min_trades_returns_none(self):
        df = pd.DataFrame({
            "timestamp": list(range(50)),
            "price": [0.5] * 50,
            "usd_amount": [100.0] * 50,
        })
        assert compute_decay_metrics(df, 1000) is None

    def test_zero_volume_returns_none(self):
        df = pd.DataFrame({
            "timestamp": list(range(150)),
            "price": [0.5] * 150,
            "usd_amount": [0.0] * 150,
        })
        assert compute_decay_metrics(df, 1000) is None

    def test_normal_market_returns_metrics(self):
        # 200 trades с нормальным объёмом
        n = 200
        # 100 trades старее 24h, 100 в окне 24h
        ts_old = list(range(100))
        ts_recent = list(range(1000 - 100, 1000))
        df = pd.DataFrame({
            "timestamp": ts_old + ts_recent,
            "price": [0.5] * n,
            "usd_amount": [100.0] * n,
        })
        # resolve_ts = 1000, window 24h = [1000-86400, 1000] → весь окне 0+
        # все 200 trades должны попасть в last_24h_volume (т.к. все ts < 1000)
        m = compute_decay_metrics(df, resolve_ts=1000)
        assert m is not None
        assert m["total_volume"] == 20000.0
        assert m["last_24h_volume"] == 20000.0  # все попадают в окно
        assert m["decay_pct"] == 1.0  # 100% decay

    def test_partial_decay(self):
        # 100 trades старее 24h (timestamp ~ 0-100)
        # 100 trades в окне (timestamp ~ resolve - 1000)
        resolve_ts = 100000
        old_ts = list(range(0, 100))   # старые
        new_ts = list(range(resolve_ts - 1000, resolve_ts - 900))  # в окне
        df = pd.DataFrame({
            "timestamp": old_ts + new_ts,
            "price": [0.5] * 200,
            "usd_amount": [100.0] * 200,
        })
        m = compute_decay_metrics(df, resolve_ts=resolve_ts)
        assert m is not None
        # window_start = resolve_ts - 86400 ≈ 13600 → старые trades (0-100) НЕ в окне
        assert m["total_volume"] == 20000.0
        # new_ts > 13600 → попадают
        assert m["last_24h_volume"] == 10000.0  # только новые 100 trades
        assert m["decay_pct"] == 0.5

    def test_market_price_at_resolve(self):
        df = pd.DataFrame({
            "timestamp": [100, 200, 300, 400],
            "price": [0.4, 0.5, 0.6, 0.7],
            "usd_amount": [100.0, 100.0, 100.0, 100.0],
        })
        # Pad to 100 для прохода MIN_TRADES_PER_MARKET
        df = pd.concat([df] * 25, ignore_index=True)
        m = compute_decay_metrics(df, resolve_ts=350)
        assert m is not None
        assert m["mp_at_resolve"] == 0.6  # last trade ≤ 350


class TestAssignDecayTier:
    def test_low_tier(self):
        assert assign_decay_tier(0.0) == "low"
        assert assign_decay_tier(0.29) == "low"

    def test_mid_tier(self):
        assert assign_decay_tier(0.30) == "mid"
        assert assign_decay_tier(0.59) == "mid"

    def test_high_tier(self):
        assert assign_decay_tier(0.60) == "high"
        assert assign_decay_tier(0.79) == "high"

    def test_extreme_tier(self):
        assert assign_decay_tier(0.80) == "extreme"
        assert assign_decay_tier(1.0) == "extreme"

    def test_above_max(self):
        # >1.01 — out of bounds
        assert assign_decay_tier(2.0) is None


class TestCorrelationBreakdown:
    def test_few_samples_skipped(self):
        df = pd.DataFrame({"decay_pct": [0.1, 0.5], "accuracy_proxy": [0.5, 0.6]})
        result = correlation_breakdown(df)
        assert "skip_reason" in result

    def test_perfect_positive_correlation(self):
        np.random.seed(42)
        n = 100
        x = np.random.uniform(0, 1, n)
        df = pd.DataFrame({"decay_pct": x, "accuracy_proxy": x})
        result = correlation_breakdown(df)
        assert result["pearson"] > 0.99

    def test_per_category_breakdown(self):
        np.random.seed(42)
        n = 200
        x = np.random.uniform(0, 1, n)
        df = pd.DataFrame({
            "decay_pct": x,
            "accuracy_proxy": x,
            "category": ["Sports"] * 100 + ["Politics"] * 100,
        })
        result = correlation_breakdown(df)
        assert "Sports" in result["per_category"]
        assert "Politics" in result["per_category"]


class TestTierBreakdown:
    def test_returns_4_tiers(self):
        np.random.seed(42)
        n = 400
        df = pd.DataFrame({
            "decay_pct": np.random.uniform(0, 1, n),
            "accuracy_proxy": np.random.uniform(0, 1, n),
            "outcome": np.random.randint(0, 2, n),
        })
        tiers = tier_breakdown(df)
        assert len(tiers) == 4
        tier_names = [t["tier"] for t in tiers]
        assert tier_names == ["low", "mid", "high", "extreme"]

    def test_skip_when_few_samples(self):
        df = pd.DataFrame({
            "decay_pct": [0.85, 0.86, 0.87],  # 3 in extreme
            "accuracy_proxy": [0.5, 0.6, 0.7],
            "outcome": [1, 0, 1],
        })
        tiers = tier_breakdown(df)
        # extreme tier должен быть skipped (n<10)
        extreme = next(t for t in tiers if t["tier"] == "extreme")
        assert extreme["skip"] is True


class TestRenderVerdict:
    def test_integrate_when_corr_above_threshold(self):
        corr = {"abs_pearson": INTEGRATE_THRESHOLD + 0.05, "n": 1000}
        tiers = []
        verdict = render_verdict(corr, tiers)
        assert "INTEGRATE" in verdict

    def test_reject_when_corr_below_and_no_tier_diff(self):
        corr = {"abs_pearson": REJECT_THRESHOLD - 0.01, "n": 1000}
        tiers = [
            {"tier": "low", "n": 100, "accuracy_mean": 0.5},
            {"tier": "extreme", "n": 100, "accuracy_mean": 0.51},  # diff < 0.05
        ]
        verdict = render_verdict(corr, tiers)
        assert "REJECT" in verdict

    def test_borderline_when_in_between(self):
        corr = {"abs_pearson": (INTEGRATE_THRESHOLD + REJECT_THRESHOLD) / 2, "n": 1000}
        tiers = []
        verdict = render_verdict(corr, tiers)
        assert "BORDERLINE" in verdict
