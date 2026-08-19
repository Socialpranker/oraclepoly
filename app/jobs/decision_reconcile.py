"""Decision reconcile — ежедневная сверка ожиданий C4 (Phase 2).

Выбирает ожидания с наступившим due_date (partial index idx_agent_actions_due_pending),
заново измеряет ту же метрику, пишет исход в reconciled_at + reconciliation_result.

TG НЕ шлёт: сверка накапливает данные для quarterly-ретро, ежедневный шум на 1-2 строки
не нужен (спека §4.3). Идемпотентность — reconciled_at IS NULL в WHERE выборки.

Design doc: docs/superpowers/specs/2026-07-27-phase2-c4-decision-reconcile-design.md §4.3
Cron: daily 08:00 UTC (после policy-engine 07:30, чтобы не конкурировать за те же агрегаты).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog

from app.analytics.decision_expectation import (
    METRIC_ROI,
    NOT_APPLICABLE,
    classify_numeric,
    classify_verdict,
)
from app.analytics.policy_engine import _DEFAULT_PARAMS, PolicyThresholds
from app.services.database import execute, fetch, fetchrow

logger = structlog.get_logger()

# Выборка «пора сверять» — условие дословно повторяет WHERE partial-индекса.
_DUE_SQL = """
SELECT id, effect
  FROM agent_actions
 WHERE due_date IS NOT NULL
   AND due_date <= now()
   AND reconciled_at IS NULL
 ORDER BY due_date
"""

# Тот же bankroll-weighted агрегат, что при измерении baseline (telegram.py).
_ACTUAL_ROI_SQL = """
SELECT CASE WHEN SUM(stake) > 0 THEN SUM(net_pnl) / SUM(stake) END AS roi
  FROM execution_orders
 WHERE execution_mode IN ('maker', 'taker')
   AND status = 'settled'
   AND settled_at > now() - ($2 || ' days')::interval
   AND detector = $1
"""

_ACTUAL_VERDICT_SQL = """
SELECT verdict
  FROM agent_runs
 WHERE kind = 'dq' AND name = $1
 ORDER BY started_at DESC
 LIMIT 1
"""

_CLOSE_SQL = """
UPDATE agent_actions
   SET reconciliation_result = $2::jsonb, reconciled_at = $3
 WHERE id = $1 AND reconciled_at IS NULL
"""


async def _measure(metric: str, entity: str, window_days: int) -> tuple[Any, str | None]:
    """Актуальное значение метрики: (value, reason_if_none)."""
    try:
        if metric == METRIC_ROI:
            row = await fetchrow(_ACTUAL_ROI_SQL, entity, str(window_days))
            if row is None or row["roi"] is None:
                return None, f"no settled orders for detector {entity!r} in window"
            return float(row["roi"]), None
        row = await fetchrow(_ACTUAL_VERDICT_SQL, entity)
        if row is None:
            return None, f"no dq run named {entity!r}"
        return str(row["verdict"]), None
    except Exception as e:
        logger.warning("decision_reconcile_measure_failed", metric=metric, entity=entity, error=str(e))
        return None, "measurement failed"


def _parse_expectation(effect: Any) -> dict | None:
    """expectation из effect; битый payload → None (закроем как not_applicable)."""
    if isinstance(effect, str):
        try:
            effect = json.loads(effect)
        except (ValueError, TypeError):
            return None
    if not isinstance(effect, dict):
        return None
    exp = effect.get("expectation")
    if not isinstance(exp, dict) or not exp.get("metric") or not exp.get("entity"):
        return None
    # "baseline" in exp, а не exp.get("baseline") truthy — легитимный baseline может быть 0.0.
    if "baseline" not in exp:
        return None
    return exp


async def run_decision_reconcile() -> dict[str, Any]:
    rows = await fetch(_DUE_SQL)
    if not rows:
        logger.info("decision_reconcile_no_due")
        return {"status": "no_due", "n_due": 0, "n_reconciled": 0, "outcomes": {}}

    window_days = PolicyThresholds.from_params(_DEFAULT_PARAMS).drawdown_window_days
    outcomes: dict[str, int] = {}
    n_reconciled = 0

    for row in rows:
        try:
            exp = _parse_expectation(row["effect"])
            now = datetime.now(UTC)
            if exp is None:
                result = {"outcome": NOT_APPLICABLE, "reason": "expectation payload missing or malformed"}
            else:
                actual, reason = await _measure(exp["metric"], exp["entity"], window_days)
                if actual is None:
                    result = {"outcome": NOT_APPLICABLE, "reason": reason}
                else:
                    baseline = exp["baseline"]
                    outcome = (
                        classify_numeric(float(baseline), float(actual))
                        if exp["metric"] == METRIC_ROI
                        else classify_verdict(str(baseline), str(actual))
                    )
                    result = {"outcome": outcome, "actual": actual, "baseline": baseline}
            result["measured_at"] = now.isoformat()

            status = await execute(_CLOSE_SQL, row["id"], json.dumps(result), now)
            if status.endswith(" 1"):
                n_reconciled += 1
                outcomes[result["outcome"]] = outcomes.get(result["outcome"], 0) + 1
        except Exception as e:
            # logger.exception (не warning) — traceback обязателен: если сбой системный
            # и повторяется на каждой строке, по одному str(e) не отличить, где упало
            # (_parse_expectation / classify_* / json.dumps / сам execute).
            logger.exception("decision_reconcile_row_failed", action_id=row["id"], error=str(e))
            continue

    summary = {"status": "ok", "n_due": len(rows), "n_reconciled": n_reconciled, "outcomes": outcomes}
    logger.info("decision_reconcile_done", **summary)
    return summary
