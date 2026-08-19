"""Тесты движка `app/analytics/counterfactual.py` — policy diff → P&L diff с CI.

Все тесты на синтетических PositionTrajectory (без БД), паттерн как
tests/test_exit_strategy_backtest.py и tests/test_audit_case4_g18_money_contribution.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.analytics.counterfactual import CounterfactualResult, run_counterfactual
from scripts.research.exit_strategy_backtest import PositionTrajectory


def _traj(order_id: str, *, stake: float = 100.0, fill_price: float = 0.40) -> PositionTrajectory:
    filled_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    return PositionTrajectory(
        order_id=order_id,
        detector="D_TEST",
        side="YES",
        stake=stake,
        fill_price=fill_price,
        category="crypto",
        fee_schedule=None,
        filled_at=filled_at,
        snapshots=[],
        expected_resolution_at=None,
    )


class _ConstPolicy:
    """Policy-протокол: возвращает фиксированный net_pnl независимо от trajectory."""

    def __init__(self, value: float) -> None:
        self._value = value

    def simulate(self, trajectory: PositionTrajectory) -> float:
        return self._value


class _PerTrajPolicy:
    """Policy-протокол: net_pnl по словарю order_id -> value."""

    def __init__(self, values: dict[str, float]) -> None:
        self._values = values

    def simulate(self, trajectory: PositionTrajectory) -> float:
        return self._values[trajectory.order_id]


def test_run_counterfactual_empty_trajectories_returns_none_fields():
    result = run_counterfactual([], baseline=_ConstPolicy(0.0), variant=_ConstPolicy(0.0))

    assert isinstance(result, CounterfactualResult)
    assert result.n == 0
    assert result.delta_pnl is None
    assert result.ci_low is None
    assert result.ci_high is None
    assert result.baseline_total_pnl == 0.0
    assert result.variant_total_pnl == 0.0


def test_run_counterfactual_no_difference_gives_zero_delta_and_ci_includes_zero():
    trajectories = [_traj(f"t{i}") for i in range(10)]
    baseline = _ConstPolicy(50.0)
    variant = _ConstPolicy(50.0)

    result = run_counterfactual(trajectories, baseline=baseline, variant=variant)

    assert result.n == 10
    assert result.delta_pnl == pytest.approx(0.0)
    assert result.ci_low <= 0.0 <= result.ci_high


def test_run_counterfactual_clear_difference_gives_nonzero_delta_ci_excludes_zero():
    trajectories = [_traj(f"t{i}") for i in range(30)]
    baseline = _ConstPolicy(10.0)
    variant = _ConstPolicy(50.0)

    result = run_counterfactual(trajectories, baseline=baseline, variant=variant)

    assert result.delta_pnl == pytest.approx(40.0)
    assert result.baseline_total_pnl == pytest.approx(300.0)
    assert result.variant_total_pnl == pytest.approx(1500.0)
    assert result.ci_low > 0.0
    assert result.ci_high > 0.0


def test_run_counterfactual_event_clustering_does_not_overstate_significance():
    """3 события по 10 коррелированных сигналов (все сигналы внутри события
    двигаются вместе — общий event-level шок) должны давать заметно более широкий
    (менее уверенный) CI, чем те же 3 значения эффекта, размеченные как 30
    полностью независимых событий — кластеризация корректно снижает effective
    sample size с 30 строк до 3 реальных событий."""
    event_shock = {"event_0": 40.0, "event_1": -20.0, "event_2": 60.0}
    clustered_trajectories = [_traj(f"t{e}_{i}") for e in range(3) for i in range(10)]
    clustered_values = {f"t{e}_{i}": event_shock[f"event_{e}"] for e in range(3) for i in range(10)}
    clustered_event_ids = [f"event_{e}" for e in range(3) for _ in range(10)]
    clustered_result = run_counterfactual(
        clustered_trajectories,
        baseline=_ConstPolicy(0.0),
        variant=_PerTrajPolicy(clustered_values),
        event_ids=clustered_event_ids,
    )

    independent_trajectories = [_traj(f"u{i}") for i in range(30)]
    independent_values = {f"u{i}": [40.0, -20.0, 60.0][i % 3] for i in range(30)}
    independent_result = run_counterfactual(
        independent_trajectories,
        baseline=_ConstPolicy(0.0),
        variant=_PerTrajPolicy(independent_values),
        event_ids=[f"event_{i}" for i in range(30)],
    )

    # 3 реальных события (коррелированные сигналы внутри каждого) должны давать
    # заметно более широкий CI, чем те же значения эффекта размеченные как
    # 30 независимых событий — меньше effective sample size, шире неопределённость.
    clustered_width = clustered_result.ci_high - clustered_result.ci_low
    independent_width = independent_result.ci_high - independent_result.ci_low
    assert clustered_width > independent_width


def test_run_counterfactual_deterministic_with_default_seed():
    trajectories = [_traj(f"t{i}") for i in range(15)]

    result_a = run_counterfactual(trajectories, baseline=_ConstPolicy(0.0), variant=_ConstPolicy(25.0))
    result_b = run_counterfactual(trajectories, baseline=_ConstPolicy(0.0), variant=_ConstPolicy(25.0))

    assert result_a.ci_low == result_b.ci_low
    assert result_a.ci_high == result_b.ci_high
