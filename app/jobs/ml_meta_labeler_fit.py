"""ML meta-labeler fit (Phase E.2) — еженедельно (Sunday 04:00 UTC, после L1).

1. Собирает resolved detector_signals за rolling 90d
2. Применяет L1 Beta calibration (если fit'ed) к каждому signal'у
3. Обогащает features из markets (entry_price, volume_24h, category, is_v2_post)
4. Train LightGBM с time-based split (последние 20% = holdout)
5. UPSERT в ml_meta_labeler с version=timestamp
6. **Validation gate** (validate_model): per-band AUC + ECE + BSS + cal slope
7. Если promote ratio + gate PASS → UPDATE is_active

Если validation gate FAIL — модель сохранена с is_active=false, alert в TG.
Это защита от D-MLCalibrator (global AUC 0.94, per-band 0.52, ROI -18%).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog

from app.calibration.beta_calibrator import BetaCalibrator
from app.calibration.meta_labeler import (
    MetaLabelerConfig,
    MetaLabelerExample,
    train_meta_labeler,
)
from app.calibration.validation import format_report, validate_model
from app.services.database import execute, fetch, fetchrow

logger = structlog.get_logger()

ROLLING_DAYS = 90
HOLDOUT_FRACTION = 0.20
MIN_TRAIN_SAMPLES = 100
PROMOTION_BRIER_RATIO = 0.95  # new должен быть на ≥5% лучше чтобы promote
PROMOTION_BRIER_RATIO_SCHEMA_MISMATCH = 1.10  # +10% deg допустим если active модель не покрывает текущие detectors
V2_CUTOVER = datetime(2026, 4, 28, tzinfo=UTC)
VALIDATION_GATE_ENABLED = True  # block promote if validation_report.passes_gate is False


# Tier 3.1 фичи (hours_to_resolution / recent_wr_30d / settle_velocity_hours).
# Все три leak-free: считаются на момент ds.generated_at, без peek в будущее.
# recent_wr_30d / settle_velocity — LATERAL strictly < ds.generated_at.
# settle_velocity NULL для большинства строк (execution_orders только у virtual/
# live) — это ожидаемо, LGB трактует NaN как missing.
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
       m.category,
       m.volume_24h,
       -- Tier 3.1: часы до end_date на момент сигнала (sharper signal у близких).
       CASE WHEN m.end_date IS NOT NULL
            THEN EXTRACT(epoch FROM (m.end_date - ds.generated_at)) / 3600.0
            ELSE NULL
       END                  AS hours_to_resolution,
       rw.recent_wr_30d,
       sv.settle_velocity_hours
  FROM detector_signals ds
  LEFT JOIN markets m ON m.polymarket_id = ds.market_id
  LEFT JOIN LATERAL (
      -- rolling WR детектора <30d strictly до текущего сигнала (anti-decay)
      SELECT AVG(CASE WHEN ds2.outcome = 'WIN' THEN 1.0 ELSE 0.0 END) AS recent_wr_30d
        FROM detector_signals ds2
       WHERE ds2.detector = ds.detector
         AND ds2.resolved = true
         AND ds2.outcome IN ('WIN', 'LOSS')
         AND ds2.generated_at < ds.generated_at
         AND ds2.generated_at >= ds.generated_at - interval '30 days'
  ) rw ON true
  LEFT JOIN LATERAL (
      -- медиана settle-времени для (detector, category) по virtual_settled ордерам
      -- strictly до текущего сигнала. NULL если ордеров нет (shadow-детекторы).
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
   AND ds.generated_at > now() - make_interval(days => $1)
 ORDER BY ds.generated_at ASC
"""

_FETCH_CALIBRATORS_SQL = """
SELECT detector, params FROM ml_calibrators
"""


async def _load_l1_calibrators() -> dict[str, BetaCalibrator]:
    rows = await fetch(_FETCH_CALIBRATORS_SQL)
    cals: dict[str, BetaCalibrator] = {}
    for r in rows:
        params = r["params"]
        if isinstance(params, str):
            params = json.loads(params)
        cals[r["detector"]] = BetaCalibrator.deserialize(params)
    return cals


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
        event_id=row.get("event_id"),
        hours_to_resolution=_opt_float(row.get("hours_to_resolution")),
        recent_wr_30d=_opt_float(row.get("recent_wr_30d")),
        settle_velocity_hours=_opt_float(row.get("settle_velocity_hours")),
    )


async def _get_active_model_meta() -> dict[str, Any] | None:
    row = await fetchrow("SELECT version, holdout_brier, model_payload FROM ml_meta_labeler WHERE is_active = true")
    if row is None:
        return None
    return dict(row)


