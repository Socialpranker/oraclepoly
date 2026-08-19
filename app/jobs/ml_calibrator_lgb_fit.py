"""LGB calibrator fit — per-detector LightGBM, параллельный к ml_calibrator_fit.py.

Чем отличается от Beta-fit:
  1. Использует full feature set (build_features из app.calibration.lgb_features),
     а не только raw_p. Это позволяет различать signals для детекторов
     с constant raw confidence (D31, D41, D33).
  2. Train job фиттит ВСЕ детекторы где N >= MIN_SAMPLES, но HONEST promote-gate:
     активирует LGB-калибратор (is_active=true) ТОЛЬКО если он строго лучше
     Beta-калибратора на одном и том же holdout.
  3. Один active row per detector (UNIQUE partial index в БД).

Промоут-логика:
    A) brier_holdout(LGB) < brier_holdout(Beta) на этом же holdout — improvement
    B) AND |LGB - Beta| / Beta >= MIN_IMPROVEMENT_PCT (5%)
    C) AND brier_holdout(LGB) < brier_holdout(raw_p) — иначе вообще нет смысла
    D) AND n_holdout >= MIN_HOLDOUT (20)
    Если хоть одно не выполнено — записывается как candidate (is_active=false).

Crontab: еженедельно (Sunday 04:00 UTC), после ml_calibrator_fit.py.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import structlog

from app.calibration.beta_calibrator import BetaCalibrator, raw_brier
from app.calibration.lgb_calibrator import LgbCalibrator
from app.calibration.lgb_features import build_features
from app.services.database import execute, fetch

logger = structlog.get_logger()

# Тот же rolling window что и L1 Beta — для honest сравнения на одном train set.
ROLLING_DAYS = 540
HOLDOUT_FRACTION = 0.20
MIN_SAMPLES = 200  # LGB нужно больше чем Beta (200 vs 30)
MIN_HOLDOUT = 20
MIN_IMPROVEMENT_PCT = 5.0  # LGB должен быть на 5%+ лучше Beta чтобы promote
# AUC guard — модель должна иметь меру discrimination ≥0.55.
# Без него: 2026-05-19 D14_crypto LGB AUC=0.47 (хуже случайного) был promoted потому что
# Brier был чуть лучше raw_p (0.204 vs 0.213). Brier измеряет калибровку, не дискриминацию;
# модель с calibrated_p ≈ base_rate имеет ОК Brier но не помогает ранжировать сигналы.
MIN_HOLDOUT_AUC = 0.55
# Ceiling-guards против too-perfect holdout (memorization signature). Реальная
# калибровка не даёт perfect ranking; AUC=1.0/brier≈0 = leak (group leakage по рынку,
# инцидент @72h 2026-06-02). Group-split их в основном устраняет, но ceiling — страховка
# чтобы протёкший калибратор НЕ активировался автоматически.
MAX_HOLDOUT_AUC = 0.995
MIN_HOLDOUT_BRIER = 0.02
MIN_HOLDOUT_MARKETS = 10  # нельзя верить holdout из горстки рынков

# Какие детекторы фиттить. None = все которые есть в detector_signals.
# Для pilot можно ограничить — но fit job дешёвый, фиттим всё.
ALLOWED_DETECTORS: list[str] | None = None

# Детекторы с per-anchor калибраторами (ключ detector@{anchor}h). STANDALONE 24h/72h
# имеют разные распределения → единый калибратор переуверен на 24h. Зеркалит
# _PER_ANCHOR_DETECTORS в ml_calibrator_fit.py (Beta). Прочие — single-anchor (плоский ключ).
_PER_ANCHOR_DETECTORS = frozenset({"STANDALONE_V2_LGB"})


def _calibrator_key(detector: str, anchor_hours: str | None) -> str:
    """Ключ: detector@{anchor}h для per-anchor детекторов, иначе плоский detector."""
    if detector in _PER_ANCHOR_DETECTORS and anchor_hours:
        return f"{detector}@{anchor_hours}h"
    return detector


_FETCH_TRAINING_SQL = """
SELECT
    ds.detector,
    ds.market_id,
    ds.confidence,
    ds.expected_edge_pct,
    ds.fair_price,
    ds.current_price,
    ds.horizon_sec,
    ds.side,
    ds.event_size,
    ds.outcome,
    ds.evidence,
    ds.generated_at,
    m.category,
    m.volume_24h,
    -- Tier 3.1: hours_to_resolution на момент signal
    -- Fallback в build_features на evidence.hours_to_resolve если NULL.
    CASE
        WHEN m.end_date IS NOT NULL
        THEN EXTRACT(epoch FROM (m.end_date - ds.generated_at)) / 3600.0
        ELSE NULL
    END AS hours_to_resolution_at_signal,
    -- Tier 3.1: recent_wr_30d — rolling WR детектора strictly < generated_at (leak-free)
    -- NULL если в 30d window нет resolved signals для этого детектора
    rw.recent_wr_30d,
    rw.detector_recent_count_30d
