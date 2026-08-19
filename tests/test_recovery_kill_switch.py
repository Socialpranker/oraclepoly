"""Tests для recovery state machine — Phase 1 Stage 8.

Pure logic tests: state transitions, audit log format, atomic write.
Interactive Click handlers (step_*) НЕ тестируем здесь — это manual
UI flow.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.recovery import kill_switch as ks


def test_states_order_is_canonical():
    """7 states в правильном порядке."""
    assert ks.STATES == (
        "TRIGGERED",
        "ACKNOWLEDGED",
        "DIAGNOSING",
        "FIXING",
        "VERIFYING",
        "SOAKING",
        "RESUMED",
    )


def test_transition_forward_allowed():
    """TRIGGERED → ACKNOWLEDGED → DIAGNOSING валидно."""
    state = {"state": "TRIGGERED", "audit_log": []}
    ks._transition(state, "ACKNOWLEDGED")
    assert state["state"] == "ACKNOWLEDGED"
    ks._transition(state, "DIAGNOSING")
    assert state["state"] == "DIAGNOSING"


def test_transition_skip_state_blocked():
    """TRIGGERED → DIAGNOSING (skip ACKNOWLEDGED) — не allowed."""
    state = {"state": "TRIGGERED", "audit_log": []}
    with pytest.raises(ValueError, match="Invalid transition"):
        ks._transition(state, "DIAGNOSING")


def test_transition_fixing_back_to_diagnose_allowed():
    """FIXING → DIAGNOSING (retry on failed fix) валидно."""
    state = {"state": "FIXING", "audit_log": []}
    ks._transition(state, "DIAGNOSING")
    assert state["state"] == "DIAGNOSING"


def test_transition_backward_blocked():
    """SOAKING → VERIFYING (regression) — не allowed."""
    state = {"state": "SOAKING", "audit_log": []}
    with pytest.raises(ValueError):
        ks._transition(state, "VERIFYING")


def test_transition_writes_audit_log():
    """Каждая transition appends event в audit_log."""
    state = {"state": "TRIGGERED", "audit_log": []}
    ks._transition(state, "ACKNOWLEDGED")
    assert len(state["audit_log"]) == 1
    assert state["audit_log"][0]["event"] == "state_transition"
    assert state["audit_log"][0]["from_state"] == "TRIGGERED"
    assert state["audit_log"][0]["to_state"] == "ACKNOWLEDGED"


def test_append_audit_includes_timestamp():
    """audit log записи имеют ISO timestamp."""
    state: dict = {}
    ks._append_audit(state, "test_event", key="value")
    assert "audit_log" in state
    assert len(state["audit_log"]) == 1
    entry = state["audit_log"][0]
    assert entry["event"] == "test_event"
    assert entry["key"] == "value"
    assert "ts" in entry  # ISO timestamp


def test_atomic_save_and_load(tmp_path: Path):
    """_save_state + _load_state round-trip без corruption."""
    with patch.object(ks, "STATE_DIR", tmp_path):
        state = {"recovery_id": "abc123", "state": "DIAGNOSING", "audit_log": []}
        ks._save_state("abc123", state)
        loaded = ks._load_state("abc123")
        assert loaded == state


def test_atomic_save_overwrite(tmp_path: Path):
    """Re-save переписывает state."""
    with patch.object(ks, "STATE_DIR", tmp_path):
        ks._save_state("xyz", {"state": "TRIGGERED"})
        ks._save_state("xyz", {"state": "ACKNOWLEDGED"})
        loaded = ks._load_state("xyz")
        assert loaded["state"] == "ACKNOWLEDGED"


def test_load_missing_state_returns_empty(tmp_path: Path):
    """No state file → empty dict (для resume validation)."""
    with patch.object(ks, "STATE_DIR", tmp_path):
        loaded = ks._load_state("nonexistent")
        assert loaded == {}


def test_step_handlers_cover_all_states():
    """Каждый non-terminal state имеет handler."""
    for st in ("TRIGGERED", "ACKNOWLEDGED", "DIAGNOSING", "FIXING", "VERIFYING", "SOAKING"):
        assert st in ks.STEP_HANDLERS, f"missing handler для {st}"
    # RESUMED terminal — no handler.
    assert "RESUMED" not in ks.STEP_HANDLERS


def test_state_dir_uses_tmp_safe():
    """STATE_DIR в /tmp — гарантированно writable."""
    assert str(ks.STATE_DIR).startswith("/tmp")
