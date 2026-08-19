"""Tests for whale_sweep_service.apply_sweep_adjustment."""

from __future__ import annotations

from app.services.whale_sweep_service import (
    MIN_SWEEP_VOLUME_USD,
    MIN_TRADES_FOR_SWEEP,
    SWEEP_WINDOW_SEC,
    SweepState,
    apply_sweep_adjustment,
)


def _make_sweep(direction: str = "YES", n: int = 3, vol: float = 5000.0, mult: float = 1.15) -> SweepState:
    return SweepState(
        market_id="0xabc",
        direction=direction,
        n_sweeps=n,
        total_volume_usd=vol,
        last_sweep_age_sec=30.0,
        confidence_multiplier=mult,
    )


def test_constants_match_research():
    """Sweep thresholds (relaxed for our feed: ≥2 trades, 60s window)."""
    assert MIN_TRADES_FOR_SWEEP == 2
    assert SWEEP_WINDOW_SEC == 60
    assert MIN_SWEEP_VOLUME_USD == 1000.0


def test_no_sweep_returns_base_confidence():
    """Нет active sweep → base_confidence unchanged."""
    adj, reason = apply_sweep_adjustment(side="YES", base_confidence=0.60, sweep=None)
    assert adj == 0.60
    assert reason == "no_sweep"


def test_sweep_direction_none_treated_as_no_sweep():
    """SweepState с direction=None → no_sweep."""
    sweep = SweepState(
        market_id="0xabc",
        direction=None,
        n_sweeps=2,
        total_volume_usd=2000,
        last_sweep_age_sec=100,
        confidence_multiplier=1.0,
    )
    adj, reason = apply_sweep_adjustment(side="YES", base_confidence=0.60, sweep=sweep)
    assert adj == 0.60
    assert reason == "no_sweep"


def test_sweep_confirms_yes_boosts():
    """YES signal + YES sweep → confidence boosted."""
    sweep = _make_sweep(direction="YES", mult=1.30)
    adj, reason = apply_sweep_adjustment(side="YES", base_confidence=0.60, sweep=sweep)
    assert reason == "sweep_confirms"
    assert adj == 0.60 * 1.30


def test_sweep_confirms_no_boosts():
    """NO signal + NO sweep → confidence boosted."""
    sweep = _make_sweep(direction="NO", mult=1.15)
    adj, reason = apply_sweep_adjustment(side="NO", base_confidence=0.50, sweep=sweep)
    assert reason == "sweep_confirms"
    assert adj == 0.50 * 1.15


def test_sweep_confirms_caps_at_one():
    """Boost не может превысить 1.0."""
    sweep = _make_sweep(direction="YES", mult=1.30)
    adj, reason = apply_sweep_adjustment(side="YES", base_confidence=0.85, sweep=sweep)
    assert adj == 1.0  # 0.85 × 1.30 = 1.105 → cap


def test_sweep_contra_rejects():
    """YES signal но NO sweep → REJECT (return 0)."""
    sweep = _make_sweep(direction="NO")
    adj, reason = apply_sweep_adjustment(side="YES", base_confidence=0.70, sweep=sweep)
    assert reason == "sweep_contra"
    assert adj == 0.0


def test_sweep_contra_no_direction():
    """NO signal но YES sweep → REJECT."""
    sweep = _make_sweep(direction="YES")
    adj, reason = apply_sweep_adjustment(side="NO", base_confidence=0.70, sweep=sweep)
    assert reason == "sweep_contra"
    assert adj == 0.0


def test_sweep_state_immutable():
    """SweepState is frozen dataclass."""
    sweep = _make_sweep()
    try:
        sweep.n_sweeps = 99  # type: ignore[misc]
        raise AssertionError("Expected frozen dataclass")
    except AttributeError:
        pass  # frozen=True works
