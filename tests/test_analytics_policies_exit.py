"""Parity-тесты: перенесённые Policy-классы в app/analytics/policies/exit.py
должны давать тот же net_pnl, что старые свободные функции в
scripts/research/exit_strategy_backtest.py и audit_case3_hold_vs_tpsl_replay.py
на одной и той же PositionTrajectory. Регрессия от переноса не допускается
(design doc, Task breakdown п.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analytics.policies.exit import (
    HoldToResolutionPolicy,
    PartialExitPolicy,
    TimeDecayPolicy,
    TrailingStopPolicy,
)
from scripts.research.audit_case3_hold_vs_tpsl_replay import simulate_hold_to_resolution
from scripts.research.exit_strategy_backtest import (
    PositionTrajectory,
    simulate_partial_exit,
    simulate_time_decay,
    simulate_trailing_stop,
)


def _traj(*, prices: list[tuple[int, float]], horizon_minutes: float = 120.0) -> PositionTrajectory:
    filled_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    from scripts.research.exit_strategy_backtest import PricePoint

    snapshots = [PricePoint(captured_at=filled_at + timedelta(minutes=m), yes_price=p) for m, p in prices]
    return PositionTrajectory(
        order_id="parity-order",
        detector="D_TEST",
        side="YES",
        stake=100.0,
        fill_price=0.40,
        category="crypto",
        fee_schedule=None,
        filled_at=filled_at,
        snapshots=snapshots,
        expected_resolution_at=filled_at + timedelta(minutes=horizon_minutes),
    )


def test_trailing_stop_policy_matches_free_function():
    traj = _traj(prices=[(0, 0.40), (10, 0.50), (20, 0.60), (30, 0.55), (40, 0.50)])

    old_result = simulate_trailing_stop(traj, trail_pct=10.0)
    new_result = TrailingStopPolicy(trail_pct=10.0).simulate(traj)

    assert new_result == old_result.net_pnl


def test_time_decay_policy_matches_free_function():
    traj = _traj(horizon_minutes=100.0, prices=[(0, 0.40), (20, 0.38), (85, 0.34), (95, 0.30)])

    old_result = simulate_time_decay(traj, wide_sl_pct=-30.0, narrow_sl_pct=-10.0, wide_tp_pct=40.0, narrow_tp_pct=15.0)
    new_result = TimeDecayPolicy(
        wide_sl_pct=-30.0, narrow_sl_pct=-10.0, wide_tp_pct=40.0, narrow_tp_pct=15.0
    ).simulate(traj)

    assert new_result == old_result.net_pnl


def test_partial_exit_policy_matches_free_function():
    traj = _traj(prices=[(0, 0.40), (10, 0.46), (20, 0.52)])

    old_result = simulate_partial_exit(traj, partial_trigger_pct=15.0, partial_fraction=0.5, actual_outcome="WIN")
    new_result = PartialExitPolicy(
        partial_trigger_pct=15.0, partial_fraction=0.5, actual_outcome_fn=lambda _traj: "WIN"
    ).simulate(traj)

    assert new_result == old_result.net_pnl


def test_hold_to_resolution_policy_matches_free_function():
    traj = _traj(prices=[])

    old_result = simulate_hold_to_resolution(traj, "WIN")
    new_result = HoldToResolutionPolicy(actual_outcome_fn=lambda _traj: "WIN").simulate(traj)

    assert new_result == old_result.net_pnl


def test_trailing_stop_policy_returns_zero_when_no_snapshots():
    """Явное решение (не silent skip): 0.0 при отсутствии снапшотов, не exception."""
    traj = _traj(prices=[])

    assert TrailingStopPolicy(trail_pct=10.0).simulate(traj) == 0.0