def _model_features(payload: Any) -> set[str]:
    """Извлекает feature_names из serialized payload."""
    import json as _json

    if isinstance(payload, str):
        payload = _json.loads(payload)
    if not isinstance(payload, dict):
        return set()
    return set(payload.get("feature_names", []))


def _validation_gate_check(
    holdout_examples: list[MetaLabelerExample],
    holdout_predictions: list[float],
    name: str,
) -> tuple[bool, list[str]]:
    """Run validate_model() на holdout и проверить HARD gate criteria.

    Возвращает (passes_gate, failures). Если данных недостаточно —
    (True, ["validation_skipped_insufficient_data"]).
    """
    if not VALIDATION_GATE_ENABLED or not holdout_examples:
        return True, []

    if len(holdout_examples) < MIN_TRAIN_SAMPLES:
        return True, [f"validation_skipped_holdout_too_small_{len(holdout_examples)}"]

    y_true = [int(ex.outcome) for ex in holdout_examples]
    if len(set(y_true)) < 2:
        return True, ["validation_skipped_one_class_holdout"]

    # market_price = entry_price (probability quoted by market for buying side)
    # segment_value = entry_price для bucketing
    # event_ids опционально (NULL → используем index)
    entry_prices = [float(ex.entry_price) for ex in holdout_examples]
    event_ids = [ex.event_id or f"_row_{i}" for i, ex in enumerate(holdout_examples)]

    report = validate_model(
        name=name,
        y_true=y_true,
        y_pred=holdout_predictions,
        market_price=entry_prices,
        segment_value=entry_prices,
        event_ids=event_ids,
        roi_per_row=None,  # bootstrap CI пропустим — нет realized_roi на holdout
    )
    logger.info(
        "ml_meta_labeler_validation_report",
        name=name,
        passes_gate=report.passes_gate,
        failures=report.failures,
        overall_auc=report.overall_auc,
        bss=report.brier_skill_score,
        cal_slope=report.calibration_slope,
        per_segment=[{"segment": s.segment, "n": s.n, "auc": s.auc, "ece": s.ece} for s in report.per_segment],
    )
    # Print full report для cron-runner логов
    print(format_report(report), flush=True)
    return report.passes_gate, report.failures


async def _persist_and_maybe_promote(
    version: str,
    payload_json: str,
    payload_dict: dict[str, Any],
    n_train: int,
    n_holdout: int,
    train_brier: float,
    holdout_brier: float,
    holdout_examples: list[MetaLabelerExample] | None = None,
    holdout_predictions: list[float] | None = None,
) -> tuple[bool, str]:
    """Возвращает (promoted, reason).

    Promotion rules:
    1. No active model → promote (если validation gate passes).
    2. Active model schema covers current detectors → standard 0.95× rule.
    3. Active model schema MISSES current detectors → relaxed 1.10× rule.
    4. **Validation gate** (validate_model): per-band AUC + ECE + BSS + cal slope.
       Если gate FAIL — promote блокируется независимо от Brier ratio.
    """
    await execute(
        """
        INSERT INTO ml_meta_labeler (
            version, model_payload, n_train, n_holdout,
            train_brier, holdout_brier, fit_at, is_active
        )
        VALUES ($1, $2::jsonb, $3, $4, $5, $6, now(), false)
        ON CONFLICT (version) DO NOTHING
        """,
        version,
        payload_json,
        n_train,
        n_holdout,
        train_brier,
        holdout_brier,
    )

    active = await _get_active_model_meta()
    new_features = set(payload_dict.get("feature_names", []))

    if active is None:
        ratio = None
        reason = "no_active_model"
        promote = True
    else:
        active_features = _model_features(active.get("model_payload"))
        active_brier = float(active["holdout_brier"])
        # Если активная модель НЕ содержит признаки которых требует current schema
        # → allow degradation up to 10% (schema upgrade > marginal Brier).
        schema_mismatch = bool(new_features - active_features)
        ratio = PROMOTION_BRIER_RATIO_SCHEMA_MISMATCH if schema_mismatch else PROMOTION_BRIER_RATIO
        promote = holdout_brier < active_brier * ratio
        reason = (
            f"schema_upgrade (ratio={ratio:.2f}, brier {holdout_brier:.4f} vs active {active_brier:.4f})"
            if schema_mismatch
            else f"standard (ratio={ratio:.2f}, brier {holdout_brier:.4f} vs active {active_brier:.4f})"
        )

    # Validation gate — блокирует promote независимо от Brier-ratio
    if promote and holdout_examples is not None and holdout_predictions is not None:
        passes_gate, failures = _validation_gate_check(
            holdout_examples=holdout_examples,
            holdout_predictions=holdout_predictions,
            name=version,
        )
        if not passes_gate:
            promote = False
            reason = f"VALIDATION_GATE_FAILED: {'; '.join(failures)}"

    if promote:
        await execute("UPDATE ml_meta_labeler SET is_active = false WHERE is_active = true")
        await execute(
            "UPDATE ml_meta_labeler SET is_active = true, last_skip_reason = NULL WHERE version = $1",
            version,
        )
        logger.info(
            "ml_meta_labeler_promoted",
            version=version,
            holdout_brier=holdout_brier,
            ratio=ratio,
            reason=reason,
        )
    else:
        # Сохраняем причину НЕ-промоута для наблюдаемости (frozen-by-gate vs broken).
        await execute(
            "UPDATE ml_meta_labeler SET last_skip_reason = $2 WHERE version = $1",
            version,
            reason,
        )
        logger.warning(
            "ml_meta_labeler_not_promoted",
            version=version,
            holdout_brier=holdout_brier,
            ratio=ratio,
            reason=reason,
        )
    return promote, reason


