"""Тесты для vpin_correlation_study.py (Phase 5.1)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "research"))

from vpin_correlation_study import (  # noqa: E402
    INTEGRATE_THRESHOLD,
    REJECT_THRESHOLD,
    compute_market_vpin,
    correlation_breakdown,
    market_price_at_resolve,
    render_verdict,
)


class TestComputeMarketVpin:
    def test_below_min_returns_none(self):
        # Меньше 2 * VPIN_BUCKETS = 20 trades
        df = pd.DataFrame({
            "timestamp": list(range(10)),
            "price": [0.5] * 10,
            "usd_amount": [100.0] * 10,
            "side": ["YES"] * 10,
        })
        assert compute_market_vpin(df) is None

    def test_balanced_buys_sells_low_vpin(self):
        # Идеально сбалансированные buy/sell → VPIN близко к 0
        n = 100
        df = pd.DataFrame({
            "timestamp": list(range(n)),
            "price": [0.5] * n,
            "usd_amount": [100.0] * n,
            "side": ["YES" if i % 2 == 0 else "NO" for i in range(n)],
        })
        vpin = compute_market_vpin(df)
        assert vpin is not None
        assert vpin < 0.5  # балансированный → low VPIN

    def test_all_buys_high_vpin(self):
        # Все buys → VPIN близко к 1
        n = 100
        df = pd.DataFrame({
            "timestamp": list(range(n)),
            "price": [0.5] * n,
            "usd_amount": [100.0] * n,
            "side": ["YES"] * n,
        })
        vpin = compute_market_vpin(df)
        assert vpin is not None
        assert vpin > 0.9


class TestMarketPriceAtResolve:
    def test_returns_last_trade_price(self):
        df = pd.DataFrame({
            "timestamp": [100, 200, 300],
            "price": [0.4, 0.5, 0.6],
        })
        assert market_price_at_resolve(df, 350) == 0.6

    def test_returns_last_before_resolve(self):
        df = pd.DataFrame({
            "timestamp": [100, 200, 300, 400],
            "price": [0.4, 0.5, 0.6, 0.7],
        })
        assert market_price_at_resolve(df, 250) == 0.5

    def test_no_trades_before_resolve(self):
        df = pd.DataFrame({
            "timestamp": [500, 600],
            "price": [0.5, 0.6],
        })
        assert market_price_at_resolve(df, 100) is None


class TestCorrelationBreakdown:
    def test_too_few_samples_skipped(self):
        df = pd.DataFrame({
            "vpin": [0.1, 0.2, 0.3],
            "accuracy_proxy": [0.5, 0.6, 0.7],
        })
        result = correlation_breakdown(df)
        assert "skip_reason" in result

    def test_perfect_positive_correlation(self):
        n = 100
        np.random.seed(42)
        x = np.random.uniform(0, 1, n)
        df = pd.DataFrame({
            "vpin": x,
            "accuracy_proxy": x,  # identical → corr = 1.0
        })
        result = correlation_breakdown(df)
        assert result["pearson"] > 0.99
        assert result["spearman"] > 0.99

    def test_no_correlation(self):
        np.random.seed(42)
        n = 1000
        df = pd.DataFrame({
            "vpin": np.random.uniform(0, 1, n),
            "accuracy_proxy": np.random.uniform(0, 1, n),
        })
        result = correlation_breakdown(df)
        # Должна быть низкая корреляция (random)
        assert abs(result["pearson"]) < 0.1

    def test_per_category_breakdown(self):
        np.random.seed(42)
        n = 200
        x = np.random.uniform(0, 1, n)
        df = pd.DataFrame({
            "vpin": x,
            "accuracy_proxy": x,
            "category": ["Sports"] * 100 + ["Politics"] * 100,
        })
        result = correlation_breakdown(df)
        assert "per_category" in result
        assert "Sports" in result["per_category"]
        assert "Politics" in result["per_category"]


class TestRenderVerdict:
    def test_integrate_when_corr_above_threshold(self):
        corr = {"abs_pearson": INTEGRATE_THRESHOLD + 0.05, "n": 1000}
        verdict = render_verdict(corr)
        assert "INTEGRATE" in verdict

    def test_reject_when_corr_below_threshold(self):
        corr = {"abs_pearson": REJECT_THRESHOLD - 0.01, "n": 1000}
        verdict = render_verdict(corr)
        assert "REJECT" in verdict

    def test_borderline_when_in_between(self):
        corr = {"abs_pearson": (INTEGRATE_THRESHOLD + REJECT_THRESHOLD) / 2, "n": 1000}
        verdict = render_verdict(corr)
        assert "BORDERLINE" in verdict

    def test_skipped_when_skip_reason(self):
        corr = {"skip_reason": "n<30"}
        verdict = render_verdict(corr)
        assert "SKIPPED" in verdict
