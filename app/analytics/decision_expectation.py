"""Чистая логика ожиданий C4 — резолв метрики по scope и классификация исхода.

Без I/O: весь SQL живёт в app/jobs/decision_reconcile.py и app/api/telegram.py.
Спека: docs/superpowers/specs/2026-07-27-phase2-c4-decision-reconcile-design.md §2, §4.1.

Ключевая поправка к разведке: policy:dq_audit пишется с kind='dq' (dq_audit_daily.py:96),
а НЕ kind='monitoring' — наивное переиспользование _MONITOR_RUNS_SQL не нашло бы строку
и молча вернуло бы «нет данных».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

IMPROVED = "improved"
UNCHANGED = "unchanged"
WORSENED = "worsened"
NOT_APPLICABLE = "not_applicable"

METRIC_ROI = "roi"
METRIC_VERDICT = "verdict"
METRIC_NONE = "none"

EXPECT_ROI = "roi_not_below_baseline"
EXPECT_VERDICT = "verdict_improves"

# Упорядочивание вердиктов DQ: чем больше, тем здоровее.
_VERDICT_ORDER = {"RED": 0, "AMBER": 1, "GREEN": 2}

# Единственный scope-источник категориальной метрики: имя run'а в agent_runs.
_DQ_RUN_NAME = "dq_audit_daily"


@dataclass(frozen=True)
class MetricSpec:
    """Какой метрикой сверять решение по данному scope."""

    kind: str  # METRIC_ROI | METRIC_VERDICT | METRIC_NONE
    entity: str | None = None  # детектор для roi, имя run'а для verdict
    reason: str | None = None  # почему источника нет (только для METRIC_NONE)


def resolve_metric(scope: str) -> MetricSpec:
    """Метрика по префиксу scope (конвенция "type:value", policy_engine.py:115).

    Разбор по префиксу, а не по списку вердиктов: новый вердикт с тем же scope
    не сломает правило.
    """
    if scope.startswith("detector:"):
        # detector:X и detector:X:category:Y — оба резолвятся в детектор X.
        entity = scope.split(":", 2)[1]
        if not entity:
            return MetricSpec(kind=METRIC_NONE, reason="empty detector in scope")
        return MetricSpec(kind=METRIC_ROI, entity=entity)

    if scope == "policy:dq_audit":
        return MetricSpec(kind=METRIC_VERDICT, entity=_DQ_RUN_NAME)

    if scope == "policy:ips":
        return MetricSpec(
            kind=METRIC_NONE,
            reason="IPS gate runs inside policy_engine and writes no agent_runs row",
        )

    return MetricSpec(kind=METRIC_NONE, reason=f"no metric source for scope {scope!r}")


def build_expectation(spec: MetricSpec, baseline: Any, now: datetime, window_days: int) -> dict[str, Any]:
    """Ожидание для записи в agent_actions.effect + due_date.

    window_days приходит из PolicyThresholds.drawdown_window_days — сверять раньше,
    чем окно метрики обновилось, значит мерить решение по сделкам до него (спека §2 п.4).
    """
    expectation = EXPECT_ROI if spec.kind == METRIC_ROI else EXPECT_VERDICT
    return {
        "metric": spec.kind,
        "entity": spec.entity,
        "baseline": baseline,
        "expectation": expectation,
        "due_date": now + timedelta(days=window_days),
    }


def classify_numeric(baseline: float, actual: float, band: float = 0.01) -> str:
    """improved | unchanged | worsened. Полоса ±band вокруг baseline — шум.

    Граница включительно: |delta| == band → UNCHANGED (движение должно быть строго
    больше полосы, чтобы считаться движением).

    round(..., 9) — против float-погрешности: -0.32 - (-0.33) в double даёт
    0.010000000000000009, что строго больше band=0.01, и точная граница молча
    классифицировалась бы как движение. 1e-9 на девять порядков меньше полосы,
    содержательных значений ROI не задевает.
    """
    delta = round(actual - baseline, 9)
    if abs(delta) <= band:
        return UNCHANGED
    return IMPROVED if delta > 0 else WORSENED


def classify_verdict(baseline: str, actual: str) -> str:
    """improved | unchanged | worsened по шкале RED < AMBER < GREEN.

    Неизвестный вердикт с любой стороны → UNCHANGED (не выдумываем движение).
    """
    b = _VERDICT_ORDER.get(baseline)
    a = _VERDICT_ORDER.get(actual)
    if b is None or a is None:
        return UNCHANGED
    if a > b:
        return IMPROVED
    if a < b:
        return WORSENED
    return UNCHANGED
