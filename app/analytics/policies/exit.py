"""Exit-policy обёртки над Policy-протоколом (app.analytics.counterfactual).

Переносим 4 существующие функции-симулятора из
scripts/research/exit_strategy_backtest.py (эталон, "не менять" — Phase 0
докстринг) и scripts/research/audit_case3_hold_vs_tpsl_replay.py в классы
с .simulate(). Не дублируем логику — импортируем и оборачиваем.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from scripts.research.audit_case3_hold_vs_tpsl_replay import simulate_hold_to_resolution
from scripts.research.exit_strategy_backtest import (
    PositionTrajectory,
    simulate_partial_exit,
    simulate_time_decay,
    simulate_trailing_stop,
)

ActualOutcomeFn = Callable[[PositionTrajectory], str]


@dataclass(frozen=True)
class TrailingStopPolicy:
    trail_pct: float

    def simulate(self, trajectory: PositionTrajectory) -> float:
        result = simulate_trailing_stop(trajectory, trail_pct=self.trail_pct)
        return result.net_pnl if result is not None else 0.0


@dataclass(frozen=True)
class TimeDecayPolicy:
    wide_sl_pct: float
    narrow_sl_pct: float
    wide_tp_pct: float
    narrow_tp_pct: float

    def simulate(self, trajectory: PositionTrajectory) -> float:
        result = simulate_time_decay(
            trajectory,
            wide_sl_pct=self.wide_sl_pct,
            narrow_sl_pct=self.narrow_sl_pct,
            wide_tp_pct=self.wide_tp_pct,
            narrow_tp_pct=self.narrow_tp_pct,
        )
        return result.net_pnl if result is not None else 0.0


@dataclass(frozen=True)
class PartialExitPolicy:
    partial_trigger_pct: float
    partial_fraction: float
    actual_outcome_fn: ActualOutcomeFn

    def simulate(self, trajectory: PositionTrajectory) -> float:
        result = simulate_partial_exit(
            trajectory,
            partial_trigger_pct=self.partial_trigger_pct,
            partial_fraction=self.partial_fraction,
            actual_outcome=self.actual_outcome_fn(trajectory),
        )
        return result.net_pnl if result is not None else 0.0


@dataclass(frozen=True)
class HoldToResolutionPolicy:
    actual_outcome_fn: ActualOutcomeFn

    def simulate(self, trajectory: PositionTrajectory) -> float:
        result = simulate_hold_to_resolution(trajectory, self.actual_outcome_fn(trajectory))
        return result.net_pnl
