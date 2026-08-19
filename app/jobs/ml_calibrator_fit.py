"""ML calibrator fit — еженедельно (Sunday 03:00 UTC) фиттит L1 Beta cal per-detector.

Тянет resolved detector_signals за rolling 90d, разделяет на train/holdout
(80/20), фиттит Beta calibrator. Если N < min_samples — записывается degenerate
fallback (constant q = mean outcome).

Параметры пишутся в `ml_calibrators` (PRIMARY KEY = detector → upsert).

Inference path читает row для нужного детектора и применяет
BetaCalibrator.deserialize(params).transform(raw_p).
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.calibration.beta_calibrator import BetaCalibrator, raw_brier
from app.services.database import execute, fetch

logger = structlog.get_logger()

# 2026-05-19: повышено с 90 до 540 после аудита (L1 использовал 20-40% доступных
# данных — 33k labeled signals в БД, но fit учитывал только последние 90 дней).
# Расширенное окно даёт 3-5× больше samples per detector:
#   D31_underdog: 1,104 → ~5,200 labeled
#   D5_long_horizon: 1,416 → ~7,085 labeled
#   D14_crypto: 80 → ~1,110 labeled
# Chronological holdout (последние 20%) защитит от distribution shift —
# если калибровка деградирует на свежих данных, brier_holdout заметно вырастет.
ROLLING_DAYS = 540
HOLDOUT_FRACTION = 0.20
MIN_SAMPLES = 30
RANDOM_SEED = 42

_FETCH_TRAINING_SQL = """
SELECT detector,
       confidence    AS raw_p,
       outcome,
       evidence->>'anchor_hours' AS anchor_hours
  FROM detector_signals
 WHERE resolved = true
   AND outcome IN ('WIN', 'LOSS')
   AND generated_at > now() - make_interval(days => $1)
 ORDER BY generated_at ASC
"""

# Детекторы, у которых калибратор фитится ПЕР-ANCHOR (ключ detector@{anchor}h).
# STANDALONE имеет 24h/72h модели с разными распределениями predicted_p_yes —
# единый калибратор переуверен на 24h (slope 1.18). Прочие детекторы single-anchor.
_PER_ANCHOR_DETECTORS = frozenset({"STANDALONE_V2_LGB"})


def _calibrator_key(detector: str, anchor_hours: str | None) -> str:
    """Ключ калибратора: detector@{anchor}h для per-anchor детекторов, иначе detector.

    Backward-compatible: при отсутствии anchor_hours или для не-per-anchor детекторов
    возвращает плоский detector (как было). _calibrate() читает по тому же правилу.
    """
    if detector in _PER_ANCHOR_DETECTORS and anchor_hours:
        return f"{detector}@{anchor_hours}h"
    return detector


_UPSERT_SQL = """
INSERT INTO ml_calibrators (
    detector, params, n_train, n_holdout,
    brier_raw, brier_train, brier_holdout, fit_at
)
VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7, now())
ON CONFLICT (detector) DO UPDATE SET
    params         = EXCLUDED.params,
    n_train        = EXCLUDED.n_train,
    n_holdout      = EXCLUDED.n_holdout,
    brier_raw      = EXCLUDED.brier_raw,
    brier_train    = EXCLUDED.brier_train,
    brier_holdout  = EXCLUDED.brier_holdout,
    fit_at         = EXCLUDED.fit_at
"""


def _split_train_holdout(
    raw_p: list[float], outcome: list[int], seed: int = RANDOM_SEED
) -> tuple[list[float], list[int], list[float], list[int]]:
    # Chronological split: последние HOLDOUT_FRACTION rows = holdout.
    # seed оставлен в сигнатуре для обратной совместимости.
    _ = seed
    cut = int(len(raw_p) * (1.0 - HOLDOUT_FRACTION))
    return raw_p[:cut], outcome[:cut], raw_p[cut:], outcome[cut:]


def _fit_one_detector(
    detector: str,
    raw_p: list[float],
    outcome: list[int],
) -> dict[str, Any] | None:
    """Возвращает row для UPSERT или None при ошибке."""
    if len(raw_p) < MIN_SAMPLES:
        logger.warning(
            "ml_calibrator_fit_too_few_samples",
            detector=detector,
            n=len(raw_p),
            min=MIN_SAMPLES,
        )
        return None

    train_p, train_y, hold_p, hold_y = _split_train_holdout(raw_p, outcome)

    try:
        cal = BetaCalibrator(min_samples=MIN_SAMPLES // 2).fit(train_p, train_y)
    except Exception as exc:
        logger.error("ml_calibrator_fit_failed", detector=detector, error=str(exc))
        return None

    brier_raw_val = raw_brier(raw_p, outcome)
    brier_train = cal.brier(train_p, train_y)
    brier_holdout = cal.brier(hold_p, hold_y) if hold_y else brier_train

    return {
        "detector": detector,
        "params": json.dumps(cal.serialize()),
        "n_train": len(train_p),
        "n_holdout": len(hold_p),
        "brier_raw": round(brier_raw_val, 6),
        "brier_train": round(brier_train, 6),
        "brier_holdout": round(brier_holdout, 6),
    }


async def run_ml_calibrator_fit() -> dict[str, Any]:
    rows = await fetch(_FETCH_TRAINING_SQL, ROLLING_DAYS)
    if not rows:
        logger.warning("ml_calibrator_fit_no_data")
        return {"detectors": [], "fitted": 0, "skipped": 0}

    # Ключ = detector@{anchor}h для per-anchor детекторов (STANDALONE), иначе плоский detector.
    by_detector: dict[str, tuple[list[float], list[int]]] = {}
    for r in rows:
        # .get — толерантно к строкам без anchor_hours (asyncpg Record поддерживает .get).
        # Для не-per-anchor детекторов anchor_hours игнорируется → плоский ключ.
        key = _calibrator_key(r["detector"], r.get("anchor_hours"))
        outcome_int = 1 if r["outcome"] == "WIN" else 0
        ps, ys = by_detector.setdefault(key, ([], []))
        ps.append(float(r["raw_p"]))
        ys.append(outcome_int)

    fitted = 0
    skipped = 0
    summary: list[dict[str, Any]] = []
    for detector, (ps, ys) in by_detector.items():
        result = _fit_one_detector(detector, ps, ys)
        if result is None:
            skipped += 1
            summary.append({"detector": detector, "fitted": False, "n": len(ps)})
            continue
        await execute(
            _UPSERT_SQL,
            result["detector"],
            result["params"],
            result["n_train"],
            result["n_holdout"],
            result["brier_raw"],
            result["brier_train"],
            result["brier_holdout"],
        )
        fitted += 1
        summary.append(
            {
                "detector": detector,
                "fitted": True,
                "n_train": result["n_train"],
                "brier_raw": result["brier_raw"],
                "brier_holdout": result["brier_holdout"],
            }
        )
        logger.info(
            "ml_calibrator_fitted",
            detector=detector,
            n_train=result["n_train"],
            brier_raw=result["brier_raw"],
            brier_holdout=result["brier_holdout"],
        )

    return {"detectors": summary, "fitted": fitted, "skipped": skipped}