FROM detector_signals ds
LEFT JOIN markets m ON m.polymarket_id = ds.market_id
LEFT JOIN LATERAL (
    SELECT
        AVG(CASE WHEN ds2.outcome = 'WIN' THEN 1.0 ELSE 0.0 END) AS recent_wr_30d,
        COUNT(*) AS detector_recent_count_30d
      FROM detector_signals ds2
     WHERE ds2.detector = ds.detector
       AND ds2.resolved = true
       AND ds2.outcome IN ('WIN', 'LOSS')
       AND ds2.generated_at < ds.generated_at
       AND ds2.generated_at >= ds.generated_at - interval '30 days'
) rw ON true
WHERE ds.resolved = true
  AND ds.outcome IN ('WIN', 'LOSS')
  AND ds.generated_at > now() - make_interval(days => $1)
ORDER BY ds.detector, ds.generated_at ASC
"""


_LOAD_BETA_SQL = """
SELECT detector, params
  FROM ml_calibrators
"""


_INSERT_SQL = """
INSERT INTO ml_calibrators_lgb (
    version, detector, model_payload,
    n_train, n_holdout,
    train_brier, holdout_brier, holdout_auc, holdout_ece,
    brier_raw_p, brier_beta,
    is_active
)
VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10, $11, $12)
"""

# Деактивирует все активные LGB-калибраторы для детектора (старые promote'ы).
_DEACTIVATE_OLD_SQL = """
UPDATE ml_calibrators_lgb
   SET is_active = false
 WHERE detector = $1 AND is_active = true
