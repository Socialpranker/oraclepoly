"""Adverse selection intel — чистый аналитический слой (без DB).

Метрика направленческого adverse selection на уровне detector×category:
где нас систематически переигрывают информированные контрагенты (проигрываем
направление И теряем деньги). Design doc:
docs/superpowers/specs/2026-07-21-adverse-selection-intel-design.md

Temperature-маркеры продублированы из app/jobs/v2_execution.py намеренно —
analytics-слой не должен зависеть от execution-job (см. §6 design doc).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analytics.edge_metrics import compute_window_metrics

# Дубль app/jobs/v2_execution.py::_TEMPERATURE_QUESTION_MARKERS (осознанно, §6 design doc)
_TEMPERATURE_MARKERS: tuple[str, ...] = ("temperature", "°f", "°c", "ºf", "ºc")


def is_temperature_market(question: str | None) -> bool:
    """True если рынок температурный (по тексту вопроса)."""
    if not question:
        return False
    q = question.lower()
    return any(marker in q for marker in _TEMPERATURE_MARKERS)


@dataclass(frozen=True)
class AdverseSelectionConfig:
    min_events: int
    wilson_threshold: float


@dataclass(frozen=True)
class AdverseCell:
    detector: str
    category: str
    n_events: int
    wins: int
    wilson_lower: float
    wilson_upper: float
    net_pnl: float
    is_temperature: bool
    is_adverse: bool


def is_adverse(n_events: int, wilson_lower: float, net_pnl: float, config: AdverseSelectionConfig) -> bool:
    """Единая точка критерия: достаточно данных И проигрыш направления И убыток."""
    return n_events >= config.min_events and wilson_lower < config.wilson_threshold and net_pnl < 0


def classify_cell(
    detector: str,
    category: str,
    outcomes: list[str],
    pnls: list[float],
    net_pnl: float,
    is_temperature: bool,
    config: AdverseSelectionConfig,
) -> AdverseCell:
    """Считает Wilson-метрики через канон edge_metrics и применяет is_adverse."""
    metrics = compute_window_metrics(outcomes, pnls)
    adverse = is_adverse(metrics.n, metrics.wilson_lower, net_pnl, config)
    return AdverseCell(
        detector=detector,
        category=category,
        n_events=metrics.n,
        wins=metrics.wins,
        wilson_lower=metrics.wilson_lower,
        wilson_upper=metrics.wilson_upper,
        net_pnl=net_pnl,
        is_temperature=is_temperature,
        is_adverse=adverse,
    )
