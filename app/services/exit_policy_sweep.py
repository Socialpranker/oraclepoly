"""Exit-policy (TP/SL) sweep движок поверх price snapshots — гипотеза H1.

Контекст (research/virtual-feedback-2026-05/report.md):

TP/SL пороги детекторов захардкожены в `position_manager._DETECTOR_OVERRIDES`
и выставлены «на глаз» по small-N эмпирике. Вопрос H1: существуют ли пороги
(tp*, sl*), которые на ИСТОРИЧЕСКИХ траекториях цены (detector_signal_price_snapshots)
дали бы лучший net-PnL, чем текущий хардкод?

Этот модуль — переиспользуемый sweep-движок. Он НЕ трогает `shadow_simulation.
simulate_signal` (который реплеит ОДНУ фиксированную пару порогов), а переиспользует
его валидированную PnL-формулу `_signal_payout_pct_at` и гоняет по ней СЕТКУ (tp, sl).

Семантика отличается от simulate_signal намеренно:
  - simulate_signal имеет HOLD_TO_RESOLUTION special-case (D38 не трогаем порогами).
    Sweep же ИЩЕТ пороги — для него hold-to-resolution это просто «TP/SL не сработали».
  - Поэтому sweep всегда проверяет триггеры по сетке, а на отсутствие триггера
    использует hold-to-resolution PnL (realized_roi_net), если сигнал resolved.

ВАЖНО (анти-overfit, см. план H1):
  - Сетка ГРУБАЯ (шаг 5%), не мелкая.
  - Out-of-sample split по времени: train на ранних сигналах, validate на поздних.
  - Рекомендация (tp*, sl*) — это РЕКОМЕНДАЦИЯ для человека, НЕ авто-применение к проду.

Pure functions + опциональный DB-read. Тестируемо на синтетике без БД.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.shadow_simulation import ROUND_TRIP_FEE_PCT, _signal_payout_pct_at

# Грубая сетка (шаг 5%). Анти-overfit: НЕ мельчить.
DEFAULT_TP_GRID: tuple[float, ...] = (10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.0)
DEFAULT_SL_GRID: tuple[float, ...] = (-10.0, -15.0, -20.0, -25.0, -30.0, -35.0, -40.0)


@dataclass(frozen=True)
class SignalPath:
    """Один сигнал с его траекторией цены — вход для sweep."""

    signal_id: str
    side: str  # 'YES' | 'NO'
    entry_yes_price: float
    price_path: list[dict[str, Any]]  # [{captured_at, yes_price}], ordered by time
    resolved: bool
    outcome: str | None  # WIN/LOSS/EXPIRED/None
    realized_roi_net: float | None  # hold-to-resolution net ROI в ПРОЦЕНТАХ (5.0=+5%)


@dataclass(frozen=True)
class GridCell:
    """Результат одной пары (tp, sl) на наборе сигналов."""

    tp_pct: float
    sl_pct: float
    n_signals: int
    n_tp: int
    n_sl: int
    n_resolution: int
    n_expired: int
    sum_pnl_pct: float
    mean_pnl_pct: float | None
    # Sortino: mean / downside-deviation. None если нет downside или n<2.
    sortino: float | None


def _simulate_one(path: SignalPath, tp_pct: float, sl_pct: float) -> tuple[str, float]:
    """Симулирует ОДИН сигнал при заданных (tp, sl). Возвращает (exit_reason, net_pnl_pct).

    Никакого hold-to-resolution special-case по детектору — sweep ищет пороги,
    поэтому проверяет триггеры всегда. Если ни TP ни SL не сработали:
      - resolved + outcome != EXPIRED → 'resolution', pnl = realized_roi_net (в %)
      - outcome == EXPIRED            → 'expired', pnl = 0
      - не resolved                   → 'no_data', pnl = 0 (caller должен исключить)
    """
    for snap in path.price_path:
        pnl_pct = _signal_payout_pct_at(path.entry_yes_price, float(snap["yes_price"]), path.side)
        if pnl_pct >= tp_pct:
            return "take_profit", tp_pct - ROUND_TRIP_FEE_PCT
        if pnl_pct <= sl_pct:
            return "stop_loss", sl_pct - ROUND_TRIP_FEE_PCT

    if not path.resolved:
        return "no_data", 0.0
    if path.outcome == "EXPIRED":
        return "expired", 0.0
    # realized_roi_net уже в ПРОЦЕНТАХ и включает fees+slippage (hold-to-resolution
    # ground truth, см. _realized_roi_net_pct). НЕ домножать на 100 (fix 2026-05-29).
    return "resolution", float(path.realized_roi_net or 0.0)


def _sortino(pnls: list[float]) -> float | None:
    """Sortino-подобная метрика: mean / downside-deviation.

    Downside-deviation = sqrt(mean(min(0, pnl)^2)). None если <2 точек или
    нет downside (тогда коэффициент бесконечен — не информативен).
    """
    if len(pnls) < 2:
        return None
    mean = sum(pnls) / len(pnls)
    downside_sq = [min(0.0, p) ** 2 for p in pnls]
    dd = (sum(downside_sq) / len(pnls)) ** 0.5
    if dd <= 0:
        return None
    return mean / dd


def evaluate_grid_cell(paths: list[SignalPath], tp_pct: float, sl_pct: float) -> GridCell:
    """Прогнать набор сигналов через одну пару (tp, sl) → GridCell."""
    exits: list[tuple[str, float]] = [_simulate_one(p, tp_pct, sl_pct) for p in paths]
    # closed = всё кроме no_data (по нему нет валидного PnL).
    closed = [(reason, pnl) for reason, pnl in exits if reason != "no_data"]
    pnls = [pnl for _, pnl in closed]
    n_tp = sum(1 for reason, _ in closed if reason == "take_profit")
    n_sl = sum(1 for reason, _ in closed if reason == "stop_loss")
    n_res = sum(1 for reason, _ in closed if reason == "resolution")
    n_exp = sum(1 for reason, _ in closed if reason == "expired")
    sum_pnl = sum(pnls)
    mean_pnl = sum_pnl / len(pnls) if pnls else None
    return GridCell(
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        n_signals=len(closed),
        n_tp=n_tp,
        n_sl=n_sl,
        n_resolution=n_res,
        n_expired=n_exp,
        sum_pnl_pct=round(sum_pnl, 2),
        mean_pnl_pct=round(mean_pnl, 3) if mean_pnl is not None else None,
        sortino=round(s, 3) if (s := _sortino(pnls)) is not None else None,
    )


def sweep(
    paths: list[SignalPath],
    *,
    tp_grid: tuple[float, ...] = DEFAULT_TP_GRID,
    sl_grid: tuple[float, ...] = DEFAULT_SL_GRID,
) -> list[GridCell]:
    """Полный sweep: для каждой пары (tp, sl) из сетки — GridCell.

    Возвращает плоский список len(tp_grid) × len(sl_grid) ячеек.
    """
    return [evaluate_grid_cell(paths, tp, sl) for tp in tp_grid for sl in sl_grid]


def best_cell(
    cells: list[GridCell],
    *,
    objective: str = "mean_pnl",
    min_signals: int = 30,
) -> GridCell | None:
    """Выбрать лучшую ячейку по objective ('mean_pnl' | 'sum_pnl' | 'sortino').

    Sample-gate: ячейки с n_signals < min_signals игнорируются (шум).
    None если ни одна ячейка не проходит gate или метрика везде None.
    """
    eligible = [c for c in cells if c.n_signals >= min_signals]
    if not eligible:
        return None

    def key(c: GridCell) -> float:
        if objective == "sum_pnl":
            return c.sum_pnl_pct
        if objective == "sortino":
            return c.sortino if c.sortino is not None else float("-inf")
        return c.mean_pnl_pct if c.mean_pnl_pct is not None else float("-inf")

    ranked = sorted(eligible, key=key, reverse=True)
    top = ranked[0]
    if key(top) == float("-inf"):
        return None
    return top


def time_split(paths: list[SignalPath], *, train_frac: float = 0.6) -> tuple[list[SignalPath], list[SignalPath]]:
    """Out-of-sample split по времени.

    paths должны быть отсортированы по времени сигнала (ранние первыми).
    train_frac доля идёт в train, остаток — validate. Анти-look-ahead:
    рекомендацию выбираем на train, проверяем на validate.
    """
    if not paths:
        return [], []
    cut = max(1, int(len(paths) * train_frac))
    return paths[:cut], paths[cut:]


def recommend(
    paths: list[SignalPath],
    *,
    tp_grid: tuple[float, ...] = DEFAULT_TP_GRID,
    sl_grid: tuple[float, ...] = DEFAULT_SL_GRID,
    objective: str = "mean_pnl",
    min_signals: int = 30,
    train_frac: float = 0.6,
) -> dict[str, Any]:
    """Полный pipeline H1: train/validate split → best на train → метрика на validate.

    Возвращает dict с рекомендацией (tp*, sl*), train-метрикой и out-of-sample
    validate-метрикой той же пары. Если данных мало для split — honest-флаг.

    Это РЕКОМЕНДАЦИЯ для человека. НЕ применять к проду автоматически.
    """
    train, validate = time_split(paths, train_frac=train_frac)
    train_cells = sweep(train, tp_grid=tp_grid, sl_grid=sl_grid)
    best = best_cell(train_cells, objective=objective, min_signals=min_signals)

    if best is None:
        return {
            "status": "insufficient_data",
            "reason": f"no grid cell with n_signals>={min_signals} on train split",
            "n_train": len(train),
            "n_validate": len(validate),
            "recommended_tp": None,
            "recommended_sl": None,
        }

    # Та же пара (tp*, sl*) на out-of-sample validate.
    oos = evaluate_grid_cell(validate, best.tp_pct, best.sl_pct) if validate else None
    return {
        "status": "ok",
        "n_train": len(train),
        "n_validate": len(validate),
        "recommended_tp": best.tp_pct,
        "recommended_sl": best.sl_pct,
        "train_mean_pnl_pct": best.mean_pnl_pct,
        "train_sum_pnl_pct": best.sum_pnl_pct,
        "train_sortino": best.sortino,
        "validate_mean_pnl_pct": oos.mean_pnl_pct if oos else None,
        "validate_sum_pnl_pct": oos.sum_pnl_pct if oos else None,
        "validate_n_signals": oos.n_signals if oos else 0,
        "objective": objective,
    }
