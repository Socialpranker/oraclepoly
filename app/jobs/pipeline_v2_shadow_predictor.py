"""pipeline_v2_shadow_predictor — каждые 10 минут (Stage 5).

Stage 5 / Two-Stage ML Pipeline.

Для каждого свежего unresolved detector_signal:
1. Прогоняет monolith path (fetch_calibrated_detector_signals) → meta_p_mono
2. Прогоняет v2 path (fetch_two_stage_calibrated_signals) → meta_p_v2 + p_event + gate
3. Если активен EV-regressor — также прогоняет его → predicted net_roi
4. INSERT в pipeline_v2_shadow_predictions ON CONFLICT DO NOTHING

Не меняет inference path в production — только наблюдает (immutable audit trail).
"""

from __future__ import annotations

from typing import Any

import structlog

from app.services.database import execute, fetch, fetchrow

logger = structlog.get_logger()


_INSERT_SQL = """
INSERT INTO pipeline_v2_shadow_predictions (
    detector_signal_id, detector, market_id, event_id, side,
    monolith_calibrated_p, monolith_meta_p,
    v2_calibrated_p, v2_p_event, v2_passed_gate, v2_meta_p,
    monolith_version, layer_a_version, layer_b_version,
    v2_ev_pred, v2_ev_version
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
ON CONFLICT (detector_signal_id) DO NOTHING
"""


async def run_pipeline_v2_shadow_predictor() -> dict[str, Any]:
    """Cron entry point. Returns counters for monitoring."""
    from app.services.detector_signal_service import fetch_calibrated_detector_signals
    from app.services.two_stage_inference import (
        fetch_ev_regressor_signals,
        fetch_two_stage_calibrated_signals,
    )

    monolith = await fetch_calibrated_detector_signals()
    v2 = await fetch_two_stage_calibrated_signals()
    ev = await fetch_ev_regressor_signals()  # пусто если EV-regressor не fit'нут

    # Index by detector_signal_id для merge
    mono_by: dict[str, dict[str, Any]] = {s["detector_signal_id"]: s for s in monolith}
    v2_by: dict[str, dict[str, Any]] = {s["detector_signal_id"]: s for s in v2}
    ev_by: dict[str, dict[str, Any]] = {s["detector_signal_id"]: s for s in ev}
    common_ids = set(mono_by.keys()) & set(v2_by.keys())
    if not common_ids:
        return {"status": "ok", "n_inserted": 0, "n_seen_common": 0, "n_mono": len(monolith), "n_v2": len(v2)}

    # Active model versions для audit trail
    mono_v = await fetchrow("SELECT version FROM ml_meta_labeler WHERE is_active = true")
    la_v = await fetchrow("SELECT version FROM ml_event_selector WHERE is_active = true")
    lb_v = await fetchrow("SELECT version FROM ml_market_picker WHERE is_active = true AND mode = 'binary'")
    ev_v = await fetchrow("SELECT version FROM ml_market_picker WHERE is_active = true AND mode = 'ev_regression'")
    mono_version = mono_v["version"] if mono_v else None
    layer_a_version = la_v["version"] if la_v else None
    layer_b_version = lb_v["version"] if lb_v else None
    ev_version = ev_v["version"] if ev_v else None

    # Pre-flight: убедимся что детекторы знают сколько signals уже сохранены
    pre_existing_q = """SELECT COUNT(*) AS n FROM pipeline_v2_shadow_predictions
                        WHERE detector_signal_id = ANY($1::uuid[])"""
    existing_row = await fetch(pre_existing_q, list(common_ids))
    pre_existing_n = int(existing_row[0]["n"]) if existing_row else 0

    n_attempted = 0
    n_failed = 0
    for sid in common_ids:
        m = mono_by[sid]
        v = v2_by[sid]
        ev_pred = None
        if sid in ev_by and ev_by[sid].get("ev_pred") is not None:
            ev_pred = round(float(ev_by[sid]["ev_pred"]), 5)
        try:
            await execute(
                _INSERT_SQL,
                sid,
                m["detector"],
                m["market_id"],
                m["event_id"],
                m["side"],
                round(float(m["calibrated_p"]), 4),
                round(float(m["meta_p"]), 4),
                round(float(v["calibrated_p"]), 4),
                round(float(v["p_event"]), 4) if v["p_event"] is not None else None,
                bool(v["passed_layer_a_gate"]),
                round(float(v["meta_p"]), 4),
                mono_version,
                layer_a_version,
                layer_b_version,
                ev_pred,
                ev_version if ev_pred is not None else None,
            )
            n_attempted += 1
        except Exception as exc:
            n_failed += 1
            logger.warning("shadow_predictor_insert_failed", sid=str(sid), error=str(exc))

    n_inserted = n_attempted - pre_existing_n
    if n_inserted < 0:
        n_inserted = 0  # safety

    logger.info(
        "pipeline_v2_shadow_predictor_done",
        n_inserted=n_inserted,
        n_seen_common=len(common_ids),
        n_failed=n_failed,
    )
    return {
        "status": "ok",
        "n_inserted": n_inserted,
        "n_seen_common": len(common_ids),
        "n_failed": n_failed,
    }
