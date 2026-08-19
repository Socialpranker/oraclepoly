"""Backtest framework — paired tick replay поверх canonical layer.

См. docs/superpowers/plans/2026-05-backtest-framework/SPEC.md
"""

from app.backtest.metrics import wilson_score_interval
from app.backtest.schema import BacktestConfig, BacktestResult, SignalOutcome

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "SignalOutcome",
    "wilson_score_interval",
]
