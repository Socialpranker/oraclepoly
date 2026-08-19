"""ml_honest_baseline_fit — one-shot job для Stage 1 (Two-Stage Pipeline plan).

В отличие от ml_meta_labeler_fit (weekly cron):
- Не активирует модель (is_active=false ВСЕГДА)
- Сохраняет под версией lgb_l2_v_honest_baseline_<date>
- Считает per-event Brier параллельно с per-row
- Раздельно репортит на HF (pre-V2) / Live (post-V2) / union
- Использует event-aware split (groups=event_id) и sample_weight=1/event_size

Цель: получить число "old Brier 0.254 with leakage vs honest per-event Brier X.XXX"
как anchor для сравнения с Layer A + Layer B в Stage 5.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog

from app.calibration.beta_calibrator import BetaCalibrator
from app.calibration.event_aware_split import event_aware_holdout_split
from app.calibration.meta_labeler import (
    MetaLabelerConfig,
    MetaLabelerExample,
    train_meta_labeler,
)
from app.calibration.per_event_metrics import count_events, per_event_brier
from app.services.database import execute, fetch

logger = structlog.get_logger()

ROLLING_DAYS = 365  # вся история — events не накапливаются быстро
MIN_TRAIN_SAMPLES = 100
HOLDOUT_FRACTION = 0.20
RANDOM_SEED = 42
V2_CUTOVER = datetime(2026, 4, 28, tzinfo=UTC)


# Tier 3.1 фичи — те же leak-free LATERAL что в ml_meta_labeler_fit (strictly
# < ds.generated_at). settle_velocity NULL для shadow-строк (LGB → missing).
_FETCH_TRAINING_SQL = """
SELECT ds.detector,
       ds.confidence       AS raw_p,
       ds.expected_edge_pct,
       -- entry_price = side-aware стоимость 1 share (см. detector_signal_service.py).
       CASE WHEN ds.side = 'YES' THEN ds.current_price
            ELSE 1.0 - ds.current_price
       END                  AS entry_price,
       ds.horizon_sec,
       ds.generated_at,
       ds.outcome,
       ds.event_id,
       ds.event_size,
       ds.archetype,
       m.category,
       m.volume_24h,
       CASE WHEN m.end_date IS NOT NULL
            THEN EXTRACT(epoch FROM (m.end_date - ds.generated_at)) / 3600.0
            ELSE NULL
       END                  AS hours_to_resolution,
       rw.recent_wr_30d,
       sv.settle_velocity_hours
  FROM detector_signals ds
  LEFT JOIN markets m ON m.polymarket_id = ds.market_id
  LEFT JOIN LATERAL (
      SELECT AVG(CASE WHEN ds2.outcome = 'WIN' THEN 1.0 ELSE 0.0 END) AS recent_wr_30d
        FROM detector_signals ds2
       WHERE ds2.detector = ds.detector
         AND ds2.resolved = true
         AND ds2.outcome IN ('WIN', 'LOSS')
         AND ds2.generated_at < ds.generated_at
         AND ds2.generated_at >= ds.generated_at - interval '30 days'
  ) rw ON true
  LEFT JOIN LATERAL (
      SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (
                 ORDER BY EXTRACT(epoch FROM (eo.settled_at - eo.filled_at)) / 3600.0
             ) AS settle_velocity_hours
        FROM execution_orders eo
        LEFT JOIN markets m2 ON m2.polymarket_id = eo.market_id
       WHERE eo.detector = ds.detector
         AND COALESCE(m2.category, 'unknown') = COALESCE(m.category, 'unknown')
         AND eo.status = 'virtual_settled'
         AND eo.filled_at IS NOT NULL
         AND eo.settled_at IS NOT NULL
         AND eo.settled_at < ds.generated_at
         AND COALESCE((eo.meta->>'reentry_excluded')::bool, false) = false
  ) sv ON true
 WHERE ds.resolved = true
   AND ds.outcome IN ('WIN', 'LOSS')
   AND ds.event_id IS NOT NULL
   AND ds.event_size IS NOT NULL
   AND ds.generated_at > now() - make_interval(days => $1)
 ORDER BY ds.generated_at ASC
