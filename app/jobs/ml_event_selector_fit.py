"""ml_event_selector_fit — weekly cron (Sunday 03:30 UTC).

Stage 2 / Two-Stage ML Pipeline. Layer A — Event Selector.

1. Тянет per-event агрегации из mv_event_outcome + events + markets
2. Универсум: events с n_signals ≥ 1 (хотя бы один детектор сработал)
3. Time-based split по min(detector_signals.generated_at) per-event
4. Train LGB Layer A
5. Раздельные holdout метрики на HF (pre-V2) / Live (post-V2) — обязательно
6. Persist в ml_event_selector
7. Promotion rule:
   - No active model → promote
   - Else: new holdout_per_event_brier < active * 0.95 → promote
   - Иначе: skip + log

Ground truth: event_roi > 0 (stake-weighted ROI из mv_event_outcome).
Fallback: mean(realized_roi) если pipeline_v2_shadow_positions пуст.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog

from app.calibration.event_selector import (
    EventSelectorConfig,
    EventSelectorExample,
    train_event_selector,
)
from app.services.database import execute, fetch, fetchrow

logger = structlog.get_logger()

ROLLING_DAYS = 365  # вся история
MIN_TRAIN_EVENTS = 50
PROMOTION_BRIER_RATIO = 0.95
V2_CUTOVER = datetime(2026, 4, 28, tzinfo=UTC)
VALIDATION_GATE_ENABLED = True  # block promote if validate_model.passes_gate is False


# Универсум: events которые в mv_event_outcome (≥1 detector signal + resolved).
#
# Стратегия (2026-05-14 update):
#   Для **backfilled events** (source='hf_backfill') canonical events НЕ в `events`
#   table (canonical condition_ids ≠ Polymarket Gamma events). Чтобы Layer A видел
#   и backfilled и live data, используем 2-track approach:
#
#   1. signal_event_agg — агрегируем attributes из `detector_signals.evidence`
#      (всегда populated, ~99% backfilled signals имеют category).
#   2. event_market_agg — LEFT JOIN на `events`+`markets` для live data (volume_24h,
#      liquidity, neg_risk, archetype недоступны из evidence).
#
# В финальном SELECT — COALESCE: prefer events table если есть, fallback к
# signal aggregates.
_FETCH_TRAINING_SQL = """
WITH event_signals AS (
    SELECT ds.event_id              AS event_polymarket_id,
           COUNT(*)                  AS n_detector_signals,
           MIN(ds.generated_at)      AS first_signal_at,
           BOOL_OR(ds.source = 'hf_backfill') AS has_backfill
      FROM detector_signals ds
     WHERE ds.event_id IS NOT NULL
       AND ds.resolved = true
       AND ds.outcome IN ('WIN', 'LOSS')
       AND ds.generated_at > now() - make_interval(days => $1)
     GROUP BY ds.event_id
),
signal_event_agg AS (
    -- Aggregates из самих detector_signals (доступно ВСЕМ events incl. backfilled).
    -- Достаём category из evidence (99% backfilled имеют), горизонт из horizon_sec.
    SELECT ds.event_id                                   AS event_polymarket_id,
           MODE() WITHIN GROUP (ORDER BY ds.evidence->>'category')  AS evidence_category,
           COUNT(DISTINCT ds.market_id)                  AS sig_n_markets,
           AVG(ds.current_price)                         AS sig_mean_market_price,
           STDDEV_POP(ds.current_price)                  AS sig_market_price_dispersion,
           MAX((ds.evidence->>'horizon_days')::numeric)  AS sig_horizon_days,
           MAX(ds.resolved_at)                           AS sig_last_resolved_at
      FROM detector_signals ds
     WHERE ds.event_id IS NOT NULL
       AND ds.resolved = true
       AND ds.outcome IN ('WIN', 'LOSS')
     GROUP BY ds.event_id
),
event_market_agg AS (
    -- Live events: full attributes из events+markets tables (NULL для backfilled).
    SELECT e.polymarket_id                              AS event_polymarket_id,
           e.category,
           e.archetype,
           COALESCE(e.volume_24h, 0)                    AS volume_24h,
           COALESCE(e.liquidity, 0)                     AS liquidity,
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
SELECT es.event_polymarket_id,
       COALESCE(ema.category, sea.evidence_category)            AS category,
       COALESCE(ema.archetype, 'unclassified')                  AS archetype,
       COALESCE(ema.n_markets, sea.sig_n_markets, 1)            AS n_markets,
       COALESCE(ema.volume_24h, 0)                              AS volume_24h,
       COALESCE(ema.liquidity, 0)                               AS liquidity,
       COALESCE(ema.mean_market_price, sea.sig_mean_market_price) AS mean_market_price,
       COALESCE(ema.market_price_dispersion, sea.sig_market_price_dispersion) AS market_price_dispersion,
       COALESCE(ema.neg_risk, false)                            AS neg_risk,
       COALESCE(ema.neg_risk_other, false)                      AS neg_risk_other,
       COALESCE(ema.n_yes_winners, 1)                           AS n_yes_winners,
       es.n_detector_signals,
       es.first_signal_at,
       -- days_to_resolution: для live из ema.end_date, для backfilled из evidence
       COALESCE(
           EXTRACT(EPOCH FROM (ema.end_date - es.first_signal_at)) / 86400.0,
           sea.sig_horizon_days,
           0
       )                                                         AS days_to_resolution,
       mvo.event_roi
  FROM event_signals es
  JOIN mv_event_outcome mvo  ON mvo.event_id           = es.event_polymarket_id
  LEFT JOIN signal_event_agg sea ON sea.event_polymarket_id = es.event_polymarket_id
  LEFT JOIN event_market_agg ema ON ema.event_polymarket_id = es.event_polymarket_id
"""


def _row_to_example(r: dict[str, Any]) -> tuple[EventSelectorExample, float]:
    """Returns (example, timestamp_for_split)."""
    event_roi = float(r["event_roi"] or 0.0)
    outcome = 1 if event_roi > 0 else 0

    ex = EventSelectorExample(
        event_id=str(r["event_polymarket_id"]),
        category=r["category"],
        archetype=r["archetype"] or "unclassified",
        n_markets=int(r["n_markets"] or 1),
        volume_24h=float(r["volume_24h"] or 0),
        liquidity=float(r["liquidity"] or 0),
        days_to_resolution=float(r["days_to_resolution"] or 0),
        mean_market_price=float(r["mean_market_price"] or 0),
        market_price_dispersion=float(r["market_price_dispersion"] or 0),
        neg_risk=bool(r["neg_risk"]),
        neg_risk_other=bool(r["neg_risk_other"]),
        n_yes_winners=int(r["n_yes_winners"]),
        n_detector_signals=int(r["n_detector_signals"]),
        outcome=outcome,
    )
    ts = r["first_signal_at"].timestamp() if r["first_signal_at"] else 0.0
    return ex, ts


def _rescore_active_brier(active_payload: str, examples: list) -> float | None:
    """Re-score active model на текущем наборе examples → per-event Brier.

    Чинит «яблоки с апельсинами»: active baseline должен меряться на ТОЙ ЖЕ
    выборке что и кандидат, а не на frozen скаляре из БД (мерян на старом holdout).
    Возвращает None если payload битый/пустой или examples пуст — caller делает fallback.
    """
    if not examples or not active_payload:
        return None
    try:
        import json

        from app.calibration.event_selector import deserialize_from_json
        from app.calibration.per_event_metrics import per_event_brier

        # JSONB-codec guard: today asyncpg возвращает model_payload как str, но если
        # codec dict→jsonb добавят project-wide, нормализуем обратно в str (mirror _load_active_layer_a).
        payload_json = active_payload if isinstance(active_payload, str) else json.dumps(active_payload)
        model = deserialize_from_json(payload_json)
        event_ids = [ex.event_id for ex in examples]
        preds = [float(p) for p in model.predict(examples)]
        outcomes = [int(ex.outcome) for ex in examples]
        return per_event_brier(event_ids, preds, outcomes)
    except Exception as exc:  # битый payload, version mismatch, len mismatch
        logger.warning("event_selector_rescore_failed", error=str(exc))
        return None


async def _persist_and_maybe_promote(
    version: str,
    payload_json: str,
    n_train: int,
    n_holdout: int,
    train_brier: float,
    holdout_brier: float,
    hf_brier: float | None,
    hf_n: int,
    live_brier: float | None,
    live_n: int,
    validation_failures: list[str] | None = None,
    live_holdout_examples: list | None = None,
    candidate_live_brier: float | None = None,
) -> tuple[bool, str]:
    await execute(
        """
        INSERT INTO ml_event_selector (
            version, model_payload, n_events_train, n_events_holdout,
            train_per_event_brier, holdout_per_event_brier,
            hf_holdout_brier, hf_n_events, live_holdout_brier, live_n_events,
            fit_at, is_active
        )
        VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7, $8, $9, $10, now(), false)
        ON CONFLICT (version) DO NOTHING
        """,
        version,
        payload_json,
        n_train,
        n_holdout,
        train_brier,
        holdout_brier,
        hf_brier,
        hf_n,
        live_brier,
        live_n,
    )

    active = await fetchrow(
        "SELECT version, holdout_per_event_brier, model_payload FROM ml_event_selector WHERE is_active = true"
    )

    if active is None:
        promote = True
        reason = "no_active_model"
    else:
        active_scalar_brier = float(active["holdout_per_event_brier"])
        # Re-score active на ТЕКУЩЕМ live-holdout → сопоставимое сравнение.
        # Bot ставит в live → судим кандидата по live-Brier vs live-Brier active.
        active_live_brier = _rescore_active_brier(active.get("model_payload") or "", live_holdout_examples or [])
        if active_live_brier is not None and candidate_live_brier is not None:
            promote = candidate_live_brier < active_live_brier * PROMOTION_BRIER_RATIO
            reason = (
                f"live (cand {candidate_live_brier:.4f} vs active_rescored {active_live_brier:.4f}, "
                f"ratio<{PROMOTION_BRIER_RATIO})"
            )
        else:
            promote = holdout_brier < active_scalar_brier * PROMOTION_BRIER_RATIO
            reason = (
                f"fallback-standard (new {holdout_brier:.4f} vs active {active_scalar_brier:.4f}, "
                f"ratio<{PROMOTION_BRIER_RATIO})"
            )

    # Validation gate — блокирует promote независимо от Brier-ratio
    if promote and validation_failures:
        promote = False
        reason = f"VALIDATION_GATE_FAILED: {'; '.join(validation_failures)}"

    if promote:
        await execute("UPDATE ml_event_selector SET is_active = false WHERE is_active = true")
        await execute(
            "UPDATE ml_event_selector SET is_active = true, last_skip_reason = NULL WHERE version = $1",
            version,
        )
        logger.info("event_selector_promoted", version=version, reason=reason)
    else:
        # Сохраняем причину НЕ-промоута для наблюдаемости (frozen-by-gate vs broken).
        await execute(
            "UPDATE ml_event_selector SET last_skip_reason = $2 WHERE version = $1",
            version,
            reason,
        )
        logger.warning("event_selector_not_promoted", version=version, reason=reason)
    return promote, reason


async def run_ml_event_selector_fit() -> dict[str, Any]:
    """Weekly Layer A fit cron entry point."""
    rows = await fetch(_FETCH_TRAINING_SQL, ROLLING_DAYS)
    if len(rows) < MIN_TRAIN_EVENTS:
        logger.warning("event_selector_too_few_events", n=len(rows), min=MIN_TRAIN_EVENTS)
        return {"status": "skipped", "reason": "too_few_events", "n": len(rows)}

    pairs = [_row_to_example(dict(r)) for r in rows]
    examples = [p[0] for p in pairs]
    timestamps = [p[1] for p in pairs]

    cfg = EventSelectorConfig(min_train_events=MIN_TRAIN_EVENTS)
    model = train_event_selector(examples, timestamps, config=cfg, holdout_fraction=0.20, seed=42)

    # Раздельные holdout метрики на HF / Live
    order = sorted(range(len(examples)), key=lambda i: timestamps[i])
    cut = int(len(order) * 0.80)
    holdout_idx = order[cut:]

    cutover_ts = V2_CUTOVER.timestamp()
    hf_idx = [i for i in holdout_idx if timestamps[i] < cutover_ts]
    live_idx = [i for i in holdout_idx if timestamps[i] >= cutover_ts]

    def _brier_for(idxs: list[int]) -> tuple[float | None, int]:
        if not idxs:
            return None, 0
        ex = [examples[i] for i in idxs]
        y = np.asarray([examples[i].outcome for i in idxs], dtype=float)
        p = model.predict(ex)
        # 1 row = 1 event (query GROUP BY event_id) → per-row MSE == per-event Brier
        return float(np.mean((p - y) ** 2)), len(idxs)

    import numpy as np

    hf_brier, hf_n = _brier_for(hf_idx)
    live_brier, live_n = _brier_for(live_idx)

    version = datetime.now(UTC).strftime("event_selector_v%Y%m%d_%H%M%S")
    payload_json = json.dumps(model.serialize())

    # Validation gate на полном holdout
    validation_failures: list[str] = []
    if VALIDATION_GATE_ENABLED and holdout_idx:
        try:
            from app.calibration.validation import format_report, validate_model

            holdout_ex = [examples[i] for i in holdout_idx]
            y_true = [int(ex.outcome) for ex in holdout_ex]
            if len(set(y_true)) >= 2:
                preds = model.predict(holdout_ex).tolist()
                seg_vals = [float(ex.mean_market_price) for ex in holdout_ex]
                event_ids = [ex.event_id for ex in holdout_ex]
                report = validate_model(
                    name=version,
                    y_true=y_true,
                    y_pred=preds,
                    market_price=seg_vals,
                    segment_value=seg_vals,
                    event_ids=event_ids,
                    roi_per_row=None,
                )
                logger.info(
                    "event_selector_validation_report",
                    name=version,
                    passes_gate=report.passes_gate,
                    failures=report.failures,
                )
                print(format_report(report), flush=True)
                if not report.passes_gate:
                    validation_failures = report.failures
        except Exception as exc:
            logger.warning("event_selector_validation_failed", error=str(exc))

    promoted, reason = await _persist_and_maybe_promote(
        version=version,
        payload_json=payload_json,
        n_train=model.n_events_train,
        n_holdout=model.n_events_holdout,
        train_brier=round(model.train_per_event_brier, 6),
        holdout_brier=round(model.holdout_per_event_brier, 6),
        hf_brier=round(hf_brier, 6) if hf_brier is not None else None,
        hf_n=hf_n,
        live_brier=round(live_brier, 6) if live_brier is not None else None,
        live_n=live_n,
        validation_failures=validation_failures,
        live_holdout_examples=[examples[i] for i in live_idx],
        candidate_live_brier=live_brier,
    )

    return {
        "status": "ok",
        "version": version,
        "n_events_train": model.n_events_train,
        "n_events_holdout": model.n_events_holdout,
        "holdout_per_event_brier": round(model.holdout_per_event_brier, 6),
        "hf_holdout_brier": round(hf_brier, 6) if hf_brier is not None else None,
        "hf_n_events": hf_n,
        "live_holdout_brier": round(live_brier, 6) if live_brier is not None else None,
        "live_n_events": live_n,
        "promoted": promoted,
        "promote_reason": reason,
    }