def _reproduce_holdout_split(examples: list[MetaLabelerExample], holdout_fraction: float) -> list[MetaLabelerExample]:
    """Reproduce holdout split из train_meta_labeler для validation gate.

    Логика mirror'ит `_event_aware_split`: если ≥50% examples имеют event_id —
    chronological event-level split. Иначе position-level cut.
    """
    n = len(examples)
    if n == 0:
        return []
    has_event = sum(1 for ex in examples if ex.event_id) / n
    if has_event < 0.5:
        cut = int(n * (1.0 - holdout_fraction))
        return list(examples[cut:])

    # Event-aware chronological split
    seen_events: dict[str, int] = {}
    for i, ex in enumerate(examples):
        eid = ex.event_id or f"_idx_{i}"
        if eid not in seen_events:
            seen_events[eid] = i
    sorted_events = sorted(seen_events.keys(), key=lambda e: seen_events[e])
    cut_event = int(len(sorted_events) * (1.0 - holdout_fraction))
    train_events = set(sorted_events[:cut_event])
    return [ex for ex in examples if (ex.event_id or "") not in train_events]


async def run_ml_meta_labeler_fit() -> dict[str, Any]:
    rows = await fetch(_FETCH_TRAINING_SQL, ROLLING_DAYS)
    if not rows:
        logger.warning("ml_meta_labeler_no_data")
        return {"status": "skipped", "reason": "no_data", "n": 0}
    if len(rows) < MIN_TRAIN_SAMPLES:
        logger.warning("ml_meta_labeler_too_few_samples", n=len(rows), min=MIN_TRAIN_SAMPLES)
        return {"status": "skipped", "reason": "too_few_samples", "n": len(rows)}

    cals = await _load_l1_calibrators()
    examples = [_row_to_example(dict(r), cals) for r in rows]

    try:
        model = train_meta_labeler(
            examples,
            config=MetaLabelerConfig(min_train_samples=MIN_TRAIN_SAMPLES),
            holdout_fraction=HOLDOUT_FRACTION,
        )
    except Exception as exc:
        logger.error("ml_meta_labeler_train_failed", error=str(exc))
        return {"status": "error", "reason": str(exc)}

    # Reproduce holdout split + predict для validation gate
    holdout_examples = _reproduce_holdout_split(examples, HOLDOUT_FRACTION)
    holdout_predictions: list[float] = []
    if holdout_examples:
        try:
            holdout_predictions = [float(p) for p in model.predict(holdout_examples)]
        except Exception as exc:
            logger.warning("ml_meta_labeler_holdout_predict_failed", error=str(exc))

    version = datetime.now(UTC).strftime("lgb_l2_v%Y%m%d_%H%M%S")
    payload_dict = model.serialize()
    promoted, promote_reason = await _persist_and_maybe_promote(
        version=version,
        payload_json=json.dumps(payload_dict),
        payload_dict=payload_dict,
        n_train=model.n_train,
        n_holdout=model.n_holdout,
        train_brier=model.train_brier,
        holdout_brier=model.holdout_brier,
        holdout_examples=holdout_examples if holdout_predictions else None,
        holdout_predictions=holdout_predictions if holdout_predictions else None,
    )

    return {
        "status": "ok",
        "version": version,
        "n_train": model.n_train,
        "n_holdout": model.n_holdout,
        "train_brier": round(model.train_brier, 6),
        "holdout_brier": round(model.holdout_brier, 6),
        "promoted": promoted,
        "promote_reason": promote_reason,
        "feature_importance": model.feature_importance(),
    }
