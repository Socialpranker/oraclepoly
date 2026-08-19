"""Counterfactual Engine — policy diff -> P&L diff с confidence interval.

Фундамент Phase 1 Portfolio Analyst Agent (docs/specs/portfolio_analyst_agent.md
§4.2, §8): переиспользуемая библиотека вместо одноразовых audit-скриптов Phase 0.
Design: docs/superpowers/specs/2026-07-20-counterfactual-engine-design.md.

Чистая функция без side-effects (без DB-вызовов), как BayesianScoreCalculator
(app/core/scoring.py). CI считается всегда — не опциональный параметр, прямое
требование спеки (не default по выбору движка).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.calibration.validation import event_cluster_bootstrap_roi
from scripts.research.exit_strategy_backtest import PositionTrajectory


class Policy(Protocol):
    """Контрфактическая политика: воспроизводит net_pnl на одной trajectory.

    Variant A (протокол, не registry по имени) — меньше indirection, пока
    вызывающих скриптов один-два (см. design doc, "Решения по ходу брейнсторминга").
    """

    def simulate(self, trajectory: PositionTrajectory) -> float: ...


@dataclass(frozen=True)
class CounterfactualResult:
    """Результат policy diff. delta_pnl/ci_* — None при пустом входе (n=0),
    не exception — паттерн compute_bankroll_weighted (audit_case1).

    delta_pnl — среднее per-trajectory (v - b) по всем trajectories (та же
    величина, вокруг которой bootstrap строит CI — event_cluster_bootstrap_roi
    кластеризует и усредняет, а не суммирует). baseline_total_pnl/variant_total_pnl —
    суммы (портфельный P&L каждой policy), для контекста рядом со средней дельтой.
    """

    delta_pnl: float | None
    ci_low: float | None
    ci_high: float | None
    n: int
    baseline_total_pnl: float
    variant_total_pnl: float


def run_counterfactual(
    trajectories: list[PositionTrajectory],
    baseline: Policy,
    variant: Policy,
    *,
    event_ids: list[str | None] | None = None,
    bootstrap_resamples: int = 1500,
    bootstrap_seed: int = 42,
) -> CounterfactualResult:
    """Прогнать baseline и variant policy на одном наборе trajectories, вернуть
    delta P&L с per-event bootstrap CI (event_cluster_bootstrap_roi как есть).

    event_ids — параллельный список (тот же порядок/длина, что trajectories).
    None-элемент или отсутствие параметра целиком -> соответствующая trajectory
    кластеризуется как собственное singleton-событие по order_id (консервативный
    fallback, тот же паттерн, что audit_case4::bootstrap_delta_ci для сигналов
    без event_id — не даёт им искусственно скоррелироваться под одним ключом).
    """
    if not trajectories:
        return CounterfactualResult(
            delta_pnl=None, ci_low=None, ci_high=None, n=0, baseline_total_pnl=0.0, variant_total_pnl=0.0
        )

    baseline_pnls = [baseline.simulate(traj) for traj in trajectories]
    variant_pnls = [variant.simulate(traj) for traj in trajectories]
    deltas = [v - b for b, v in zip(baseline_pnls, variant_pnls, strict=True)]

    if event_ids is None:
        event_ids = [None] * len(trajectories)
    event_keys = [
        event_id if event_id else traj.order_id for event_id, traj in zip(event_ids, trajectories, strict=True)
    ]

    ci_low, ci_high = event_cluster_bootstrap_roi(
        event_keys, deltas, n_resamples=bootstrap_resamples, confidence=0.95, seed=bootstrap_seed
    )

    return CounterfactualResult(
        delta_pnl=round(sum(deltas) / len(deltas), 4),
        ci_low=ci_low,
        ci_high=ci_high,
        n=len(trajectories),
        baseline_total_pnl=round(sum(baseline_pnls), 4),
        variant_total_pnl=round(sum(variant_pnls), 4),
    )
