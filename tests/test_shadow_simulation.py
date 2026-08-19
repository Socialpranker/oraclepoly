"""Unit tests для shadow TP/SL simulation."""

from __future__ import annotations

import pytest

from app.services.shadow_simulation import (
    ROUND_TRIP_FEE_PCT,
    ShadowSimResult,
    _signal_payout_pct_at,
    aggregate_sim_results,
    simulate_signal,
)

# ─── PnL formula sanity ──────────────────────────────────────────────────────


def test_no_side_pnl_positive_when_yes_drops():
    """NO side @ entry 0.20 (cost 0.80), price → 0.10 (cost 0.90).
    PnL = (0.90 - 0.80) / 0.80 × 100 = +12.5%."""
    pnl = _signal_payout_pct_at(entry_yes=0.20, current_yes=0.10, side="NO")
    assert pnl == pytest.approx(12.5, abs=0.01)


def test_no_side_pnl_negative_when_yes_rises():
    """NO @ 0.20 (cost 0.80), price → 0.40 (cost 0.60).
    PnL = (0.60 - 0.80) / 0.80 × 100 = -25%."""
    pnl = _signal_payout_pct_at(entry_yes=0.20, current_yes=0.40, side="NO")
    assert pnl == pytest.approx(-25.0, abs=0.01)


def test_yes_side_pnl_positive_when_yes_rises():
    """YES @ 0.30, price → 0.40. PnL = (0.40-0.30)/0.30 × 100 = +33.3%."""
    pnl = _signal_payout_pct_at(entry_yes=0.30, current_yes=0.40, side="YES")
    assert pnl == pytest.approx(33.33, abs=0.01)


# ─── simulate_signal: TP/SL trigger paths ────────────────────────────────────


def test_tp_fires_on_first_snapshot_above_threshold():
    """Generic detector, default TP +30%. Price path crosses TP — exit take_profit."""
    # NO @ entry 0.40 (cost 0.60), TP fires когда cost ≥ 0.60 × 1.30 = 0.78
    # → yes_price ≤ 0.22
    path = [
        {"yes_price": 0.40, "captured_at": "t1"},
        {"yes_price": 0.30, "captured_at": "t2"},  # PnL +16.7%, не TP
        {"yes_price": 0.20, "captured_at": "t3"},  # PnL +33%, TP fired
    ]
    res = simulate_signal(
        detector="Generic_Test",
        signal_id="sig1",
        side="NO",
        entry_yes_price=0.40,
        price_path=path,
        resolved=True,
        outcome="WIN",
        realized_roi_net=50.0,  # в % (не используется — TP срабатывает раньше)
    )
    assert res.exit_reason == "take_profit"
    # sim_pnl = TP - fees (30 - 4 = 26)
    assert res.sim_pnl_pct == pytest.approx(26.0)
    assert res.exit_yes_price == 0.20


def test_sl_fires_when_price_drops_against_position():
    """NO @ 0.20 (cost 0.80), default SL -20% fires когда cost ≤ 0.64
    → yes_price ≥ 0.36."""
    path = [
        {"yes_price": 0.20, "captured_at": "t1"},
        {"yes_price": 0.30, "captured_at": "t2"},  # PnL -12.5%, не SL
        {"yes_price": 0.40, "captured_at": "t3"},  # PnL -25%, SL fired
    ]
    res = simulate_signal(
        detector="Generic_Test",
        signal_id="sig2",
        side="NO",
        entry_yes_price=0.20,
        price_path=path,
        resolved=True,
        outcome="LOSS",
        realized_roi_net=-100.0,  # в % (не используется — SL срабатывает раньше)
    )
    assert res.exit_reason == "stop_loss"
    # sim_pnl = SL - fees (-20 - 4 = -24)
    assert res.sim_pnl_pct == pytest.approx(-24.0)


def test_per_detector_overrides_used():
    """D38 has wide TP=200%, SL=-80% via _DETECTOR_OVERRIDES.
    На обычный move TP не сработает — пойдёт к hold-to-resolution."""
    path = [
        {"yes_price": 0.10, "captured_at": "t1"},
        {"yes_price": 0.30, "captured_at": "t2"},  # PnL -22% — не SL -80%
    ]
    res = simulate_signal(
        detector="D38_resolution_lag",
        signal_id="d38_sig",
        side="NO",
        entry_yes_price=0.10,
        price_path=path,
        resolved=True,
        outcome="WIN",
        realized_roi_net=85.0,  # в процентах (как хранит БД)
    )
    # D38 is hold-to-resolution → exit=resolution даже если TP/SL params wide
    assert res.exit_reason == "resolution"
    assert res.is_hold_to_resolution is True
    # sim_pnl = realized_roi_net (уже в %, без домножения)
    assert res.sim_pnl_pct == pytest.approx(85.0)
    assert res.tp_pct == 200.0
    assert res.sl_pct == -80.0


