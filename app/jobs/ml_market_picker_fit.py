"""ml_market_picker_fit — weekly cron (Sunday 04:30 UTC, после Layer A at 03:30).

Stage 3 / Two-Stage ML Pipeline. Layer B = Market+Side Picker.

Workflow:
1. Loads active Layer A model (returns skipped if absent)
2. Fetches detector_signals + event aggregations
3. Computes p_event для каждого unique event'а через Layer A.predict
4. Filters universe: только rows где p_event > LAYER_A_THRESHOLD (default 0.50)
5. Применяет L1 Beta calibrators для calibrated_p
6. Train Layer B с GroupKFold(event_id) + sample_weight=1/event_size
7. Per-category metrics + top-1 accuracy + HF/Live разделение
8. **3-gate promotion** (все должны пройти):
   A. Standard: new < active * 0.95
   B. HF-overfit guard: live_brier ≤ hf_brier × 1.10 (если live_n_events ≥ 30)
   C. Unsafe categories: Tech&AI, World&Economy НЕ должны быть хуже coin (0.25)

Anchor (см. memory/honest_baseline_2026-05-01.md):
- D1 baseline per-event Brier: 0.168 (главный gate)
- D5: 0.183
- Layer B должен улучшать D1 (где надёжная статистика)
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
    top1_per_event_accuracy,
    train_market_picker,
)
from app.calibration.per_event_metrics import per_event_brier
from app.services.database import execute, fetch, fetchrow

logger = structlog.get_logger()

ROLLING_DAYS = 365
MIN_TRAIN_ROWS = 100
LAYER_A_THRESHOLD = 0.50
VALIDATION_GATE_ENABLED = True  # block promote if validate_model.passes_gate is False
PROMOTION_BRIER_RATIO = 0.95
HF_LIVE_RATIO_GATE = 1.10  # live ≤ hf × 1.10
NAIVE_COIN_BRIER = 0.25  # 50% prob, mixed outcomes
V2_CUTOVER = datetime(2026, 4, 28, tzinfo=UTC)
UNSAFE_CATEGORIES = ("Tech & AI", "World & Economy")
UNSAFE_CATEGORY_MIN_N_EVENTS = 10  # exemption если выборка слишком мала

_FETCH_LAYER_A_SQL = "SELECT model_payload FROM ml_event_selector WHERE is_active = true"

_FETCH_TRAINING_SQL = """
WITH event_signals AS (
    SELECT ds.event_id              AS event_polymarket_id,
           COUNT(*)                  AS n_detector_signals,
           MIN(ds.generated_at)      AS first_signal_at
      FROM detector_signals ds
     WHERE ds.event_id IS NOT NULL
       AND ds.resolved = true
       AND ds.outcome IN ('WIN', 'LOSS')
       AND ds.generated_at > now() - make_interval(days => $1)
     GROUP BY ds.event_id
),
signal_event_agg AS (
    -- Aggregates из detector_signals.evidence (доступно ВСЕМ events incl.
    -- backfilled). Symmetric с Layer A fix 2026-05-14.
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
    -- Live events: full attributes (NULL для backfilled, fallback из signal_event_agg ниже).
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
       -- event_size: prefer column, fallback к computed n_markets из aggregates
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


def _row_to_market_example(r: dict[str, Any], cals: dict[str, BetaCalibrator], p_event: float) -> MarketPickerExample:
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
    )


def _rescore_active_brier_picker(active_payload: str, examples: list, event_ids: list) -> float | None:
    """Re-score active Layer B на текущих examples → per-event Brier.

    Переиспользует per_event_brier (та же агрегация что в fit). Чинит «яблоки с
    апельсинами»: active baseline должен меряться на ТОЙ ЖЕ выборке что кандидат.
    Возвращает None если payload битый/пустой или examples пуст — caller fallback.
    """
    if not examples or not active_payload:
        return None
    try:
        import json

        from app.calibration.market_picker import TrainedMarketPicker

        # JSONB-codec guard: payload приходит как str (asyncpg без jsonb-codec), но если
        # codec dict→jsonb добавят, json.loads(dict) упадёт → принимаем dict напрямую.
        data = json.loads(active_payload) if isinstance(active_payload, str) else active_payload
        model = TrainedMarketPicker.deserialize(data)
        preds = [float(p) for p in model.predict(examples)]
        outcomes = [int(ex.outcome) for ex in examples]
        return per_event_brier(event_ids, preds, outcomes)
    except Exception as exc:  # битый payload, version mismatch, len mismatch
        logger.warning("market_picker_rescore_failed", error=str(exc))
        return None


