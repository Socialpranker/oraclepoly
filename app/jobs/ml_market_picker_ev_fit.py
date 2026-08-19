"""ml_market_picker_ev_fit — параллельный trainer L2 на EV-target (regression).

Запускается по тому же расписанию что и `ml_market_picker_fit` (weekly Sun 04:35 UTC),
но обучает регрессор на `realized_roi_net / 100` вместо binary outcome.

NOTE: Validation gate (validate_model) НЕ применим — он для binary classification.
Для regression нужен отдельный harness: R², MAE, calibration на ROI декили.
TODO(future): создать `validate_regression_model()` в app/calibration/validation.py.

Зачем:
- Текущий L2 учится на outcome ∈ {0,1} → не различает "WIN с +1% net" и "WIN с +1900% net".
  Открывает NO @ 0.99 (gross +1%, net негативно после fee/slippage) с тем же confidence
  что и D5 NO @ 0.05 (net +1900%).
- EV-regressor предсказывает expected net_roi напрямую → сигналы с малым потенциалом
  (extreme prices) автоматически получают низкий predicted_ev → отфильтровываются sizer'ом.

Shadow A/B:
- Старая binary модель остаётся боевой (открывает позиции).
- Новая EV-regressor пишет predictions в `pipeline_v2_shadow_predictions.v2_ev_pred` неделю.
- Через 7 дней сравним по реализованному net PnL — если EV лучше, промотируем.

Источник target: `detector_signals.realized_roi_net` (миграция, populated by
detector_signals_resolve.py via _realized_roi_net_pct).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog

from app.calibration.beta_calibrator import BetaCalibrator
from app.calibration.event_selector import EventSelectorExample
from app.calibration.event_selector import deserialize_from_json as deserialize_layer_a
from app.calibration.market_picker import (
    MarketPickerConfig,
    MarketPickerExample,
    train_market_picker,
)
from app.services.database import execute, fetch, fetchrow

logger = structlog.get_logger()

ROLLING_DAYS = 365
MIN_TRAIN_ROWS = 100
LAYER_A_THRESHOLD = 0.50
V2_CUTOVER = datetime(2026, 4, 28, tzinfo=UTC)
PROMOTION_MAE_RATIO = 0.95  # new MAE < active MAE × 0.95
MODE = "ev_regression"

_FETCH_LAYER_A_SQL = "SELECT model_payload FROM ml_event_selector WHERE is_active = true"

# Аналогично binary fetch, но дополнительно: realized_roi_net (target).
# Отбираем только rows где target доступен (post-migration data).
_FETCH_TRAINING_SQL = """
WITH event_signals AS (
    SELECT ds.event_id              AS event_polymarket_id,
           COUNT(*)                  AS n_detector_signals,
           MIN(ds.generated_at)      AS first_signal_at
      FROM detector_signals ds
     WHERE ds.event_id IS NOT NULL
       AND ds.resolved = true
       AND ds.outcome IN ('WIN', 'LOSS')
       AND ds.realized_roi_net IS NOT NULL
       AND ds.generated_at > now() - make_interval(days => $1)
     GROUP BY ds.event_id
),
signal_event_agg AS (
    -- Aggregates из detector_signals.evidence (доступно ВСЕМ events incl. backfilled).
    -- Symmetric с Layer A/B fix 2026-05-14.
    SELECT ds.event_id                                       AS event_polymarket_id,
           MODE() WITHIN GROUP (ORDER BY ds.evidence->>'category')  AS evidence_category,
           COUNT(DISTINCT ds.market_id)                      AS sig_n_markets,
           AVG(ds.current_price)                             AS sig_mean_market_price,
           STDDEV_POP(ds.current_price)                      AS sig_market_price_dispersion,
           MAX((ds.evidence->>'horizon_days')::numeric)      AS sig_horizon_days
      FROM detector_signals ds
     WHERE ds.event_id IS NOT NULL
       AND ds.resolved = true
       AND ds.outcome IN ('WIN', 'LOSS')
     GROUP BY ds.event_id
),
event_meta AS (
    -- Live events: full attributes (NULL для backfilled, fallback из signal_event_agg).
    SELECT e.polymarket_id                              AS event_polymarket_id,
           e.category                                   AS event_category,
           e.archetype,
           COALESCE(e.volume_24h, 0)                    AS event_volume_24h,
           COALESCE(e.liquidity, 0)                     AS event_liquidity,
           COALESCE(e.neg_risk, false)                  AS neg_risk,
           COALESCE(e.neg_risk_other, false)            AS neg_risk_other,
           COALESCE(e.n_yes_winners, 1)                 AS n_yes_winners,
           e.end_date,
           COUNT(m.id)                                  AS n_markets,
           AVG(m.current_price)                         AS mean_market_price,
           STDDEV_POP(m.current_price)                  AS market_price_dispersion
      FROM events e
      JOIN markets m ON m.event_id = e.id
     GROUP BY e.id, e.polymarket_id, e.category, e.archetype, e.volume_24h,
              e.liquidity, e.neg_risk, e.neg_risk_other, e.n_yes_winners, e.end_date
)
SELECT ds.id                                                          AS signal_id,
       ds.detector,
       ds.market_id,
       ds.event_id                                                     AS event_polymarket_id,
       ds.side,
       ds.confidence                                                   AS raw_p,
       ds.expected_edge_pct,
       -- entry_price = side-aware стоимость 1 share (см. detector_signal_service.py).
       CASE WHEN ds.side = 'YES' THEN ds.current_price
            ELSE 1.0 - ds.current_price
       END                                                             AS entry_price,
       ds.horizon_sec,
       ds.generated_at,
       ds.outcome,
       ds.realized_roi_net,
       COALESCE(ds.event_size,
                em.n_markets,
                sea.sig_n_markets,
                1)                                                     AS event_size,
       COALESCE(em.archetype, 'unclassified')                          AS archetype,
       COALESCE(em.event_category, sea.evidence_category)              AS event_category,
       COALESCE(em.event_volume_24h, 0)                                AS event_volume_24h,
       COALESCE(em.event_liquidity, 0)                                 AS event_liquidity,
       COALESCE(em.mean_market_price, sea.sig_mean_market_price)       AS mean_market_price,
       COALESCE(em.market_price_dispersion, sea.sig_market_price_dispersion) AS market_price_dispersion,
       COALESCE(em.neg_risk, false)                                    AS neg_risk,
       COALESCE(em.neg_risk_other, false)                              AS neg_risk_other,
       COALESCE(em.n_yes_winners, 1)                                   AS n_yes_winners,
       COALESCE(em.n_markets, sea.sig_n_markets, 1)                    AS n_markets,
       es.n_detector_signals,
       es.first_signal_at,
       COALESCE(
           EXTRACT(EPOCH FROM (em.end_date - es.first_signal_at)) / 86400.0,
           sea.sig_horizon_days,
           0
       )                                                                AS days_to_resolution,
       COALESCE(m.category, sea.evidence_category)                      AS market_category,
       COALESCE(m.volume_24h, 0)                                        AS market_volume_24h
  FROM detector_signals ds
  JOIN event_signals es ON es.event_polymarket_id = ds.event_id
  LEFT JOIN signal_event_agg sea ON sea.event_polymarket_id = ds.event_id
  LEFT JOIN event_meta    em ON em.event_polymarket_id = ds.event_id
  LEFT JOIN markets       m  ON m.polymarket_id        = ds.market_id
 WHERE ds.resolved = true
   AND ds.outcome IN ('WIN', 'LOSS')
   AND ds.event_id IS NOT NULL
   AND ds.realized_roi_net IS NOT NULL
   AND ds.generated_at > now() - make_interval(days => $1)
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


