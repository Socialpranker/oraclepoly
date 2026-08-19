"""Tests для drill scenarios catalogue — Phase 1 Stage 8.

Pure-data tests: scenario count, layer/severity filters, unique IDs.
"""

from __future__ import annotations

from scripts.drill.scenarios import (
    ALL_SCENARIOS,
    CASCADE_SCENARIOS,
    CHAOS_SCENARIOS,
    FUNCTIONAL_SCENARIOS,
    get_scenario,
    scenarios_by_layer,
    scenarios_by_severity,
)


def test_total_count_27():
    """Research §Findings.2: 27 scenarios = 12+8+7."""
    assert len(FUNCTIONAL_SCENARIOS) == 12
    assert len(CASCADE_SCENARIOS) == 8
    assert len(CHAOS_SCENARIOS) == 7
    assert len(ALL_SCENARIOS) == 27


def test_unique_ids():
    """Каждый scenario имеет unique id."""
    ids = [s.id for s in ALL_SCENARIOS]
    assert len(ids) == len(set(ids))


def test_id_naming_convention():
    """F=Functional, X=Cascade, C=Chaos."""
    for s in FUNCTIONAL_SCENARIOS:
        assert s.id.startswith("F"), f"functional id should start with F: {s.id}"
    for s in CASCADE_SCENARIOS:
        assert s.id.startswith("X"), f"cascade id should start with X: {s.id}"
    for s in CHAOS_SCENARIOS:
        assert s.id.startswith("C"), f"chaos id should start with C: {s.id}"


def test_get_scenario_lookup():
    """get_scenario(id) returns matching scenario."""
    s = get_scenario("F01")
    assert s is not None
    assert s.id == "F01"
    assert "Kill switch" in s.title


def test_get_scenario_unknown_returns_none():
    assert get_scenario("Z99") is None


def test_scenarios_by_layer_filter():
    """L3_daemon_db должен содержать F04, F05, F10, F11, F12 минимум."""
    daemon = scenarios_by_layer("L3_daemon_db")
    ids = {s.id for s in daemon}
    assert "F04" in ids  # reconciliation
    assert "F05" in ids  # exposure
    assert "F10" in ids  # daily PnL hard
    assert "F11" in ids  # anomaly
    assert "F12" in ids  # DB CHECK


def test_scenarios_by_severity_s1_count():
    """S1 critical — должно быть несколько scenarios для money path."""
    s1 = scenarios_by_severity("S1")
    assert len(s1) >= 5, f"expected >=5 S1, got {len(s1)}"


def test_all_have_hypothesis_pre_trigger_expected():
    """Каждый scenario должен иметь все required fields filled."""
    for s in ALL_SCENARIOS:
        assert s.hypothesis, f"{s.id}: missing hypothesis"
        assert s.pre_state, f"{s.id}: missing pre_state"
        assert s.trigger, f"{s.id}: missing trigger"
        assert s.expected, f"{s.id}: missing expected"


def test_severity_only_known_values():
    """Severity = S1/S2/S3/S4 only."""
    for s in ALL_SCENARIOS:
        assert s.severity in ("S1", "S2", "S3", "S4"), (
            f"{s.id}: bad severity {s.severity}"
        )


def test_layer_only_known_values():
    """Layer = L1_safety/L2_risk_engine/L3_daemon_db/cross_layer/chaos."""
    for s in ALL_SCENARIOS:
        assert s.layer in (
            "L1_safety", "L2_risk_engine", "L3_daemon_db", "cross_layer", "chaos"
        ), f"{s.id}: bad layer {s.layer}"


def test_chaos_scenarios_all_chaos_layer():
    """Все chaos scenarios должны иметь layer='chaos'."""
    for s in CHAOS_SCENARIOS:
        assert s.layer == "chaos"


def test_money_path_scenarios_are_s1():
    """Critical money-path scenarios должны быть S1 severity.

    F01 kill_switch, F04 reconcile drift, F05 exposure cap, F06 main dead,
    F07 daemon dead, F10 daily hard, X01 latent code, X02 race, C02 redis,
    C03 polymarket 5xx.
    """
    money_path_ids = {"F01", "F04", "F05", "F06", "F07", "F10", "X01", "X02", "C02", "C03"}
    for id_ in money_path_ids:
        s = get_scenario(id_)
        assert s is not None, f"missing: {id_}"
        assert s.severity == "S1", f"{id_}: expected S1, got {s.severity}"
