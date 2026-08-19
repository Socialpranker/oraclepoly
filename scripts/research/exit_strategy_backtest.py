"""Бэктест альтернативных exit-стратегий поверх реальных исторических сигналов.

Контекст: research/position-exit-strategies-2026-07/ — внешний ресёрч не дал
прямого ответа на три гипотезы (trailing stop / time-decay exit / partial exit),
потому что вся количественная evidence взята с continuous equity/options рынков,
а наш рынок — bounded [0,1] с binary resolution. Единственный способ закрыть
open questions OQ1/OQ2 из отчёта — прогнать альтернативные exit-правила через
собственные исторические сигналы.

Не пересчитывает сигналы (это делает app/backtest/replay_engine.py для canonical
parquet-данных) — берёт уже сгенерированные реальные virtual-сигналы из
execution_orders + detector_signal_price_snapshots и симулирует поверх них
альтернативные моменты выхода.

Разведка на проде (2026-07-03) показала: снапшоты покрывают только период с
28 мая 2026, и статистически осмысленный объём дают только два детектора —
D38_resolution_lag (165 resolved-ордеров) и D42_event_driven_ml (138). Остальные
детекторы физически не имеют траектории цены между входом и выходом — их сюда
не включать, не обобщать выводы на весь портфель.

Запускать ТОЛЬКО на проде (см. CLAUDE.md: scripts/research/*.py — БД только через
SSH, локальный PostgreSQL пуст). Через -m (не прямой путь к файлу — иначе Python
кладёт директорию скрипта, а не /app, первой в sys.path и `from app...` падает):
    docker exec oraclepoly-backend python -m scripts.research.exit_strategy_backtest \
        --detector D38_resolution_lag --start 2026-05-28

Superseded (2026-07-20): симуляторы обёрнуты как Policy-классы в
app/analytics/policies/exit.py для переиспользования в Counterfactual Engine
(app/analytics/counterfactual.py). Этот файл НЕ удаляется — функции
по-прежнему импортируются оттуда напрямую (не дублируются), история/
трассировка Phase 0 сохраняется.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime

from app.jobs.position_manager import _compute_early_exit_pnl
from app.jobs.v2_execution_resolve import _compute_settlement_pnl
from app.services.database import close_db, fetch, init_db
from app.services.jsonb_utils import parse_jsonb


@dataclass
class PricePoint:
    captured_at: datetime
    yes_price: float


@dataclass
class PositionTrajectory:
    order_id: str
    detector: str
    side: str
    stake: float
    fill_price: float
    category: str
    fee_schedule: dict | None
    filled_at: datetime
    snapshots: list[PricePoint]  # отсортированы по captured_at ASC
    expected_resolution_at: datetime | None


@dataclass
class SimResult:
    order_id: str
    exit_reason: str
    exit_price: float
    net_pnl: float


@dataclass
class PartialExitResult:
    order_id: str
    partial_exit_price: float | None
    partial_stake: float
    remainder_stake: float
    net_pnl: float


_FLOAT_EPS = 1e-9  # толерантность для >=/<= сравнений с порогами (float division precision)


def _unrealized_pnl_pct(side: str, fill_price: float, current_price: float) -> float | None:
    """Дублирует формулу _unrealized_pnl_pct из position_manager.py (та не экспортируется
    как public API для чужих модулей — здесь чистая копия той же математики, не импорт
    приватной функции через подчёркивание из другого модуля)."""
    if fill_price <= 0.0:
        return None
    if side == "YES":
        return (current_price / fill_price - 1.0) * 100.0
    if side == "NO":
        no_current = 1.0 - current_price
        return (no_current / fill_price - 1.0) * 100.0
    return None


def simulate_trailing_stop(traj: PositionTrajectory, trail_pct: float) -> SimResult | None:
    """Trailing stop: отслеживаем пик unrealized PnL%, выходим когда откат от
    пика превышает trail_pct (в процентных пунктах PnL%, не в цене).

    Возвращает None если нет снапшотов (нечего симулировать).
    """
    if not traj.snapshots:
        return None

    peak_pnl_pct = float("-inf")
    for point in traj.snapshots:
        pnl_pct = _unrealized_pnl_pct(traj.side, traj.fill_price, point.yes_price)
        if pnl_pct is None:
            continue
        if pnl_pct > peak_pnl_pct:
            peak_pnl_pct = pnl_pct
            continue
        drawdown_from_peak = peak_pnl_pct - pnl_pct
        if drawdown_from_peak >= trail_pct - _FLOAT_EPS:
            pnl = _compute_early_exit_pnl(
                side=traj.side,
                stake=traj.stake,
                fill_price=traj.fill_price,
                exit_price=point.yes_price,
                category=traj.category,
                fee_schedule=traj.fee_schedule,
            )
            return SimResult(
                order_id=traj.order_id,
                exit_reason="trailing_stop",
                exit_price=point.yes_price,
                net_pnl=pnl["net_pnl"],
            )

    last = traj.snapshots[-1]
    pnl = _compute_early_exit_pnl(
        side=traj.side,
        stake=traj.stake,
        fill_price=traj.fill_price,
        exit_price=last.yes_price,
        category=traj.category,
        fee_schedule=traj.fee_schedule,
    )
    return SimResult(
        order_id=traj.order_id,
        exit_reason="held_to_end_of_data",
        exit_price=last.yes_price,
        net_pnl=pnl["net_pnl"],
    )


def simulate_time_decay(
    traj: PositionTrajectory,
    *,
    wide_sl_pct: float,
    narrow_sl_pct: float,
    wide_tp_pct: float,
    narrow_tp_pct: float,
) -> SimResult | None:
    """Мягкая версия H2 (open question OQ2 из ресёрча): TP/SL диапазон линейно
    сужается от wide_* (в начале хода позиции) до narrow_* (у ожидаемой резолюции),
    вместо жёсткого принудительного выхода. sl_pct отрицательный, tp_pct положительный.

    Требует expected_resolution_at — если не задан, используем последний снапшот
    как прокси горизонта (progress всегда < 1.0, эффективно = wide-порог).
    """
    if not traj.snapshots:
        return None

    horizon_start = traj.filled_at
    horizon_end = traj.expected_resolution_at or traj.snapshots[-1].captured_at
    horizon_span = (horizon_end - horizon_start).total_seconds()

    for point in traj.snapshots:
        elapsed = (point.captured_at - horizon_start).total_seconds()
        progress = min(max(elapsed / horizon_span, 0.0), 1.0) if horizon_span > 0 else 0.0

        sl_threshold = wide_sl_pct + (narrow_sl_pct - wide_sl_pct) * progress
        tp_threshold = wide_tp_pct + (narrow_tp_pct - wide_tp_pct) * progress

        pnl_pct = _unrealized_pnl_pct(traj.side, traj.fill_price, point.yes_price)
        if pnl_pct is None:
            continue

        if pnl_pct <= sl_threshold + _FLOAT_EPS:
            pnl = _compute_early_exit_pnl(
                side=traj.side,
                stake=traj.stake,
                fill_price=traj.fill_price,
                exit_price=point.yes_price,
                category=traj.category,
                fee_schedule=traj.fee_schedule,
            )
            return SimResult(
                order_id=traj.order_id,
                exit_reason="time_decay_sl",
                exit_price=point.yes_price,
                net_pnl=pnl["net_pnl"],
            )
        if pnl_pct >= tp_threshold - _FLOAT_EPS:
            pnl = _compute_early_exit_pnl(
                side=traj.side,
                stake=traj.stake,
                fill_price=traj.fill_price,
                exit_price=point.yes_price,
                category=traj.category,
                fee_schedule=traj.fee_schedule,
            )
            return SimResult(
                order_id=traj.order_id,
                exit_reason="time_decay_tp",
                exit_price=point.yes_price,
                net_pnl=pnl["net_pnl"],
            )

    last = traj.snapshots[-1]
    pnl = _compute_early_exit_pnl(
        side=traj.side,
        stake=traj.stake,
        fill_price=traj.fill_price,
        exit_price=last.yes_price,
        category=traj.category,
        fee_schedule=traj.fee_schedule,
    )
    return SimResult(
        order_id=traj.order_id,
        exit_reason="held_to_end_of_data",
        exit_price=last.yes_price,
        net_pnl=pnl["net_pnl"],
    )


def simulate_partial_exit(
    traj: PositionTrajectory,
    *,
    partial_trigger_pct: float,
    partial_fraction: float,
    actual_outcome: str,
) -> PartialExitResult | None:
    """Продать partial_fraction позиции при первом достижении partial_trigger_pct
    unrealized PnL%, остаток держать до фактического исхода (actual_outcome —
    WIN/LOSS, приходит из фактических данных, т.к. симулировать саму резолюцию
    не наша задача — она уже случилась).

    Обе ноги считаются раздельно: partial-нога = early exit (двойная комиссия,
    _compute_early_exit_pnl), remainder-нога = settlement (одна нога, fee=0 при
    выигрыше — _compute_settlement_pnl). Итоговый net_pnl = сумма обеих.
    """
    if not traj.snapshots:
        return None

    partial_stake = traj.stake * partial_fraction
    remainder_stake = traj.stake - partial_stake

    for point in traj.snapshots:
        pnl_pct = _unrealized_pnl_pct(traj.side, traj.fill_price, point.yes_price)
        if pnl_pct is None:
            continue
        if pnl_pct >= partial_trigger_pct - _FLOAT_EPS:
            partial_pnl = _compute_early_exit_pnl(
                side=traj.side,
                stake=partial_stake,
                fill_price=traj.fill_price,
                exit_price=point.yes_price,
                category=traj.category,
                fee_schedule=traj.fee_schedule,
            )
            remainder_pnl = _compute_settlement_pnl(
                stake=remainder_stake,
                fill_price=traj.fill_price,
                outcome=actual_outcome,
                category=traj.category,
                fee_schedule=traj.fee_schedule,
            )
            return PartialExitResult(
                order_id=traj.order_id,
                partial_exit_price=point.yes_price,
                partial_stake=partial_stake,
                remainder_stake=remainder_stake,
                net_pnl=round(partial_pnl["net_pnl"] + remainder_pnl["net_pnl"], 4),
            )

    # Триггер не достигнут — вся позиция идёт как обычный settlement (baseline).
    full_pnl = _compute_settlement_pnl(
        stake=traj.stake,
        fill_price=traj.fill_price,
        outcome=actual_outcome,
        category=traj.category,
        fee_schedule=traj.fee_schedule,
    )
    return PartialExitResult(
        order_id=traj.order_id,
        partial_exit_price=None,
        partial_stake=0.0,
        remainder_stake=traj.stake,
        net_pnl=full_pnl["net_pnl"],
    )


_FETCH_TRAJECTORIES_SQL = """
SELECT eo.id, eo.detector, eo.side, eo.stake::float AS stake, eo.fill_price::float AS fill_price,
       COALESCE(m.category, 'other') AS category, m.fee_schedule,
       eo.filled_at, eo.outcome, eo.net_pnl::float AS actual_net_pnl,
       eo.exit_reason AS actual_exit_reason,
       COALESCE(ds.resolved_at, eo.settled_at) AS expected_resolution_at,
       eo.detector_signal_id
  FROM execution_orders eo
  JOIN detector_signals ds ON ds.id = eo.detector_signal_id
  LEFT JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.status = 'virtual_settled' AND eo.execution_mode = 'virtual'
   AND eo.detector = $1 AND eo.filled_at >= $2
   AND eo.outcome IN ('WIN', 'LOSS', 'EARLY_EXIT', 'EXPIRED')