async def _load_active_layer_a():
    row = await fetchrow(_FETCH_LAYER_A_SQL)
    if row is None:
        return None
    payload = row["model_payload"]
    payload_json = payload if isinstance(payload, str) else json.dumps(payload)
    return deserialize_layer_a(payload_json)


def _build_layer_a_input(r: dict[str, Any]) -> EventSelectorExample:
    return EventSelectorExample(
        event_id=str(r["event_polymarket_id"]),
        category=r["event_category"],
        archetype=r["archetype"] or "unclassified",
        n_markets=int(r["n_markets"] or 1),
        volume_24h=float(r["event_volume_24h"] or 0),
        liquidity=float(r["event_liquidity"] or 0),
        days_to_resolution=float(r["days_to_resolution"] or 0),
        mean_market_price=float(r["mean_market_price"] or 0),
        market_price_dispersion=float(r["market_price_dispersion"] or 0),
        neg_risk=bool(r["neg_risk"]),
        neg_risk_other=bool(r["neg_risk_other"]),
        n_yes_winners=int(r["n_yes_winners"]),
        n_detector_signals=int(r["n_detector_signals"]),
        outcome=0,  # не используется при predict
    )


def _row_to_market_example(
    r: dict[str, Any],
    cals: dict[str, BetaCalibrator],
    p_event: float,
) -> MarketPickerExample:
    """Build example с binary outcome AND ev target.

    binary outcome — для совместимости (mode='binary' fallback).
    net_roi_target — для mode='ev_regression'. Делим на 100 чтобы ROI in [-1, +N]
    (исходные значения в %).
    """
    detector = r["detector"]
    raw_p = float(r["raw_p"])
    cal = cals.get(detector)
    if cal is not None and cal.params is not None:
        try:
            calibrated_p = float(cal.transform(raw_p))
        except Exception:
            calibrated_p = raw_p
    else:
        calibrated_p = raw_p

    realized_roi_net = r.get("realized_roi_net")
    net_roi_target = float(realized_roi_net) / 100.0 if realized_roi_net is not None else None

    return MarketPickerExample(
        detector_id=detector,
        calibrated_p=calibrated_p,
        edge_pct=float(r["expected_edge_pct"]),
        entry_price=float(r["entry_price"]),
        horizon_sec=int(r["horizon_sec"]),
        category=r["market_category"],
        volume_24h=float(r["market_volume_24h"] or 0),
        is_v2_post=r["generated_at"] > V2_CUTOVER,
        p_event=p_event,
        archetype=r["archetype"] or "unclassified",
        n_markets_in_event=int(r["n_markets"] or 1),
        outcome=1 if r["outcome"] == "WIN" else 0,
        net_roi_target=net_roi_target,
    )