def _evaluate_3gate_promotion(
    new_brier: float,
    active_brier: float | None,
    hf_brier: float | None,
    hf_n: int,
    live_brier: float | None,
    live_n: int,
    per_category: dict[str, dict[str, Any]],
    active_live_brier: float | None = None,
    candidate_live_brier: float | None = None,
) -> tuple[bool, list[str]]:
    """Returns (promote, block_reasons)."""
    block_reasons: list[str] = []

    # Gate A: standard improvement — live-holdout re-score (bot ставит в live).
    if active_brier is not None:
        if active_live_brier is not None and candidate_live_brier is not None:
            if candidate_live_brier >= active_live_brier * PROMOTION_BRIER_RATIO:
                block_reasons.append(
                    f"live: cand {candidate_live_brier:.4f} >= active_rescored "
                    f"{active_live_brier:.4f} × {PROMOTION_BRIER_RATIO}"
                )
        else:
            # Fallback: нет live-holdout / payload битый → frozen scalar на общем holdout
            if new_brier >= active_brier * PROMOTION_BRIER_RATIO:
                block_reasons.append(
                    f"fallback-standard: new {new_brier:.4f} >= active {active_brier:.4f} × {PROMOTION_BRIER_RATIO}"
                )

    # Gate B: HF-overfit guard
    if hf_brier is not None and live_brier is not None and hf_n >= 30 and live_n >= 30:
        if live_brier > hf_brier * HF_LIVE_RATIO_GATE:
            block_reasons.append(f"hf_overfit: live {live_brier:.4f} > hf {hf_brier:.4f} × {HF_LIVE_RATIO_GATE}")

    # Gate C: unsafe categories
    for cat in UNSAFE_CATEGORIES:
        m = per_category.get(cat)
        if m is None:
            continue
        if m["n_events"] >= UNSAFE_CATEGORY_MIN_N_EVENTS and m["brier"] > NAIVE_COIN_BRIER:
            block_reasons.append(
                f"unsafe_category[{cat}]: brier {m['brier']:.4f} > coin {NAIVE_COIN_BRIER} (n_events={m['n_events']})"
            )

    promote = not block_reasons
    return promote, block_reasons


