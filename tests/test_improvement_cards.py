"""Tests для app.analytics.improvement_cards (чистый слой, без DB)."""

from __future__ import annotations

import pytest

from app.analytics.improvement_cards import (
    TierConfig,
    build_title,
    classify_source_kind,
    compute_action_rate,
    ev_per_hour,
    extract_leak_usd_from_metrics,
    initial_lifecycle_stage,
    next_attention_status,
    resolve_mechanism_ref,
)


@pytest.mark.parametrize(
    "run_name,expected",
    [
        ("adverse_selection_monitor:STANDALONE_V2_LGB×World & Economy", "adverse_selection"),
        ("edge_decay_monitor:detector:D38_resolution_lag", "edge_decay"),
        ("counterfactual_case1_hold_vs_tpsl", "counterfactual"),
        ("some_other_job:foo", None),
    ],
)
def test_classify_source_kind(run_name, expected):
    assert classify_source_kind(run_name) == expected


def test_extract_leak_usd_adverse_selection_reads_net_pnl():
    metrics = {"net_pnl": -15973.95, "detector": "STANDALONE_V2_LGB"}
    assert extract_leak_usd_from_metrics("adverse_selection", metrics) == pytest.approx(-15973.95)


def test_extract_leak_usd_edge_decay_returns_none():
    # edge_decay metrics не содержит $ — досчёт делает джоба своим SQL
    metrics = {"recent": {"n": 1700, "wins": 1555}}
    assert extract_leak_usd_from_metrics("edge_decay", metrics) is None


def test_extract_leak_usd_counterfactual_reads_pnl_diff_when_present():
    assert extract_leak_usd_from_metrics("counterfactual", {"pnl_diff_usd": -420.0}) == pytest.approx(-420.0)


def test_extract_leak_usd_counterfactual_none_when_absent():
    assert extract_leak_usd_from_metrics("counterfactual", {"foo": 1}) is None


def test_extract_leak_usd_counterfactual_fallback_tpsl_minus_hold():
    # Реальный прод-скрипт пишет tpsl_pnl/hold_to_resolution_pnl, не pnl_diff_usd.
    metrics = {"tpsl_pnl": -500.0, "hold_to_resolution_pnl": 120.0}
    assert extract_leak_usd_from_metrics("counterfactual", metrics) == pytest.approx(-620.0)


def test_extract_leak_usd_counterfactual_fallback_none_when_only_one_key():
    assert extract_leak_usd_from_metrics("counterfactual", {"tpsl_pnl": -500.0}) is None


def test_extract_leak_usd_counterfactual_fallback_none_when_policy_ahead():
    # Реальные прод-значения (id=7): as-is TP/SL впереди hold-to-resolution →
    # delta = 3912.51 - 2573.15 = +1339.36 (положительный) → это НЕ утечка.
    metrics = {"tpsl_pnl": 3912.51, "hold_to_resolution_pnl": 2573.15}
    assert extract_leak_usd_from_metrics("counterfactual", metrics) is None


def test_ev_per_hour_basic():
    tier = TierConfig(p_fix_success=0.6, effort_hours=2.0)
    # |−15973.95| × 0.6 / 2 = 4792.185
    assert ev_per_hour(-15973.95, tier) == pytest.approx(4792.185)


def test_ev_per_hour_none_when_leak_none():
    assert ev_per_hour(None, TierConfig(0.6, 2.0)) is None


def test_ev_per_hour_none_when_hours_zero():
    assert ev_per_hour(-100.0, TierConfig(0.6, 0.0)) is None


def test_resolve_mechanism_ref_temperature_wins():
    ref = resolve_mechanism_ref("adverse_selection", "detector:D41", is_temperature=True)
    assert ref == "docs/knowledge/behavioral_catalog.md#temperature-bots"


def test_resolve_mechanism_ref_by_source_kind():
    assert resolve_mechanism_ref("edge_decay", "detector:D38", False).endswith("methods_playbook.md#changepoint")


def test_build_title_with_leak():
    title = build_title("adverse_selection", "detector:D36_obi_pressure×Sports", -547.0)
    assert "Adverse selection" in title and "$547" in title


def test_build_title_without_leak():
    assert "не измерена" in build_title("edge_decay", "detector:D38", None)


def test_initial_lifecycle_stage():
    assert initial_lifecycle_stage(-100.0) == "digest"
    assert initial_lifecycle_stage(None) == "shadow"


def test_next_attention_status_valid():
    assert next_attention_status("pending", "acted") == "acted"


def test_next_attention_status_invalid_raises():
    with pytest.raises(ValueError):
        next_attention_status("acted", "pending")


def test_compute_action_rate():
    assert compute_action_rate(0, 0) is None
    assert compute_action_rate(4, 1) == pytest.approx(0.25)