async def _persist_and_maybe_promote(
    version: str,
    payload_json: str,
    n_train_rows: int,
    n_holdout_rows: int,
    n_events_train: int,
    n_events_holdout: int,
    train_mae: float,
    holdout_mae: float,
    layer_a_threshold: float,
) -> tuple[bool, list[str]]:
    """Persist + 1-gate promotion (new MAE < active MAE × 0.95).

    Многоступенчатый gate (как в binary fit) добавим после первой неделей shadow
    данных — пока недостаточно метрик для defensive gates.
    """
    await execute(
        """
        INSERT INTO ml_market_picker (
            version, model_payload, n_train_rows, n_holdout_rows,
            n_events_train, n_events_holdout, train_per_event_brier, holdout_per_event_brier,
            layer_a_threshold, mode, fit_at, is_active
        )
        VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7, $8, $9, $10, now(), false)
        ON CONFLICT (version) DO NOTHING
        """,
        version,
        payload_json,
        n_train_rows,
        n_holdout_rows,
        n_events_train,
        n_events_holdout,
        train_mae,  # для regression mode это MAE, не Brier
        holdout_mae,
        layer_a_threshold,
        MODE,
    )

    active = await fetchrow(
        "SELECT version, holdout_per_event_brier FROM ml_market_picker WHERE is_active = true AND mode = $1",
        MODE,
    )
    active_mae = float(active["holdout_per_event_brier"]) if active else None

    block_reasons: list[str] = []
    if active_mae is not None and holdout_mae >= active_mae * PROMOTION_MAE_RATIO:
        block_reasons.append(
            f"mae_not_improved: new {holdout_mae:.4f} >= active {active_mae:.4f} × {PROMOTION_MAE_RATIO}"
        )

    promote = not block_reasons
    if promote:
        await execute(
            "UPDATE ml_market_picker SET is_active = false WHERE is_active = true AND mode = $1",
            MODE,
        )
        await execute("UPDATE ml_market_picker SET is_active = true WHERE version = $1", version)
        logger.info("ev_market_picker_promoted", version=version)
    else:
        logger.info("ev_market_picker_not_promoted", version=version, block_reasons=block_reasons)
    return promote, block_reasons


