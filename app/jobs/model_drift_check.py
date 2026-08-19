"""Model drift check (Phase E.3) — daily 06:00 UTC.

Не academic ADWIN/BOCPD — простой Brier degradation monitor:

1. Для каждого детектора в ml_calibrators:
   - Считаем rolling Brier за last 7 days (на свежих resolved сигналах)
   - Сравниваем с baseline (brier_holdout последнего fit)
   - Если rolling_brier > baseline * 1.30 → drift, alert в TG

2. Для L2 meta-labeler в ml_meta_labeler (active):
   - То же самое: rolling Brier за last 7d vs holdout_brier при fit

Drift triggers:
- ratio > 1.30 — degradation
- ratio > 1.50 — severe degradation, демоут модели (UPDATE is_active=false)

Cron: daily 06:00 UTC, после settle/strategy-health.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import structlog

from app.calibration.beta_calibrator import BetaCalibrator
from app.calibration.fallback_chain import load_l1_calibrators as _load_l1_calibrators
from app.calibration.meta_labeler import (
    KNOWN_CATEGORIES,  # noqa: F401  (ensure import succeeds for future use)
    MetaLabelerExample,
    deserialize_from_json,
)
from app.services.database import execute, fetch, fetchrow

logger = structlog.get_logger()

ROLLING_DAYS = 7
DEGRADATION_RATIO = 1.30
SEVERE_RATIO = 1.50


# Tier 3.1 фичи в drift-check ОБЯЗАТЕЛЬНЫ: модель обучена с ними, и если подать
# NaN там где данные есть, rolling Brier сместится → ложный drift-сигнал.
# Те же leak-free LATERAL что в fit/inference (strictly < ds.generated_at).
_RECENT_SIGNALS_SQL = """
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
   AND ds.generated_at > now() - make_interval(days => $1)
