"""pipeline_v2_drift_check — weekly Sunday 06:30 UTC.

Stage 5.5 / Two-Stage ML Pipeline.

Активируется ПОСЛЕ Stage 5 cutover. Сравнивает rolling 7d per-event Brier
свежих live signals с training Brier active Layer A + Layer B моделей.
Alert при degradation ratio ≥ 1.30.

Не делает auto-demote по умолчанию — только log alert. HUMAN решает rollback.
"""

from __future__ import annotations

import math
from typing import Any

import structlog

from app.calibration.per_event_metrics import per_event_brier
from app.services.database import fetch, fetchrow

logger = structlog.get_logger()

ALERT_RATIO = 1.30
SEVERE_RATIO = 1.50
MIN_LIVE_EVENTS = 30  # Под этим — undefined ratio, skip


_FETCH_LAYER_A_BASELINE = """
SELECT version, holdout_per_event_brier
  FROM ml_event_selector WHERE is_active = true
"""

_FETCH_LAYER_B_BASELINE = """
SELECT version, holdout_per_event_brier
  FROM ml_market_picker WHERE is_active = true
"""

_FETCH_LIVE_SHADOW_SQL = """
SELECT sp.event_id,
       sp.v2_p_event,
       sp.v2_meta_p,
       sp.outcome_int
  FROM pipeline_v2_shadow_predictions sp
 WHERE sp.resolved = true
   AND sp.outcome_int IS NOT NULL
   AND sp.v2_passed_gate = true
   AND sp.predicted_at > now() - interval '7 days'
"""


def _compute_drift_status(
    training_brier: float,
    live_brier: float,
    alert_ratio: float = ALERT_RATIO,
    severe_ratio: float = SEVERE_RATIO,
) -> dict[str, Any]:
    """Returns drift status dict."""
    if training_brier <= 0:
        return {
            "alert": True,
            "severity": "severe",
            "ratio": math.inf,
            "training_brier": training_brier,
            "live_brier": live_brier,
            "reason": "training_brier=0 degenerate",
        }
    ratio = live_brier / training_brier
    if ratio >= severe_ratio:
        sev = "severe"
        alert = True
    elif ratio >= alert_ratio:
        sev = "moderate"
        alert = True
    else:
        sev = "ok"
        alert = False
    return {
        "alert": alert,
        "severity": sev,
        "ratio": ratio,
        "training_brier": training_brier,
        "live_brier": live_brier,
    }


async def run_pipeline_v2_drift_check() -> dict[str, Any]:
    """Cron entry point."""
    layer_a = await fetchrow(_FETCH_LAYER_A_BASELINE)
    layer_b = await fetchrow(_FETCH_LAYER_B_BASELINE)
    if layer_a is None or layer_b is None:
        return {"status": "skipped", "reason": "no_active_layer_models"}

    rows = await fetch(_FETCH_LIVE_SHADOW_SQL)
    if not rows:
        return {"status": "skipped", "reason": "no_live_resolved_data"}

    # Layer A drift: per-event на event-level (агрегируем по уникальным events)
    by_event: dict[str, dict[str, Any]] = {}
    for r in rows:
        ev = str(r["event_id"] or "")
        if not ev:
            continue
        slot = by_event.setdefault(ev, {"p_event": None, "outcomes": []})
        if r["v2_p_event"] is not None:
            slot["p_event"] = float(r["v2_p_event"])
        slot["outcomes"].append(int(r["outcome_int"]))

    # Layer A: outcome event-level = 1 если ХОТЬ ОДИН market в event'е выиграл
    layer_a_event_ids: list[str] = []
    layer_a_p: list[float] = []
    layer_a_y: list[int] = []
    for ev, slot in by_event.items():
        if slot["p_event"] is None:
            continue
        layer_a_event_ids.append(ev)
        layer_a_p.append(slot["p_event"])
        layer_a_y.append(1 if any(y == 1 for y in slot["outcomes"]) else 0)

    if len(set(layer_a_event_ids)) < MIN_LIVE_EVENTS:
        return {
            "status": "skipped",
            "reason": "too_few_live_events",
            "n_events": len(set(layer_a_event_ids)),
            "min_required": MIN_LIVE_EVENTS,
        }

    layer_a_live_brier = per_event_brier(layer_a_event_ids, layer_a_p, layer_a_y)

    # Layer B drift: per-event на per-row predictions
    rows_dicts = [dict(r) for r in rows if r["event_id"] is not None]
    layer_b_event_ids = [str(r["event_id"]) for r in rows_dicts]
    layer_b_p = [float(r["v2_meta_p"]) for r in rows_dicts]
    layer_b_y = [int(r["outcome_int"]) for r in rows_dicts]
    layer_b_live_brier = per_event_brier(layer_b_event_ids, layer_b_p, layer_b_y)

    layer_a_status = _compute_drift_status(
        training_brier=float(layer_a["holdout_per_event_brier"]),
        live_brier=layer_a_live_brier,
    )
    layer_b_status = _compute_drift_status(
        training_brier=float(layer_b["holdout_per_event_brier"]),
        live_brier=layer_b_live_brier,
    )

    if layer_a_status["alert"]:
        logger.warning(
            "layer_a_drift_alert",
            severity=layer_a_status["severity"],
            ratio=round(layer_a_status["ratio"], 3),
            training=layer_a_status["training_brier"],
            live=round(layer_a_status["live_brier"], 4),
            version=layer_a["version"],
        )
    if layer_b_status["alert"]:
        logger.warning(
            "layer_b_drift_alert",
            severity=layer_b_status["severity"],
            ratio=round(layer_b_status["ratio"], 3),
            training=layer_b_status["training_brier"],
            live=round(layer_b_status["live_brier"], 4),
            version=layer_b["version"],
        )

    return {
        "status": "ok",
        "n_live_events": len(set(layer_a_event_ids)),
        "n_live_rows": len(rows_dicts),
        "layer_a": {
            "version": layer_a["version"],
            "training_brier": float(layer_a["holdout_per_event_brier"]),
            "live_brier": round(layer_a_live_brier, 6),
            "ratio": round(layer_a_status["ratio"], 3),
            "alert": layer_a_status["alert"],
            "severity": layer_a_status["severity"],
        },
        "layer_b": {
            "version": layer_b["version"],
            "training_brier": float(layer_b["holdout_per_event_brier"]),
            "live_brier": round(layer_b_live_brier, 6),
            "ratio": round(layer_b_status["ratio"], 3),
            "alert": layer_b_status["alert"],
            "severity": layer_b_status["severity"],
        },
    }