async def run_ml_market_picker_ev_fit() -> dict[str, Any]:
    """Weekly Layer B EV-regression fit cron entry point."""
    layer_a = await _load_active_layer_a()
    if layer_a is None:
        logger.warning("ev_market_picker_no_active_layer_a")
        return {"status": "skipped", "reason": "no_active_layer_a"}

    rows = await fetch(_FETCH_TRAINING_SQL, ROLLING_DAYS)
    if len(rows) < MIN_TRAIN_ROWS:
        return {"status": "skipped", "reason": "too_few_rows", "n": len(rows)}

    rows_dicts = [dict(r) for r in rows]

    # Step 1: p_event для уникальных events через Layer A
    unique_events: dict[str, dict[str, Any]] = {}
    for r in rows_dicts:
        ev = str(r["event_polymarket_id"])
        if ev not in unique_events:
            unique_events[ev] = r
    layer_a_inputs = [_build_layer_a_input(r) for r in unique_events.values()]
    layer_a_preds = layer_a.predict(layer_a_inputs)
    p_event_by: dict[str, float] = dict(zip(unique_events.keys(), [float(p) for p in layer_a_preds], strict=True))

    # Step 2: filter by Layer A threshold
    cals = await _load_l1_calibrators()
    examples: list[MarketPickerExample] = []
    event_ids: list[str] = []
    sample_weight: list[float] = []
    for r in rows_dicts:
        ev = str(r["event_polymarket_id"])
        p_event = p_event_by[ev]
        if p_event < LAYER_A_THRESHOLD:
            continue
        examples.append(_row_to_market_example(r, cals, p_event))
        event_ids.append(ev)
        sample_weight.append(1.0 / max(int(r["event_size"]), 1))

    if len(examples) < MIN_TRAIN_ROWS:
        return {
            "status": "skipped",
            "reason": "too_few_after_layer_a_filter",
            "n": len(examples),
            "threshold": LAYER_A_THRESHOLD,
        }

    # Step 3: Train Layer B EV-regressor
    cfg = MarketPickerConfig.for_ev_regression()
    cfg.min_train_rows = MIN_TRAIN_ROWS
    model = train_market_picker(
        examples,
        event_ids=event_ids,
        sample_weight=sample_weight,
        config=cfg,
        holdout_fraction=0.20,
        seed=42,
    )

    # Step 4: persist + promote (1-gate by MAE)
    version = datetime.now(UTC).strftime("market_picker_ev_v%Y%m%d_%H%M%S")
    payload_json = json.dumps(model.serialize())
    promoted, block_reasons = await _persist_and_maybe_promote(
        version=version,
        payload_json=payload_json,
        n_train_rows=model.n_train_rows,
        n_holdout_rows=model.n_holdout_rows,
        n_events_train=model.n_events_train,
        n_events_holdout=model.n_events_holdout,
        train_mae=round(model.train_per_event_brier, 6),  # field reused — это MAE для regression
        holdout_mae=round(model.holdout_per_event_brier, 6),
        layer_a_threshold=LAYER_A_THRESHOLD,
    )

    return {
        "status": "ok",
        "mode": MODE,
        "version": version,
        "n_train_rows": model.n_train_rows,
        "n_holdout_rows": model.n_holdout_rows,
        "n_events_holdout": model.n_events_holdout,
        "holdout_mae": round(model.holdout_per_event_brier, 6),
        "train_mae": round(model.train_per_event_brier, 6),
        "promoted": promoted,
        "block_reasons": block_reasons,
    }