"""


def _opt_float(val: Any) -> float | None:
    """None-safe float конверсия для optional Tier 3.1 фич (БД → None/Decimal)."""
    return None if val is None else float(val)


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


async def _check_l1_drift(
    detector: str,
    cal: BetaCalibrator,
    baseline_brier: float,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    raw_p = np.asarray([float(r["raw_p"]) for r in rows], dtype=float)
    y = np.asarray([1 if r["outcome"] == "WIN" else 0 for r in rows], dtype=float)
    if len(raw_p) < 30:
        return None

    p_cal = cal.transform(raw_p)
    p_arr = np.asarray(p_cal if hasattr(p_cal, "__len__") else [p_cal], dtype=float)
    rolling = _brier(y, p_arr)
    ratio = rolling / baseline_brier if baseline_brier > 0 else 1.0

    severity = "ok"
    if ratio >= SEVERE_RATIO:
        severity = "severe"
    elif ratio >= DEGRADATION_RATIO:
        severity = "degraded"

    logger.info(
        "l1_drift_check",
        detector=detector,
        n=len(rows),
        rolling_brier=round(rolling, 5),
        baseline_brier=round(baseline_brier, 5),
        ratio=round(ratio, 3),
        severity=severity,
    )
    return {
        "level": "L1",
        "detector": detector,
        "n": len(rows),
        "rolling_brier": rolling,
        "baseline_brier": baseline_brier,
        "ratio": ratio,
        "severity": severity,
    }


async def _check_l2_drift(
    rows: list[dict[str, Any]],
    cals: dict[str, BetaCalibrator],
) -> dict[str, Any] | None:
    active = await fetchrow("SELECT version, model_payload, holdout_brier FROM ml_meta_labeler WHERE is_active = true")
    if active is None:
        return None
    if len(rows) < 30:
        return None

    payload = active["model_payload"]
    if isinstance(payload, str):
        payload_json = payload
    else:
        payload_json = json.dumps(payload)
    model = deserialize_from_json(payload_json)

    examples = []
    for r in rows:
        detector = r["detector"]
        raw_p = float(r["raw_p"])
        # Beta-only СОЗНАТЕЛЬНО (не полная G18→LGB→Beta цепочка из fallback_chain):
        # drift-check измеряет качество самого Beta-калибратора против outcomes.
        cal = cals.get(detector)
        calibrated_p = float(cal.transform(raw_p)) if cal is not None and cal.params is not None else raw_p
        examples.append(
            MetaLabelerExample(
                detector_id=detector,
                calibrated_p=calibrated_p,
                edge_pct=float(r["expected_edge_pct"]),
                entry_price=float(r["entry_price"]),
                horizon_sec=int(r["horizon_sec"]),
                category=r.get("category"),
                volume_24h=float(r["volume_24h"] or 0),
                is_v2_post=True,
                outcome=1 if r["outcome"] == "WIN" else 0,
                hours_to_resolution=_opt_float(r.get("hours_to_resolution")),
                recent_wr_30d=_opt_float(r.get("recent_wr_30d")),
                settle_velocity_hours=_opt_float(r.get("settle_velocity_hours")),
            )
        )

    preds = model.predict(examples)
    y = np.asarray([ex.outcome for ex in examples], dtype=float)
    rolling = _brier(y, preds)
    baseline = float(active["holdout_brier"])
    ratio = rolling / baseline if baseline > 0 else 1.0

    severity = "ok"
    if ratio >= SEVERE_RATIO:
        severity = "severe"
    elif ratio >= DEGRADATION_RATIO:
        severity = "degraded"

    logger.info(
        "l2_drift_check",
        version=active["version"],
        n=len(examples),
        rolling_brier=round(rolling, 5),
        baseline_brier=round(baseline, 5),
        ratio=round(ratio, 3),
        severity=severity,
    )
    return {
        "level": "L2",
        "version": active["version"],
        "n": len(examples),
        "rolling_brier": rolling,
        "baseline_brier": baseline,
        "ratio": ratio,
        "severity": severity,
    }


async def _check_layer_drift(table: str, label: str) -> dict[str, Any] | None:
    """Drift по Layer A (ml_event_selector) / Layer B (ml_market_picker).

    Опция А (дёшево): обе таблицы УЖЕ хранят live_holdout_brier (на post-cutover
    событиях) и holdout_per_event_brier (at-fit baseline) активной модели. Ratio
    live/baseline ловит ту же деградацию, что мы нашли вручную (Layer A 0.17→0.27 ≈
    ratio 1.5). НЕ re-score — модель уже посчитала live-Brier при последнем fit.
    NB: метрики из ПОСЛЕДНЕГО fit активной модели; обновляются раз в неделю.
    """
    row = await fetchrow(
        f"SELECT version, holdout_per_event_brier, live_holdout_brier, live_n_events, fit_at "  # noqa: S608
        f"FROM {table} WHERE is_active = true"
    )
    if row is None:
        logger.info("layer_drift_check_no_active", level=label, table=table)
        return {"level": label, "active": False, "severity": "no_active"}

    baseline = float(row["holdout_per_event_brier"])
    live = row["live_holdout_brier"]
    live_n = int(row["live_n_events"] or 0)
    if live is None or live_n < 30 or baseline <= 0:
        return {"level": label, "active": True, "severity": "insufficient", "live_n": live_n}

    live = float(live)
    ratio = live / baseline
    severity = "ok"
    if ratio >= SEVERE_RATIO:
        severity = "severe"
    elif ratio >= DEGRADATION_RATIO:
        severity = "degraded"

    logger.info(
        "layer_drift_check",
        level=label,
        version=row["version"],
        live_n=live_n,
        live_brier=round(live, 5),
        baseline_brier=round(baseline, 5),
        ratio=round(ratio, 3),
        severity=severity,
    )
    return {
        "level": label,
        "active": True,
        "version": row["version"],
        "live_n": live_n,
        "live_brier": live,
        "baseline_brier": baseline,
        "ratio": ratio,
        "severity": severity,
    }


async def _demote_l2_if_severe(severity: str, version: str | None) -> None:
    if severity != "severe" or not version:
        return
    await execute(
        "UPDATE ml_meta_labeler SET is_active = false WHERE version = $1",
        version,
    )
    logger.warning("l2_demoted_due_to_severe_drift", version=version)


async def run_model_drift_check() -> dict[str, Any]:
    rows = await fetch(_RECENT_SIGNALS_SQL, ROLLING_DAYS)
    if not rows:
        logger.info("drift_check_no_recent_signals")
        return {"status": "skipped", "reason": "no_recent_signals"}

    cals = await _load_l1_calibrators()

    by_detector: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_detector.setdefault(r["detector"], []).append(dict(r))

    l1_results: list[dict[str, Any]] = []
    cal_meta = await fetch("SELECT detector, brier_holdout FROM ml_calibrators")
    baseline_by_det = {r["detector"]: float(r["brier_holdout"]) for r in cal_meta}

    for detector, det_rows in by_detector.items():
        cal = cals.get(detector)
        baseline = baseline_by_det.get(detector)
        if cal is None or baseline is None:
            continue
        result = await _check_l1_drift(detector, cal, baseline, det_rows)
        if result is not None:
            l1_results.append(result)

    l2_result = await _check_l2_drift([dict(r) for r in rows], cals)
    if l2_result is not None:
        await _demote_l2_if_severe(l2_result["severity"], l2_result["version"])

    # Layer A/B — мониторинг без auto-demote: активная модель тут может быть
    # единственной рабочей (frozen-by-gate), снимать её опаснее чем держать.
    layer_a = await _check_layer_drift("ml_event_selector", "LayerA")
    layer_b = await _check_layer_drift("ml_market_picker", "LayerB")

    return {
        "status": "ok",
        "l1": l1_results,
        "l2": l2_result,
        "layer_a": layer_a,
        "layer_b": layer_b,
    }
