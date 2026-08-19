"""pipeline_v2_comparison_report — daily 06:00 UTC.

Stage 5 / Two-Stage ML Pipeline.

Считает на rolling 30d resolved post-V2 signals:
- per-event Brier(monolith) vs Brier(v2)
- per-category breakdown
- top1_per_event для v2 vs random baseline
- N events / N rows для статистической силы

Возвращает JSON summary + cutover gates evaluation. НЕ меняет state — это
decision-support tool. Cutover decision принимает HUMAN после dashboard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.calibration.market_picker import top1_per_event_accuracy
from app.calibration.per_event_metrics import per_event_brier
from app.services.database import fetch

logger = structlog.get_logger()


_FETCH_SQL = """
SELECT sp.detector_signal_id, sp.detector, sp.market_id, sp.event_id, sp.side,
       sp.monolith_meta_p, sp.v2_meta_p, sp.v2_passed_gate, sp.outcome_int,
       sp.predicted_at, ds.event_size, m.category
  FROM pipeline_v2_shadow_predictions sp
  JOIN detector_signals ds ON ds.id = sp.detector_signal_id
  LEFT JOIN markets m ON m.polymarket_id = sp.market_id
 WHERE sp.resolved = true
   AND sp.outcome_int IS NOT NULL
   AND sp.predicted_at > now() - interval '30 days'
"""


async def run_pipeline_v2_comparison_report() -> dict[str, Any]:
    rows = await fetch(_FETCH_SQL)
    if not rows:
        logger.warning("pipeline_v2_comparison_no_data")
        return {"status": "no_data", "n": 0}

    rows_dicts = [dict(r) for r in rows]
    event_ids = [str(r["event_id"]) for r in rows_dicts if r["event_id"]]
    if not event_ids:
        return {"status": "no_data", "reason": "no_event_ids", "n": 0}

    # Filter to rows with event_id (Layer A нужен event_id)
    valid = [r for r in rows_dicts if r["event_id"]]
    event_ids = [str(r["event_id"]) for r in valid]
    mono_p = [float(r["monolith_meta_p"]) for r in valid]
    v2_p = [float(r["v2_meta_p"]) for r in valid]
    y = [int(r["outcome_int"]) for r in valid]

    # Overall per-event Brier
    mono_brier = per_event_brier(event_ids, mono_p, y)
    v2_brier = per_event_brier(event_ids, v2_p, y)
    n_events = len(set(event_ids))
    n_rows = len(valid)

    # Top-1 per-event accuracy
    mono_top1 = top1_per_event_accuracy(event_ids, mono_p, y)
    v2_top1 = top1_per_event_accuracy(event_ids, v2_p, y)

    # Per-category breakdown
    by_cat: dict[str, list[int]] = {}
    for i, r in enumerate(valid):
        cat = r["category"] or "Unknown"
        by_cat.setdefault(cat, []).append(i)

    per_category: dict[str, dict[str, Any]] = {}
    for cat, idxs in by_cat.items():
        cat_evs = [event_ids[i] for i in idxs]
        cat_mono = [mono_p[i] for i in idxs]
        cat_v2 = [v2_p[i] for i in idxs]
        cat_y = [y[i] for i in idxs]
        per_category[cat] = {
            "n_events": len(set(cat_evs)),
            "n_rows": len(idxs),
            "mono_brier": round(per_event_brier(cat_evs, cat_mono, cat_y), 6),
            "v2_brier": round(per_event_brier(cat_evs, cat_v2, cat_y), 6),
            "win_rate": round(sum(cat_y) / max(len(cat_y), 1), 4),
        }

    # Cutover gates evaluation
    gate_brier = v2_brier < mono_brier
    gate_n_events = n_events >= 50
    gate_no_category_regression = all(
        m["v2_brier"] <= m["mono_brier"] * 1.10 or m["n_events"] < 10 for m in per_category.values()
    )
    all_passed = gate_brier and gate_n_events and gate_no_category_regression

    summary = {
        "status": "ok",
        "as_of": datetime.now(UTC).isoformat(),
        "n_rows": n_rows,
        "n_events": n_events,
        "mono_brier": round(mono_brier, 6),
        "v2_brier": round(v2_brier, 6),
        "mono_top1_per_event": round(mono_top1, 4),
        "v2_top1_per_event": round(v2_top1, 4),
        "per_category": per_category,
        "gates": {
            "brier_v2_better_than_mono": gate_brier,
            "n_events_ge_50": gate_n_events,
            "no_category_regression": gate_no_category_regression,
            "all_passed": all_passed,
        },
    }
    logger.info(
        "pipeline_v2_comparison_report_done",
        n_events=n_events,
        v2_better=gate_brier,
        all_gates_passed=all_passed,
    )
    return summary
