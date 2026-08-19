"""Tests для app.analytics.decision_expectation (Phase 2 C4)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.analytics.decision_expectation import (
    IMPROVED,
    UNCHANGED,
    WORSENED,
    build_expectation,
    classify_numeric,
    classify_verdict,
    resolve_metric,
)

# ── resolve_metric: три формы scope ───────────────────────────────────────────


def test_resolve_metric_detector_gives_roi():
    spec = resolve_metric("detector:D3_cross_mkt_arb")
    assert spec.kind == "roi"
    assert spec.entity == "D3_cross_mkt_arb"


def test_resolve_metric_detector_with_category_uses_first_segment():
    # adverse-selection scope: detector:X:category:Y — _DRAWDOWN_SQL агрегирует
    # по детектору, категорийного разреза в нём нет (осознанное огрубление, спека §2).
    spec = resolve_metric("detector:D38_weather:category:World & Economy")
    assert spec.kind == "roi"
    assert spec.entity == "D38_weather"


def test_resolve_metric_dq_audit_gives_verdict():
    spec = resolve_metric("policy:dq_audit")
    assert spec.kind == "verdict"
    assert spec.entity == "dq_audit_daily"


def test_resolve_metric_ips_has_no_source():
    # IPS-гейт живёт внутри policy-engine и своего agent_runs не пишет —
    # сверять не с чем, закрываем с явной причиной (спека §2, поправка №2).
    spec = resolve_metric("policy:ips")
    assert spec.kind == "none"
    assert spec.reason is not None


def test_resolve_metric_unknown_prefix_has_no_source():
    spec = resolve_metric("something:else")
    assert spec.kind == "none"


# ── classify_numeric: границы полосы ──────────────────────────────────────────


def test_classify_numeric_above_band_is_improved():
    assert classify_numeric(-0.33, -0.31) == IMPROVED


def test_classify_numeric_below_band_is_worsened():
    assert classify_numeric(-0.33, -0.35) == WORSENED


def test_classify_numeric_inside_band_is_unchanged():
    assert classify_numeric(-0.33, -0.332) == UNCHANGED


def test_classify_numeric_exactly_on_band_edge_is_unchanged():
    # Граница включительно: |delta| == band → шум, не движение (strict-сравнение
    # снаружи полосы, как в тесте порогов C1).
    assert classify_numeric(-0.33, -0.32) == UNCHANGED
    assert classify_numeric(-0.33, -0.34) == UNCHANGED


# ── classify_verdict: упорядочивание RED < AMBER < GREEN ──────────────────────


def test_classify_verdict_red_to_green_is_improved():
    assert classify_verdict("RED", "GREEN") == IMPROVED


def test_classify_verdict_amber_to_red_is_worsened():
    assert classify_verdict("AMBER", "RED") == WORSENED


def test_classify_verdict_same_is_unchanged():
    assert classify_verdict("AMBER", "AMBER") == UNCHANGED


# ── build_expectation ─────────────────────────────────────────────────────────


def test_build_expectation_due_date_is_now_plus_window():
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    spec = resolve_metric("detector:D3_cross_mkt_arb")
    exp = build_expectation(spec, -0.33, now, window_days=30)
    assert exp["metric"] == "roi"
    assert exp["entity"] == "D3_cross_mkt_arb"
    assert exp["baseline"] == -0.33
    assert exp["expectation"] == "roi_not_below_baseline"
    assert exp["due_date"] == datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def test_build_expectation_verdict_metric_uses_verdict_improves():
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    spec = resolve_metric("policy:dq_audit")
    exp = build_expectation(spec, "AMBER", now, window_days=30)
    assert exp["metric"] == "verdict"
    assert exp["expectation"] == "verdict_improves"
    assert exp["baseline"] == "AMBER"