"""


def _version_for(detector: str) -> str:
    """Compact version: detector + UTC timestamp + sha-short(seed)."""
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    short = hashlib.sha1(f"{detector}_{ts}".encode()).hexdigest()[:8]
    return f"{detector}__{ts}__{short}"


def _ece(p: list[float], y: list[int], n_bins: int = 10) -> float:
    """Expected Calibration Error."""
    import numpy as np

    p_arr = np.asarray(p, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    bins = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    n = len(p_arr)
    if n == 0:
        return 0.0
    for i in range(n_bins):
        if i == n_bins - 1:
            mask = (p_arr >= bins[i]) & (p_arr <= bins[i + 1])
        else:
            mask = (p_arr >= bins[i]) & (p_arr < bins[i + 1])
        if mask.sum() < 1:
            continue
        bin_p = p_arr[mask].mean()
        bin_y = y_arr[mask].mean()
        ece += (mask.sum() / n) * abs(bin_p - bin_y)
    return float(ece)


def _auc(p: list[float], y: list[int]) -> float | None:
    try:
        from sklearn.metrics import roc_auc_score

        if len(set(y)) < 2:
            return None
        return float(roc_auc_score(y, p))
    except Exception:
        return None


def _top_features_by_gain(cal: LgbCalibrator, top_n: int = 5) -> list[tuple[str, float]]:
    """Возвращает топ-N features по importance gain для диагностики.

    Если booster degenerate или ещё не loaded — пустой list.
    """
    if cal.params is None or cal.params.degenerate:
        return []
    booster = cal._ensure_booster()
    if booster is None:
        return []
    try:
        importances = booster.feature_importance(importance_type="gain")
        pairs = sorted(
            zip(cal.params.feature_names, importances, strict=True),
            key=lambda x: -float(x[1]),
        )
        return [(name, float(gain)) for name, gain in pairs[:top_n]]
    except Exception:
        return []


def _group_split_indices(market_ids: list[str]) -> tuple[list[int], list[int]]:
    """Group-aware chronological split по market_id.

    ВСЕ строки одного рынка идут целиком в train ИЛИ в holdout — никогда не
    разрезаются. Иначе LGB запоминает рынок через entry_price/volume/current_price
    (почти-уникальный отпечаток) → leak (holdout AUC=1.0). См. план group-leakage.

    rows приходят ORDER BY generated_at → собираем уникальные рынки в порядке
    первого появления (хронология), последние ~HOLDOUT_FRACTION уникальных рынков
    → holdout. Возвращает (train_idx, holdout_idx) для синхронного среза feats/outcomes/raw.
    """
    # Уникальные рынки в порядке первого появления.
    seen: dict[str, None] = {}
    for m in market_ids:
        if m not in seen:
            seen[m] = None
    unique_markets = list(seen.keys())
    n_unique = len(unique_markets)
    # Последние HOLDOUT_FRACTION рынков → holdout (минимум 1 рынок если есть ≥2).
    n_holdout_markets = max(1, int(n_unique * HOLDOUT_FRACTION)) if n_unique >= 2 else 0
    holdout_market_set = set(unique_markets[n_unique - n_holdout_markets :]) if n_holdout_markets else set()

    train_idx, holdout_idx = [], []
    for i, m in enumerate(market_ids):
        (holdout_idx if m in holdout_market_set else train_idx).append(i)
    return train_idx, holdout_idx


def _fit_one_detector(
    detector: str,
    rows: list[dict[str, Any]],
    beta_cal: BetaCalibrator | None,
) -> dict[str, Any] | None:
    """Фиттит LGB на этом наборе rows + сравнивает с Beta на одном holdout.

    Возвращает payload для INSERT или None если N < MIN_SAMPLES.
    """
    if len(rows) < MIN_SAMPLES:
        logger.info("lgb_fit_skip_too_few", detector=detector, n=len(rows), min=MIN_SAMPLES)
        return None

    feats = [build_features(r) for r in rows]
    outcomes = [1 if r["outcome"] == "WIN" else 0 for r in rows]
    raw_p_arr = [float(r["confidence"]) for r in rows]
    market_ids = [str(r.get("market_id") or "") for r in rows]

    # Group-aware split по market_id — все строки рынка целиком в train ИЛИ holdout
    # (иначе LGB запоминает рынок → leak). Синхронный срез всех трёх массивов по индексам.
    train_idx, holdout_idx = _group_split_indices(market_ids)
    feats_tr = [feats[i] for i in train_idx]
    y_tr = [outcomes[i] for i in train_idx]
    feats_ho = [feats[i] for i in holdout_idx]
    y_ho = [outcomes[i] for i in holdout_idx]
    raw_ho = [raw_p_arr[i] for i in holdout_idx]
    n_holdout_markets = len({market_ids[i] for i in holdout_idx})

    if len(y_ho) < MIN_HOLDOUT:
        logger.info("lgb_fit_skip_thin_holdout", detector=detector, n_holdout=len(y_ho), min=MIN_HOLDOUT)
        return None

    try:
        cal = LgbCalibrator(min_samples=MIN_SAMPLES // 2).fit(
            feats_tr, y_tr, valid_features=feats_ho, valid_outcome=y_ho
        )
    except Exception as exc:
        logger.error("lgb_fit_failed", detector=detector, error=str(exc))
        return None

    # LGB metrics
    p_tr = cal.transform(feats_tr)
    p_ho = cal.transform(feats_ho)
    if hasattr(p_tr, "tolist"):
        p_tr_list = p_tr.tolist()
    else:
        p_tr_list = [float(p_tr)]
    if hasattr(p_ho, "tolist"):
        p_ho_list = p_ho.tolist()
    else:
        p_ho_list = [float(p_ho)]

    train_brier = sum((a - b) ** 2 for a, b in zip(p_tr_list, y_tr, strict=True)) / len(y_tr)
    holdout_brier = sum((a - b) ** 2 for a, b in zip(p_ho_list, y_ho, strict=True)) / len(y_ho)
    holdout_ece = _ece(p_ho_list, y_ho)
    holdout_auc = _auc(p_ho_list, y_ho)

    # Baselines на ТОМ ЖЕ holdout (для честного promote-gate)
    brier_raw_p = raw_brier(raw_ho, y_ho)
    brier_beta: float | None = None
    if beta_cal is not None and beta_cal.params is not None:
        try:
            beta_p_ho = beta_cal.transform(raw_ho)
            if hasattr(beta_p_ho, "tolist"):
                beta_list = beta_p_ho.tolist()
            else:
                beta_list = [float(beta_p_ho)]
            brier_beta = sum((a - b) ** 2 for a, b in zip(beta_list, y_ho, strict=True)) / len(y_ho)
        except Exception as exc:
            logger.warning("lgb_fit_beta_compare_failed", detector=detector, error=str(exc))

    # Promote-gate: LGB должен быть лучше И raw_p И beta + AUC≥0.55
    should_promote = _should_promote(
        detector=detector,
        holdout_brier=holdout_brier,
        brier_raw_p=brier_raw_p,
        brier_beta=brier_beta,
        n_holdout=len(y_ho),
        holdout_auc=holdout_auc,
        n_holdout_markets=n_holdout_markets,
    )

    # Diagnostic: топ-5 фичей по importance (для аудита Tier 3.1 эффекта)
    top_feats = _top_features_by_gain(cal, top_n=5)
    if top_feats:
        logger.info(
            "lgb_top_features",
            detector=detector,
            top_features=[f"{n}={int(g)}" for n, g in top_feats],
        )

    return {
        "version": _version_for(detector),
        "detector": detector,
        "model_payload": json.dumps(cal.serialize()),
        "n_train": len(feats_tr),
        "n_holdout": len(feats_ho),
        "train_brier": round(train_brier, 6),
        "holdout_brier": round(holdout_brier, 6),
        "holdout_auc": round(holdout_auc, 4) if holdout_auc is not None else None,
        "holdout_ece": round(holdout_ece, 6),
        "brier_raw_p": round(brier_raw_p, 6),
        "brier_beta": round(brier_beta, 6) if brier_beta is not None else None,
        "is_active": should_promote,
    }


def _should_promote(
    *,
    detector: str,
    holdout_brier: float,
    brier_raw_p: float,
    brier_beta: float | None,
    n_holdout: int,
    holdout_auc: float | None = None,
    n_holdout_markets: int = 0,
) -> bool:
    """Honest promote-gate.

    True ТОЛЬКО если все условия выполнены:
      - holdout_brier < brier_raw_p (LGB лучше чем no calibration)
      - holdout_brier < brier_beta (если Beta есть) — LGB лучше Beta
      - improvement >= MIN_IMPROVEMENT_PCT
      - n_holdout >= MIN_HOLDOUT
      - holdout_auc >= MIN_HOLDOUT_AUC (модель умеет ранжировать)
      - holdout НЕ too-perfect (AUC<=MAX, brier>=MIN — иначе memorization/leak)
      - n_holdout_markets >= MIN_HOLDOUT_MARKETS (holdout не из горстки рынков)
    """
    if n_holdout < MIN_HOLDOUT:
        return False
    # Ceiling-guards: too-perfect holdout = memorization/leak, не активируем.
    if holdout_auc is not None and holdout_auc > MAX_HOLDOUT_AUC:
        logger.warning(
            "lgb_no_promote_too_perfect_auc",
            detector=detector,
            auc=round(holdout_auc, 4),
            max=MAX_HOLDOUT_AUC,
        )
        return False
    if holdout_brier < MIN_HOLDOUT_BRIER:
        logger.warning(
            "lgb_no_promote_too_perfect_brier",
            detector=detector,
            brier=round(holdout_brier, 6),
            min=MIN_HOLDOUT_BRIER,
        )
        return False
    if 0 < n_holdout_markets < MIN_HOLDOUT_MARKETS:
        logger.info(
            "lgb_no_promote_few_holdout_markets",
            detector=detector,
            markets=n_holdout_markets,
            min=MIN_HOLDOUT_MARKETS,
        )
        return False
    # AUC guard: модель должна различать классы. Brier может быть лучше raw_p
    # даже у "predict-constant" модели — AUC ловит этот failure mode.
    # holdout_auc=None означает 1-class holdout — не доверяем (returns False через caller).
    if holdout_auc is not None and holdout_auc < MIN_HOLDOUT_AUC:
        logger.info(
            "lgb_no_promote_low_auc",
            detector=detector,
            auc=round(holdout_auc, 4),
            min=MIN_HOLDOUT_AUC,
        )
        return False
    if holdout_brier >= brier_raw_p:
        logger.info(
            "lgb_no_promote_worse_than_raw",
            detector=detector,
            lgb=holdout_brier,
            raw=brier_raw_p,
        )
        return False
    if brier_beta is not None:
        if holdout_brier >= brier_beta:
            logger.info(
                "lgb_no_promote_worse_than_beta",
                detector=detector,
                lgb=holdout_brier,
                beta=brier_beta,
            )
            return False
        improvement_pct = 100.0 * (brier_beta - holdout_brier) / brier_beta
        if improvement_pct < MIN_IMPROVEMENT_PCT:
            logger.info(
                "lgb_no_promote_marginal",
                detector=detector,
                improvement_pct=round(improvement_pct, 2),
                min=MIN_IMPROVEMENT_PCT,
            )
            return False
    # Если Beta нет — сравниваем с raw_p. Уже проверили выше.
    return True


async def _load_beta_cals() -> dict[str, BetaCalibrator]:
    rows = await fetch(_LOAD_BETA_SQL)
    out: dict[str, BetaCalibrator] = {}
    for r in rows:
        params = r["params"]
        if isinstance(params, str):
            params = json.loads(params)
        try:
            out[r["detector"]] = BetaCalibrator.deserialize(params)
        except Exception as exc:
            logger.warning("beta_load_failed", detector=r["detector"], error=str(exc))
    return out


async def run_ml_calibrator_lgb_fit() -> dict[str, Any]:
    rows = await fetch(_FETCH_TRAINING_SQL, ROLLING_DAYS)
    if not rows:
        logger.warning("lgb_fit_no_data")
        return {"detectors": [], "fitted": 0, "promoted": 0, "skipped": 0}

    beta_cals = await _load_beta_cals()

    # Группируем по composite-ключу: для per-anchor детекторов (STANDALONE) — detector@{anchor}h,
    # извлекая anchor из evidence.anchor_hours (jsonb → str|dict). Прочие — плоский detector.
    by_detector: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        det = r["detector"]
        if ALLOWED_DETECTORS is not None and det not in ALLOWED_DETECTORS:
            continue
        anchor_h: str | None = None
        if det in _PER_ANCHOR_DETECTORS:
            ev = r.get("evidence")  # толерантно к строкам без evidence
            if isinstance(ev, str):
                ev = json.loads(ev) if ev else {}
            anchor_val = (ev or {}).get("anchor_hours")
            anchor_h = str(anchor_val) if anchor_val is not None else None
        key = _calibrator_key(det, anchor_h)
        by_detector.setdefault(key, []).append(dict(r))

    fitted = 0
    promoted = 0
    skipped = 0
    summary: list[dict[str, Any]] = []

    for det, det_rows in sorted(by_detector.items()):
        # det здесь = composite-ключ (e.g. STANDALONE_V2_LGB@24h). beta_cals тоже keyed так же.
        result = _fit_one_detector(det, det_rows, beta_cals.get(det))
        if result is None:
            skipped += 1
            summary.append({"detector": det, "fitted": False, "n": len(det_rows)})
            continue

        # ВАЖНО (2026-05-19): demote политика semi-sticky.
        # Деактивируем старый active ВСЕГДА (когда fit прошёл) — независимо от того,
        # promote'нут ли новый. Это защищает от случая когда новый fit показал, что
        # старая модель больше не лучше Beta/raw, но новая тоже не promotable —
        # в БД остался is_active=true на stale модели, которая по нашим же метрикам
        # уже не оправдана. Лучше fallback на Beta/raw чем держать deteriorated LGB.
        await execute(_DEACTIVATE_OLD_SQL, det)

        await execute(
            _INSERT_SQL,
            result["version"],
            result["detector"],
            result["model_payload"],
            result["n_train"],
            result["n_holdout"],
            result["train_brier"],
            result["holdout_brier"],
            result["holdout_auc"],
            result["holdout_ece"],
            result["brier_raw_p"],
            result["brier_beta"],
            result["is_active"],
        )

        fitted += 1
        if result["is_active"]:
            promoted += 1
        summary.append(
            {
                "detector": det,
                "fitted": True,
                "promoted": result["is_active"],
                "n_train": result["n_train"],
                "n_holdout": result["n_holdout"],
                "holdout_brier_lgb": result["holdout_brier"],
                "holdout_brier_beta": result["brier_beta"],
                "holdout_brier_raw": result["brier_raw_p"],
                "holdout_auc": result["holdout_auc"],
            }
        )
        logger.info(
            "lgb_fitted",
            detector=det,
            promoted=result["is_active"],
            holdout_brier_lgb=result["holdout_brier"],
            holdout_brier_beta=result["brier_beta"],
            holdout_brier_raw=result["brier_raw_p"],
            n_train=result["n_train"],
        )

    return {
        "detectors": summary,
        "fitted": fitted,
        "promoted": promoted,
        "skipped": skipped,
    }
