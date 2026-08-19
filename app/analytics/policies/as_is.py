"""AsIsPolicy — baseline для большинства сравнений: фактический net_pnl из
execution_orders без пересимуляции.

PositionTrajectory не несёт net_pnl/outcome как поля (см. design doc,
"Существующий фундамент" — SQL-запрос в fetch_resolved_with_snapshots их
выбирает, но row-to-dataclass маппинг их отбрасывает). Движок остаётся чистым
(без DB-вызовов внутри Policy) — вызывающий код (CLI-скрипт) один раз
загружает net_pnl по всем order_id и передаёт lookup-функцию сюда. Тот же
паттерн, что actual_outcome_fn в app/analytics/policies/exit.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from scripts.research.exit_strategy_backtest import PositionTrajectory

ActualNetPnlFn = Callable[[PositionTrajectory], float]


@dataclass(frozen=True)
class AsIsPolicy:
    actual_net_pnl_fn: ActualNetPnlFn

    def simulate(self, trajectory: PositionTrajectory) -> float:
        return self.actual_net_pnl_fn(trajectory)