"""

_FETCH_SNAPSHOTS_SQL = """
SELECT captured_at, yes_price::float AS yes_price
  FROM detector_signal_price_snapshots
 WHERE detector_signal_id = $1
 ORDER BY captured_at ASC
"""


async def fetch_resolved_with_snapshots(detector: str, start_date: datetime) -> list[PositionTrajectory]:
    """Загрузить resolved-позиции детектора вместе с ценовой траекторией.
    Только те, у кого есть хотя бы 1 снапшот (иначе симуляция невозможна)."""
    rows = await fetch(_FETCH_TRAJECTORIES_SQL, detector, start_date)
    trajectories = []
    for row in rows:
        snapshot_rows = await fetch(_FETCH_SNAPSHOTS_SQL, row["detector_signal_id"])
        if not snapshot_rows:
            continue
        snapshots = [PricePoint(captured_at=s["captured_at"], yes_price=s["yes_price"]) for s in snapshot_rows]
        trajectories.append(
            PositionTrajectory(
                order_id=str(row["id"]),
                detector=row["detector"],
                side=row["side"],
                stake=row["stake"],
                fill_price=row["fill_price"],
                category=row["category"],
                fee_schedule=parse_jsonb(row["fee_schedule"]),
                filled_at=row["filled_at"],
                snapshots=snapshots,
                expected_resolution_at=row["expected_resolution_at"],
            )
        )
    return trajectories


def _sortino_ratio(net_pnls: list[float]) -> float | None:
    """Sortino ratio на списке net_pnl (per-trade return, не % — но достаточно
    для relative-сравнения baseline vs альтернатива на одном и том же наборе позиций)."""
    if not net_pnls:
        return None
    mean = sum(net_pnls) / len(net_pnls)
    downside = [p for p in net_pnls if p < 0]
    if not downside:
        return None
    downside_std = (sum(p**2 for p in downside) / len(downside)) ** 0.5
    if downside_std == 0:
        return None
    return mean / downside_std


async def run_backtest(detector: str, start_date: datetime) -> dict:
    trajectories = await fetch_resolved_with_snapshots(detector, start_date)

    baseline_pnls = []
    trailing_pnls = []
    decay_pnls = []
    partial_pnls = []

    for traj in trajectories:
        row = await fetch(
            "SELECT net_pnl::float AS net_pnl FROM execution_orders WHERE id = $1", traj.order_id
        )
        if row:
            baseline_pnls.append(row[0]["net_pnl"] or 0.0)

        trail_result = simulate_trailing_stop(traj, trail_pct=15.0)
        if trail_result:
            trailing_pnls.append(trail_result.net_pnl)

        decay_result = simulate_time_decay(
            traj, wide_sl_pct=-30.0, narrow_sl_pct=-10.0, wide_tp_pct=40.0, narrow_tp_pct=15.0
        )
        if decay_result:
            decay_pnls.append(decay_result.net_pnl)

        outcome_row = await fetch(
            "SELECT outcome FROM execution_orders WHERE id = $1", traj.order_id
        )
        actual_outcome = outcome_row[0]["outcome"] if outcome_row else "LOSS"
        # Partial exit требует финального исхода WIN/LOSS для remainder-ноги —
        # EARLY_EXIT/EXPIRED позиции в реальности не резолвились обычным путём,
        # используем LOSS как консервативный proxy (остаток истекает без выигрыша).
        proxy_outcome = actual_outcome if actual_outcome in ("WIN", "LOSS") else "LOSS"
        partial_result = simulate_partial_exit(
            traj, partial_trigger_pct=15.0, partial_fraction=0.5, actual_outcome=proxy_outcome
        )
        if partial_result:
            partial_pnls.append(partial_result.net_pnl)

    return {
        "detector": detector,
        "n_trajectories": len(trajectories),
        "baseline": {
            "n": len(baseline_pnls),
            "total_net_pnl": round(sum(baseline_pnls), 2),
            "sortino": _sortino_ratio(baseline_pnls),
        },
        "trailing_stop_15pct": {
            "n": len(trailing_pnls),
            "total_net_pnl": round(sum(trailing_pnls), 2),
            "sortino": _sortino_ratio(trailing_pnls),
        },
        "time_decay": {
            "n": len(decay_pnls),
            "total_net_pnl": round(sum(decay_pnls), 2),
            "sortino": _sortino_ratio(decay_pnls),
        },
        "partial_exit_50pct": {
            "n": len(partial_pnls),
            "total_net_pnl": round(sum(partial_pnls), 2),
            "sortino": _sortino_ratio(partial_pnls),
        },
    }


async def _run_with_db(detector: str, start_date: datetime) -> dict:
    await init_db()
    try:
        return await run_backtest(detector, start_date)
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detector", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    start_date = datetime.strptime(args.start, "%Y-%m-%d")
    result = asyncio.run(_run_with_db(args.detector, start_date))

    import json

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
