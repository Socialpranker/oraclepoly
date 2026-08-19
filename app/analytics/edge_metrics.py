"""Wilson/Sortino rolling-метрики для edge decay/regime monitor.

Новый канон для Wilson/Sortino в этом проекте (app/jobs/edge_decay_monitor.py).
Существующие копии (app/services/detector_metrics.py::compute_sortino,
app/backtest/metrics.py, app/jobs/calibration_bins_refit.py::wilson_ci,
app/services/exit_policy_sweep.py::_sortino) НЕ мигрируют на эту реализацию
в этой волне — отдельная будущая задача.

Wilson переиспользует app.services.wilson_ci.wilson_score_interval напрямую
(готовая чистая формула, специфики промоут-гейтов нет). Sortino — реализация
здесь, идентичная формула (MAR=0), но принимает список net_pnl вместо
предагрегированных скаляров, как detector_metrics.py::compute_sortino.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.services.wilson_ci import wilson_score_interval


def synthetic_pnl(entry_price: float, outcome: str) -> float:
    """Ставка $1 условно, без fee/slippage — оптимистичная верхняя граница.

    Используется когда execution_orders.net_pnl недоступен (shadow-стадия,
    детектор ещё не исполняет реальные ордера).

    Raises:
        ValueError если outcome не в {"WIN", "LOSS"}.
    """
    if outcome == "WIN":
        return 1.0 - entry_price
    if outcome == "LOSS":
        return -entry_price
    raise ValueError(f"unresolved outcome: {outcome}")


@dataclass(frozen=True)
class WindowMetrics:
    n: int
    wins: int
    wilson_lower: float
    wilson_upper: float
    sortino: float | None  # None если n=0 или дегенерат без downside-смысла


def compute_window_metrics(outcomes: list[str], pnls: list[float]) -> WindowMetrics:
    """Wilson CI + Sortino на одном окне (baseline или recent).

    Args:
        outcomes: список "WIN"/"LOSS" (EXPIRED уже отфильтрован вызывающим SQL).
        pnls: parallel-список net_pnl (реальный execution_orders.net_pnl если
            есть, иначе synthetic_pnl()) — той же длины что outcomes.

    Raises:
        ValueError если len(outcomes) != len(pnls).
    """
    if len(outcomes) != len(pnls):
        raise ValueError(f"outcomes and pnls must have same length, got {len(outcomes)} vs {len(pnls)}")

    n = len(outcomes)
    wins = sum(1 for o in outcomes if o == "WIN")
    wilson_lower, wilson_upper = wilson_score_interval(wins, n)

    sortino: float | None
    if n == 0:
        sortino = None
    else:
        mean_r = sum(pnls) / n
        sum_neg_sq = sum(min(p, 0.0) ** 2 for p in pnls)
        if sum_neg_sq > 0:
            downside_vol = math.sqrt(sum_neg_sq / n)
            sortino = mean_r / downside_vol if downside_vol > 0 else None
        else:
            sortino = float("inf") if mean_r > 0 else None

    return WindowMetrics(n=n, wins=wins, wilson_lower=wilson_lower, wilson_upper=wilson_upper, sortino=sortino)