"""


async def _load_l1_calibrators() -> dict[str, BetaCalibrator]:
    rows = await fetch("SELECT detector, params FROM ml_calibrators")
    out: dict[str, BetaCalibrator] = {}
    for r in rows:
        params = r["params"]
        if isinstance(params, str):
            params = json.loads(params)
        out[r["detector"]] = BetaCalibrator.deserialize(params)
    return out


def _opt_float(val: Any) -> float | None:
    """None-safe float конверсия для optional Tier 3.1 фич (БД → None/Decimal)."""
    return None if val is None else float(val)


def _row_to_example(row: dict[str, Any], cals: dict[str, BetaCalibrator]) -> MetaLabelerExample:
    detector = row["detector"]
    raw_p = float(row["raw_p"])
    cal = cals.get(detector)
    if cal is not None and cal.params is not None:
        try:
            calibrated_p = float(cal.transform(raw_p))
        except Exception:
            calibrated_p = raw_p
    else:
        calibrated_p = raw_p

    return MetaLabelerExample(
        detector_id=detector,
        calibrated_p=calibrated_p,
        edge_pct=float(row["expected_edge_pct"]),
        entry_price=float(row["entry_price"]),
        horizon_sec=int(row["horizon_sec"]),
        category=row.get("category"),
        volume_24h=float(row["volume_24h"] or 0),
        is_v2_post=row["generated_at"] > V2_CUTOVER,
        outcome=1 if row["outcome"] == "WIN" else 0,
        hours_to_resolution=_opt_float(row.get("hours_to_resolution")),
        recent_wr_30d=_opt_float(row.get("recent_wr_30d")),
        settle_velocity_hours=_opt_float(row.get("settle_velocity_hours")),
    )


def _slice_metrics(
    examples: list[MetaLabelerExample],
    event_ids: list[str],
    holdout_pred: list[float],
    holdout_idx: list[int],
    idx_filter: list[int],
) -> dict[str, Any]:
    """Per-row + per-event Brier для подмножества holdout (например HF или Live)."""
    sub = [i for i in idx_filter if i in set(holdout_idx)]
    if not sub:
        return {"n_rows": 0, "n_events": 0, "per_row_brier": None, "per_event_brier": None}

    # Map indices в массиве holdout (0..len(holdout_idx))
    holdout_pos = {i: pos for pos, i in enumerate(holdout_idx)}
    positions = [holdout_pos[i] for i in sub]

    sub_p = [holdout_pred[p] for p in positions]
    sub_y = [examples[i].outcome for i in sub]
    sub_ev = [event_ids[i] for i in sub]

    per_row = float(sum((p - y) ** 2 for p, y in zip(sub_p, sub_y, strict=True)) / len(sub_y))
    per_event = per_event_brier(sub_ev, sub_p, sub_y)
    return {
        "n_rows": len(sub),
        "n_events": count_events(sub_ev),
        "per_row_brier": round(per_row, 6),
        "per_event_brier": round(per_event, 6),
    }


async def run_ml_honest_baseline_fit() -> dict[str, Any]:
    """Запуск honest baseline. Возвращает summary с числами для memory file."""
    rows = await fetch(_FETCH_TRAINING_SQL, ROLLING_DAYS)
    if len(rows) < MIN_TRAIN_SAMPLES:
        logger.warning("honest_baseline_too_few_samples", n=len(rows), min=MIN_TRAIN_SAMPLES)
        return {"status": "skipped", "reason": "too_few_samples", "n": len(rows)}

    cals = await _load_l1_calibrators()
    rows_dicts = [dict(r) for r in rows]
    examples = [_row_to_example(r, cals) for r in rows_dicts]
    event_ids = [str(r["event_id"]) for r in rows_dicts]
    event_sizes = [int(r["event_size"]) for r in rows_dicts]
    sample_weight = [1.0 / max(es, 1) for es in event_sizes]

    cfg = MetaLabelerConfig(min_train_samples=MIN_TRAIN_SAMPLES)
    model = train_meta_labeler(
        examples,
        config=cfg,
        holdout_fraction=HOLDOUT_FRACTION,
        seed=RANDOM_SEED,
        sample_weight=sample_weight,
        groups=event_ids,
    )

    # Воспроизводим тот же event-aware split чтобы посчитать per-event Brier на holdout
    train_idx, holdout_idx = event_aware_holdout_split(event_ids, holdout_fraction=HOLDOUT_FRACTION, seed=RANDOM_SEED)
    holdout_examples = [examples[i] for i in holdout_idx]
    holdout_event_ids = [event_ids[i] for i in holdout_idx]
    holdout_y = [examples[i].outcome for i in holdout_idx]
    holdout_pred = [float(p) for p in model.predict(holdout_examples)]

    per_event_holdout = per_event_brier(holdout_event_ids, holdout_pred, holdout_y)
    n_events_holdout = count_events(holdout_event_ids)

    # Раздельно по regime: HF (pre-V2) / Live (post-V2)
    hf_idx = [i for i in holdout_idx if not examples[i].is_v2_post]
    live_idx = [i for i in holdout_idx if examples[i].is_v2_post]

    hf_metrics = _slice_metrics(examples, event_ids, holdout_pred, holdout_idx, hf_idx)
    live_metrics = _slice_metrics(examples, event_ids, holdout_pred, holdout_idx, live_idx)

    # Per-detector breakdown — anchor для сравнения Layer A+B по каждому детектору отдельно
    # (выборка кривая: D4 доминирует, D1 underrepresented после event_id enrichment)
    per_detector_metrics: dict[str, dict[str, Any]] = {}
    detectors_in_holdout = sorted({examples[i].detector_id for i in holdout_idx})
    for det in detectors_in_holdout:
        det_idx = [i for i in holdout_idx if examples[i].detector_id == det]
        per_detector_metrics[det] = _slice_metrics(examples, event_ids, holdout_pred, holdout_idx, det_idx)

    # Train-set sanity: распределение детекторов и regime, чтобы можно было
    # judge representativeness anchor'а
    train_detector_counts: dict[str, int] = {}
    train_regime_counts = {"hf_pre_v2": 0, "live_post_v2": 0}
    for i in train_idx:
        det = examples[i].detector_id
        train_detector_counts[det] = train_detector_counts.get(det, 0) + 1
        if examples[i].is_v2_post:
            train_regime_counts["live_post_v2"] += 1
        else:
            train_regime_counts["hf_pre_v2"] += 1

    version = datetime.now(UTC).strftime("lgb_l2_v_honest_baseline_%Y%m%d_%H%M%S")
    summary = {
        "status": "ok",
        "version": version,
        "n_train_rows": model.n_train,
        "n_holdout_rows": model.n_holdout,
        "n_events_holdout": n_events_holdout,
        "old_holdout_brier_per_row": round(model.holdout_brier, 6),
        "honest_holdout_per_event_brier": round(per_event_holdout, 6),
        "hf_pre_v2": hf_metrics,
        "live_post_v2": live_metrics,
        "per_detector": per_detector_metrics,
        "train_detector_counts": train_detector_counts,
        "train_regime_counts": train_regime_counts,
        "config": {
            "rolling_days": ROLLING_DAYS,
            "holdout_fraction": HOLDOUT_FRACTION,
            "split": "event_aware_groupkfold",
            "weighting": "1/event_size",
        },
    }

    # Persist (is_active=false ВСЕГДА — никаких promotions)
    payload = json.dumps(model.serialize())
    await execute(
        """
        INSERT INTO ml_meta_labeler (version, model_payload, n_train, n_holdout,
                                     train_brier, holdout_brier, fit_at, is_active)
        VALUES ($1, $2::jsonb, $3, $4, $5, $6, now(), false)
        ON CONFLICT (version) DO NOTHING
        """,
        version,
        payload,
        model.n_train,
        model.n_holdout,
        round(model.train_brier, 6),
        round(model.holdout_brier, 6),
    )

    logger.info("ml_honest_baseline_done", **summary)
    return summary
