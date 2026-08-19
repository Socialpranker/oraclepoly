"""Тесты AsIsPolicy — baseline, возвращает фактический net_pnl без пересимуляции."""

from __future__ import annotations

from datetime import UTC, datetime

from app.analytics.policies.as_is import AsIsPolicy
from scripts.research.exit_strategy_backtest import PositionTrajectory


def _traj(order_id: str) -> PositionTrajectory:
    return PositionTrajectory(
        order_id=order_id,
        detector="D_TEST",
        side="YES",
        stake=100.0,
        fill_price=0.40,
        category="crypto",
        fee_schedule=None,
        filled_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        snapshots=[],
        expected_resolution_at=None,
    )


def test_as_is_policy_returns_looked_up_net_pnl():
    lookup = {"order-1": 42.5, "order-2": -13.0}
    policy = AsIsPolicy(actual_net_pnl_fn=lambda traj: lookup[traj.order_id])

    assert policy.simulate(_traj("order-1")) == 42.5
    assert policy.simulate(_traj("order-2")) == -13.0


def test_as_is_policy_does_not_resimulate_from_snapshots():
    """AsIsPolicy игнорирует ценовую траекторию целиком — использует только lookup,
    даже если snapshots присутствуют (в отличие от trailing/decay/partial)."""
    from scripts.research.exit_strategy_backtest import PricePoint

    traj = PositionTrajectory(
        order_id="order-3",
        detector="D_TEST",
        side="YES",
        stake=100.0,
        fill_price=0.40,
        category="crypto",
        fee_schedule=None,
        filled_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        snapshots=[
            PricePoint(captured_at=datetime(2026, 6, 1, 12, 10, tzinfo=UTC), yes_price=0.90),
        ],
        expected_resolution_at=None,
    )
    policy = AsIsPolicy(actual_net_pnl_fn=lambda _traj: 7.0)

    assert policy.simulate(traj) == 7.0