async def _persist_and_maybe_promote(
    version: str,
    payload_json: str,
    n_train_rows: int,
    n_holdout_rows: int,
    n_events_train: int,
    n_events_holdout: int,
    train_brier: float,
    holdout_brier: float,
    hf_brier: float | None,
    hf_n: int,
    live_brier: float | None,
    live_n: int,
    per_category: dict[str, dict[str, Any]],
    top1_acc: float,
    layer_a_threshold: float,
    validation_failures: list[str] | None = None,
    live_holdout_examples: list | None = None,
    live_holdout_event_ids: list | None = None,
    candidate_live_brier: float | None = None,
) -> tuple[bool, list[str]]:
    await execute(
        """
        INSERT INTO ml_market_picker (
            version, model_payload, n_train_rows, n_holdout_rows,
            n_events_train, n_events_holdout, train_per_event_brier, holdout_per_event_brier,
            hf_holdout_brier, hf_n_events, live_holdout_brier, live_n_events,
            per_category_metrics, top1_per_event_accuracy, layer_a_threshold,
            fit_at, is_active
        )
        VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14, $15, now(), false)
        ON CONFLICT (version) DO NOTHING
        """,
        version,
        payload_json,
        n_train_rows,
        n_holdout_rows,
        n_events_train,
        n_events_holdout,
        train_brier,
        holdout_brier,
        hf_brier,
        hf_n,
        live_brier,
        live_n,
        json.dumps(per_category),
        top1_acc,
        layer_a_threshold,
    )

    active = await fetchrow(
        "SELECT version, holdout_per_event_brier, model_payload FROM ml_market_picker WHERE is_active = true"
    )
    active_brier = float(active["holdout_per_event_brier"]) if active else None
    active_live_brier = None
    if active is not None and live_holdout_examples:
        active_live_brier = _rescore_active_brier_picker(
            active.get("model_payload") or "",
            live_holdout_examples,
            live_holdout_event_ids or [],
        )

    promote, block_reasons = _evaluate_3gate_promotion(
        new_brier=holdout_brier,
        active_brier=active_brier,
        hf_brier=hf_brier,
        hf_n=hf_n,
        live_brier=live_brier,
        live_n=live_n,
        per_category=per_category,
        active_live_brier=active_live_brier,
        candidate_live_brier=candidate_live_brier,
    )

    # Validation gate (validate_model: per-band AUC + ECE + BSS + cal slope)
    if promote and validation_failures:
        promote = False
        block_reasons = list(block_reasons) + [f"VALIDATION_GATE_FAILED: {f}" for f in validation_failures]

    if promote:
        await execute("UPDATE ml_market_picker SET is_active = false WHERE is_active = true")
        await execute(
            "UPDATE ml_market_picker SET is_active = true, last_skip_reason = NULL WHERE version = $1",
            version,
        )
        logger.info("market_picker_promoted", version=version)
    else:
        # Сохраняем причину НЕ-промоута для наблюдаемости (frozen-by-gate vs broken).
        await execute(
            "UPDATE ml_market_picker SET last_skip_reason = $2 WHERE version = $1",
            version,
            "; ".join(block_reasons) if block_reasons else "blocked",
        )
        logger.warning("market_picker_not_promoted", version=version, block_reasons=block_reasons)
    return promote, block_reasons


