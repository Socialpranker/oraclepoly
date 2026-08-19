"""DQ-мини-аудит — чистый слой классификации (без DB).

Phase 2 C0. Design doc:
docs/superpowers/specs/2026-07-23-portfolio-agent-phase2-policy-engine-design.md §2.1.
4 проверки: freshness, completeness, EXPIRED-share, referential integrity.
Вердикты — фиксированный словарь GREEN|AMBER|RED; RED в C1 блокирует
risk-вердикты policy engine. Пороги приходят из policy_rules (джоба),
здесь — только математика сравнения.
"""

from __future__ import annotations

from dataclasses import dataclass

VERDICT_GREEN = "GREEN"
VERDICT_AMBER = "AMBER"
VERDICT_RED = "RED"

_SEVERITY = {VERDICT_GREEN: 0, VERDICT_AMBER: 1, VERDICT_RED: 2}


@dataclass(frozen=True)
class DqThresholds:
    freshness_markets_amber_minutes: float
    freshness_markets_red_minutes: float
    freshness_signals_amber_minutes: float
    freshness_signals_red_minutes: float
    completeness_end_date_amber_pct: float
    completeness_end_date_red_pct: float
    completeness_tokens_amber_pct: float
    completeness_tokens_red_pct: float
    expired_share_amber_pct: float
    expired_share_red_pct: float
    expired_min_resolved: int
    ri_orphan_amber_pct: float
    ri_orphan_red_pct: float
    lookback_days: int

    @classmethod
    def from_params(cls, params: dict) -> DqThresholds:
        return cls(
            freshness_markets_amber_minutes=float(params["freshness_markets_amber_minutes"]),
            freshness_markets_red_minutes=float(params["freshness_markets_red_minutes"]),
            freshness_signals_amber_minutes=float(params["freshness_signals_amber_minutes"]),
            freshness_signals_red_minutes=float(params["freshness_signals_red_minutes"]),
            completeness_end_date_amber_pct=float(params["completeness_end_date_amber_pct"]),
            completeness_end_date_red_pct=float(params["completeness_end_date_red_pct"]),
            completeness_tokens_amber_pct=float(params["completeness_tokens_amber_pct"]),
            completeness_tokens_red_pct=float(params["completeness_tokens_red_pct"]),
            expired_share_amber_pct=float(params["expired_share_amber_pct"]),
            expired_share_red_pct=float(params["expired_share_red_pct"]),
            expired_min_resolved=int(params["expired_min_resolved"]),
            ri_orphan_amber_pct=float(params["ri_orphan_amber_pct"]),
            ri_orphan_red_pct=float(params["ri_orphan_red_pct"]),
            lookback_days=int(params["lookback_days"]),
        )


@dataclass(frozen=True)
class DqCheckResult:
    check: str  # напр. "freshness:markets", "ri:orphans"
    verdict: str  # GREEN | AMBER | RED
    measured: float  # измеренное значение (минуты или %)
    amber_threshold: float
    red_threshold: float
    detail: str  # человекочитаемая строка для TG/metrics


def _classify_value(value: float, amber: float, red: float) -> str:
    """Пороги включительные в сторону худшего вердикта (fail-closed)."""
    if value >= red:
        return VERDICT_RED
    if value >= amber:
        return VERDICT_AMBER
    return VERDICT_GREEN


def classify_freshness(
    age_minutes: float | None, amber_minutes: float, red_minutes: float, source: str
) -> DqCheckResult:
    """Возраст последней записи источника. age=None (источник пуст) → RED."""
    if age_minutes is None:
        return DqCheckResult(
            check=f"freshness:{source}",
            verdict=VERDICT_RED,
            measured=-1.0,
            amber_threshold=amber_minutes,
            red_threshold=red_minutes,
            detail=f"{source}: источник пуст (нет строк в окне)",
        )
    verdict = _classify_value(age_minutes, amber_minutes, red_minutes)
    return DqCheckResult(
        check=f"freshness:{source}",
        verdict=verdict,
        measured=round(age_minutes, 1),
        amber_threshold=amber_minutes,
        red_threshold=red_minutes,
        detail=f"{source}: возраст последней записи {age_minutes:.0f} мин",
    )


def classify_share(check: str, share_pct: float, amber_pct: float, red_pct: float, detail: str) -> DqCheckResult:
    """Классификация доли (%): completeness-дыры, RI-сироты."""
    return DqCheckResult(
        check=check,
        verdict=_classify_value(share_pct, amber_pct, red_pct),
        measured=round(share_pct, 2),
        amber_threshold=amber_pct,
        red_threshold=red_pct,
        detail=detail,
    )


def classify_expired_share(expired: int, resolved_total: int, thresholds: DqThresholds) -> DqCheckResult:
    """EXPIRED-доля среди resolved. 100% EXPIRED = маркер ID-бага (M2/D45).

    При resolved_total < expired_min_resolved — GREEN с пометкой: недостаточно
    данных для статистического вывода, не наказываем тихие периоды.
    """
    if resolved_total < thresholds.expired_min_resolved:
        return DqCheckResult(
            check="expired_share",
            verdict=VERDICT_GREEN,
            measured=0.0,
            amber_threshold=thresholds.expired_share_amber_pct,
            red_threshold=thresholds.expired_share_red_pct,
            detail=f"resolved n={resolved_total} < min {thresholds.expired_min_resolved}, недостаточно данных",
        )
    share_pct = 100.0 * expired / resolved_total
    return DqCheckResult(
        check="expired_share",
        verdict=_classify_value(share_pct, thresholds.expired_share_amber_pct, thresholds.expired_share_red_pct),
        measured=round(share_pct, 2),
        amber_threshold=thresholds.expired_share_amber_pct,
        red_threshold=thresholds.expired_share_red_pct,
        detail=f"EXPIRED {expired}/{resolved_total} = {share_pct:.1f}%",
    )


def overall_verdict(results: list[DqCheckResult]) -> str:
    """Максимум по серьёзности. Пустой список = нет данных = RED (fail-closed)."""
    if not results:
        return VERDICT_RED
    return max((r.verdict for r in results), key=lambda v: _SEVERITY[v])