def test_hold_to_resolution_detector_skips_tpsl():
    """D38 в HOLD_TO_RESOLUTION_DETECTORS — даже большой move не trigger TP/SL,
    идёт к resolution."""
    # Большой positive move — для NO side cost растёт с 0.95 до 0.99
    # Default TP fires при +30%, но D38 hold-to-resolution.
    path = [
        {"yes_price": 0.05, "captured_at": "t1"},
        {"yes_price": 0.01, "captured_at": "t2"},  # +83% от cost 0.95
    ]
    res = simulate_signal(
        detector="D38_resolution_lag",
        signal_id="d38_sig",
        side="NO",
        entry_yes_price=0.05,
        price_path=path,
        resolved=True,
        outcome="WIN",
        realized_roi_net=4.0,  # в процентах (как хранит БД)
    )
    assert res.is_hold_to_resolution is True
    assert res.exit_reason == "resolution"
    # sim_pnl = realized_roi_net (уже в %, не TP threshold)
    assert res.sim_pnl_pct == pytest.approx(4.0)


# ─── simulate_signal: resolution / expired paths ─────────────────────────────


def test_resolution_path_uses_realized_roi_net():
    """Нет TP/SL trigger + resolved=true → exit=resolution, sim_pnl = realized_roi_net (в %)."""
    path = [
        {"yes_price": 0.40, "captured_at": "t1"},
        {"yes_price": 0.35, "captured_at": "t2"},  # PnL +20%, ниже default TP +30%
    ]
    res = simulate_signal(
        detector="Generic_Test",
        signal_id="sig3",
        side="NO",
        entry_yes_price=0.40,
        price_path=path,
        resolved=True,
        outcome="WIN",
        realized_roi_net=66.0,  # в процентах (как хранит БД)
    )
    assert res.exit_reason == "resolution"
    assert res.sim_pnl_pct == pytest.approx(66.0)


def test_expired_outcome_zero_pnl():
    """EXPIRED outcome → sim_pnl = 0."""
    res = simulate_signal(
        detector="Generic_Test",
        signal_id="sig4",
        side="NO",
        entry_yes_price=0.40,
        price_path=[],
        resolved=True,
        outcome="EXPIRED",
        realized_roi_net=None,
    )
    assert res.exit_reason == "expired"
    assert res.sim_pnl_pct == 0.0


def test_unresolved_no_data_returns_no_data():
    """resolved=false + no trigger → exit=no_data, skip из aggregation."""
    res = simulate_signal(
        detector="Generic_Test",
        signal_id="sig5",
        side="NO",
        entry_yes_price=0.40,
        price_path=[{"yes_price": 0.38, "captured_at": "t1"}],
        resolved=False,
        outcome=None,
        realized_roi_net=None,
    )
    assert res.exit_reason == "no_data"


# ─── aggregate_sim_results ───────────────────────────────────────────────────


def test_aggregate_empty_returns_zeros():
    out = aggregate_sim_results([])
    assert out["n_total"] == 0
    assert out["mean_pnl_pct"] is None


def test_aggregate_mixed_results():
    """3 TP + 2 SL + 1 resolution + 1 no_data → aggregates корректно."""
    results = [
        _make_result(exit_reason="take_profit", sim_pnl_pct=26.0),
        _make_result(exit_reason="take_profit", sim_pnl_pct=26.0),
        _make_result(exit_reason="take_profit", sim_pnl_pct=26.0),
        _make_result(exit_reason="stop_loss", sim_pnl_pct=-24.0),
        _make_result(exit_reason="stop_loss", sim_pnl_pct=-24.0),
        _make_result(exit_reason="resolution", sim_pnl_pct=10.0),
        _make_result(exit_reason="no_data", sim_pnl_pct=0.0),
    ]
    out = aggregate_sim_results(results)
    assert out["n_total"] == 7
    assert out["n_tp"] == 3
    assert out["n_sl"] == 2
    assert out["n_resolution"] == 1
    assert out["n_no_data"] == 1
    # closed = 6 (без no_data), TP rate = 3/6 = 50%
    assert out["tp_rate_pct"] == 50.0
    assert out["sl_rate_pct"] == pytest.approx(33.3, abs=0.1)
    # sum closed = 3×26 + 2×(-24) + 10 = 78 - 48 + 10 = 40
    assert out["sum_pnl_pct"] == pytest.approx(40.0)
    # mean = 40 / 6 ≈ 6.67
    assert out["mean_pnl_pct"] == pytest.approx(6.67, abs=0.01)


def _make_result(*, exit_reason: str, sim_pnl_pct: float) -> ShadowSimResult:
    return ShadowSimResult(
        detector="X",
        signal_id="x",
        entry_yes_price=0.5,
        side="NO",
        tp_pct=30.0,
        sl_pct=-20.0,
        exit_reason=exit_reason,
        sim_pnl_pct=sim_pnl_pct,
        n_snapshots=0,
        exit_yes_price=None,
        is_hold_to_resolution=False,
    )


def test_fee_constant_is_round_trip():
    """ROUND_TRIP_FEE_PCT = 4.0 (2% per side × 2)."""
    assert ROUND_TRIP_FEE_PCT == 4.0