async def run_ml_market_picker_fit() -> dict[str, Any]:
    """Weekly Layer B fit cron entry point."""
    layer_a = await _load_active_layer_a()
    if layer_a is None:
        logger.warning("market_picker_no_active_layer_a")
        return {"status": "skipped", "reason": "no_active_layer_a"}

    rows = await fetch(_FETCH_TRAINING_SQL, ROLLING_DAYS)
    if len(rows) < MIN_TRAIN_ROWS:
        return {"status": "skipped", "reason": "too_few_rows", "n": len(rows)}

    rows_dicts = [dict(r) for r in rows]

    # Step 1: Compute p_event для уникальных events через Layer A
    unique_events: dict[str, dict[str, Any]] = {}
    for r in rows_dicts:
        ev = str(r["event_polymarket_id"])
        if ev not in unique_events:
            unique_events[ev] = r
    layer_a_inputs = [_build_layer_a_input(r) for r in unique_events.values()]
    layer_a_preds = layer_a.predict(layer_a_inputs)
    p_event_by: dict[str, float] = dict(zip(unique_events.keys(), [float(p) for p in layer_a_preds], strict=True))

    # Step 2: Filter universe by Layer A threshold
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

    # Step 3: Train Layer B
    cfg = MarketPickerConfig(min_train_rows=MIN_TRAIN_ROWS)
    model = train_market_picker(
        examples,
        event_ids=event_ids,
        sample_weight=sample_weight,
        config=cfg,
        holdout_fraction=0.20,
        seed=42,
    )

    # Step 4: Per-category breakdown на holdout
    holdout_idx = model.holdout_indices
    holdout_ex = [examples[i] for i in holdout_idx]
    holdout_y = [examples[i].outcome for i in holdout_idx]
    holdout_ev = [event_ids[i] for i in holdout_idx]
    holdout_pred = [float(p) for p in model.predict(holdout_ex)]

    per_category: dict[str, dict[str, Any]] = {}
    cats = {ex.category for ex in holdout_ex if ex.category}
    for cat in cats:
        idxs = [i for i, ex in enumerate(holdout_ex) if ex.category == cat]
        if not idxs:
            continue
        cat_ev = [holdout_ev[i] for i in idxs]
        cat_p = [holdout_pred[i] for i in idxs]
        cat_y = [holdout_y[i] for i in idxs]
        per_category[cat] = {
            "brier": round(per_event_brier(cat_ev, cat_p, cat_y), 6),
            "n_events": len(set(cat_ev)),
            "n_rows": len(idxs),
            "win_rate": round(sum(cat_y) / max(len(cat_y), 1), 4),
        }

    # Step 5: HF / Live разделение
    hf_idx = [i for i, ex in enumerate(holdout_ex) if not ex.is_v2_post]
    live_idx = [i for i, ex in enumerate(holdout_ex) if ex.is_v2_post]

    def _slice_brier(idxs: list[int]) -> tuple[float | None, int]:
        if not idxs:
            return None, 0
        evs = [holdout_ev[i] for i in idxs]
        ps = [holdout_pred[i] for i in idxs]
        ys = [holdout_y[i] for i in idxs]
        return round(per_event_brier(evs, ps, ys), 6), len(set(evs))

    hf_brier, hf_n_events = _slice_brier(hf_idx)
    live_brier, live_n_events = _slice_brier(live_idx)

    # Step 6: Top-1 per-event accuracy
    top1 = top1_per_event_accuracy(holdout_ev, holdout_pred, holdout_y)

    # Validation gate (validate_model на полном holdout)
    version = datetime.now(UTC).strftime("market_picker_v%Y%m%d_%H%M%S")
    validation_failures: list[str] = []
    if VALIDATION_GATE_ENABLED and holdout_ex and len(set(holdout_y)) >= 2:
        try:
            from app.calibration.validation import format_report, validate_model

            seg_vals = [float(ex.entry_price) for ex in holdout_ex]
            report = validate_model(
                name=version,
                y_true=holdout_y,
                y_pred=holdout_pred,
                market_price=seg_vals,
                segment_value=seg_vals,
                event_ids=holdout_ev,
                roi_per_row=None,
            )
            logger.info(
                "market_picker_validation_report",
                name=version,
                passes_gate=report.passes_gate,
                failures=report.failures,
            )
            print(format_report(report), flush=True)
            if not report.passes_gate:
                validation_failures = report.failures
        except Exception as exc:
            logger.warning("market_picker_validation_failed", error=str(exc))

    # Step 7: Persist + 3-gate promotion + validation gate
    payload_json = json.dumps(model.serialize())
    promoted, block_reasons = await _persist_and_maybe_promote(
        version=version,
        payload_json=payload_json,
        n_train_rows=model.n_train_rows,
        n_holdout_rows=model.n_holdout_rows,
        n_events_train=model.n_events_train,
        n_events_holdout=model.n_events_holdout,
        train_brier=round(model.train_per_event_brier, 6),
        holdout_brier=round(model.holdout_per_event_brier, 6),
        hf_brier=hf_brier,
        hf_n=hf_n_events,
        live_brier=live_brier,
        live_n=live_n_events,
        per_category=per_category,
        top1_acc=round(top1, 4),
        layer_a_threshold=LAYER_A_THRESHOLD,
        validation_failures=validation_failures,
        live_holdout_examples=[holdout_ex[i] for i in live_idx],
        live_holdout_event_ids=[holdout_ev[i] for i in live_idx],
        candidate_live_brier=live_brier,
    )

    return {
        "status": "ok",
        "version": version,
        "n_train_rows": model.n_train_rows,
        "n_holdout_rows": model.n_holdout_rows,
        "n_events_holdout": model.n_events_holdout,
        "holdout_per_event_brier": round(model.holdout_per_event_brier, 6),
        "hf_holdout_brier": hf_brier,
        "hf_n_events": hf_n_events,
        "live_holdout_brier": live_brier,
        "live_n_events": live_n_events,
        "top1_per_event_accuracy": round(top1, 4),
        "per_category_metrics": per_category,
        "promoted": promoted,
        "block_reasons": block_reasons,
    }
