"""Two-stage inference: detector → L1 Beta → Layer A → gate → Layer B → output.

Stage 4 / Two-Stage ML Pipeline.

Параллельный путь к app.services.detector_signal_service.fetch_calibrated_detector_signals
(монолитный L2). Активируется через config.pipeline_version == "v2_two_stage".

Cold-start правила:
- L1 missing → calibrated_p = raw_p
- Layer A missing → p_event = None, passed_layer_a_gate = True (no gate)
- Layer B missing → meta_p = calibrated_p
- Layer A fail (p_event < threshold) → meta_p = calibrated_p, passed_layer_a_gate = False

Сигнатура совместима с fetch_calibrated_detector_signals — возвращает list of dicts
с теми же ключами + дополнительные:
- p_event: float | None
- passed_layer_a_gate: bool
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog

from app.calibration.event_selector import EventSelectorExample
from app.calibration.event_selector import deserialize_from_json as deserialize_layer_a
from app.calibration.fallback_chain import CAL_SOURCES
from app.calibration.fallback_chain import calibrate as _calibrate
from app.calibration.fallback_chain import load_l1_calibrators as _load_l1_calibrators
from app.calibration.fallback_chain import load_lgb_calibrators as _load_lgb_calibrators
from app.calibration.market_picker import MarketPickerExample
from app.calibration.market_picker import deserialize_from_json as deserialize_layer_b
from app.core.config import get_settings
from app.jobs.ml_calibrator_fit import _PER_ANCHOR_DETECTORS
from app.services.database import fetch, fetchrow
from app.services.jsonb_utils import parse_jsonb

logger = structlog.get_logger()

V2_CUTOVER = datetime(2026, 4, 28, tzinfo=UTC)
LAYER_A_THRESHOLD_DEFAULT = 0.50


_FETCH_FRESH_SIGNALS_SQL = """
WITH event_signals AS (
    SELECT ds.event_id              AS event_polymarket_id,
           COUNT(*)                  AS n_detector_signals
      FROM detector_signals ds
     WHERE ds.event_id IS NOT NULL
       AND ds.generated_at > now() - make_interval(days => 7)
     GROUP BY ds.event_id
)
SELECT ds.id,
       ds.detector,
       ds.market_id,
       ds.event_id,
       ds.side,
       ds.confidence       AS raw_p,
       ds.expected_edge_pct,
       ds.fair_price,
       ds.current_price,   -- LGB feature builder — отдельно от entry_price (раскладка YES/NO)
       -- entry_price = стоимость 1 share нашей стороны (side-aware).
       -- ds.current_price хранится как YES-цена; для NO платим (1 - YES).
       -- Критично для EV-sizer: payout = 1/entry_price.
       CASE WHEN ds.side = 'YES' THEN ds.current_price
            ELSE 1.0 - ds.current_price
       END                  AS entry_price,
       ds.horizon_sec,
       ds.token_id,
       -- YES-токен для live-price телеметрии: get_live_yes_price ждёт YES-токен,
       -- а token_id выше — токен ТОРГУЕМОЙ стороны (для NO это NO-токен).
       m.clob_token_ids->>0 AS yes_token_id,
       ds.evidence,
       ds.generated_at,
       ds.event_size,
       ds.archetype,
       m.category,
       m.volume_24h,
       m.end_date          AS market_end_date,
       e.volume_24h        AS event_volume_24h,
       e.liquidity         AS event_liquidity,
       COALESCE(e.neg_risk, false)        AS neg_risk,
       COALESCE(e.neg_risk_other, false)  AS neg_risk_other,
       COALESCE(e.n_yes_winners, 1)       AS n_yes_winners,
       e.end_date,
       (SELECT COUNT(*) FROM markets m2 WHERE m2.event_id = e.id) AS n_markets,
       (SELECT AVG(m2.current_price) FROM markets m2 WHERE m2.event_id = e.id) AS mean_market_price,
       (SELECT STDDEV_POP(m2.current_price) FROM markets m2 WHERE m2.event_id = e.id)
                                                                 AS market_price_dispersion,
       COALESCE(es.n_detector_signals, 1) AS n_detector_signals_event,
       EXTRACT(EPOCH FROM (e.end_date - ds.generated_at)) / 86400.0 AS days_to_resolution,
       -- Tier 3.1: hours_to_resolution на момент signal (для LGB feature)
       CASE
           WHEN m.end_date IS NOT NULL
           THEN EXTRACT(epoch FROM (m.end_date - ds.generated_at)) / 3600.0
           ELSE NULL
       END AS hours_to_resolution_at_signal,
       -- Tier 3.1: rolling 30d window метрики (strictly < generated_at — leak-free)
       rw.recent_wr_30d,
       rw.detector_recent_count_30d
  FROM detector_signals ds
  LEFT JOIN markets m ON m.polymarket_id = ds.market_id
  LEFT JOIN events e  ON e.polymarket_id = ds.event_id
  LEFT JOIN event_signals es ON es.event_polymarket_id = ds.event_id
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
 WHERE ds.resolved = false
   AND ds.generated_at > now() - make_interval(secs => ds.horizon_sec)
"""


async def _fetch_fresh_signals() -> list[dict[str, Any]]:
    rows = await fetch(_FETCH_FRESH_SIGNALS_SQL)
    return [dict(r) for r in rows]


async def _load_active_layer_a():
    row = await fetchrow("SELECT model_payload FROM ml_event_selector WHERE is_active = true")
    if row is None:
        return None
    payload = row["model_payload"]
    return deserialize_layer_a(payload if isinstance(payload, str) else json.dumps(payload))


async def _load_active_layer_b(mode: str = "binary"):
    """Load active Layer B model. Mode-aware: binary (legacy) or ev_regression (shadow A/B)."""
    row = await fetchrow(
        "SELECT model_payload, layer_a_threshold FROM ml_market_picker WHERE is_active = true AND mode = $1",
        mode,
    )
    if row is None:
        return None, LAYER_A_THRESHOLD_DEFAULT
    payload = row["model_payload"]
    threshold = float(row["layer_a_threshold"] or LAYER_A_THRESHOLD_DEFAULT)
    return deserialize_layer_b(payload if isinstance(payload, str) else json.dumps(payload)), threshold


def _build_layer_a_input(r: dict[str, Any]) -> EventSelectorExample:
    return EventSelectorExample(
        event_id=str(r["event_id"] or ""),
        category=r["category"],
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
        n_detector_signals=int(r["n_detector_signals_event"]),
        outcome=0,
    )


def _build_layer_b_input(r: dict[str, Any], calibrated_p: float, p_event: float) -> MarketPickerExample:
    return MarketPickerExample(
        detector_id=r["detector"],
        calibrated_p=calibrated_p,
        edge_pct=float(r["expected_edge_pct"]),
        entry_price=float(r["entry_price"]),
        horizon_sec=int(r["horizon_sec"]),
        category=r["category"],
        volume_24h=float(r["volume_24h"] or 0),
        is_v2_post=r["generated_at"] > V2_CUTOVER,
        p_event=p_event,
        archetype=r["archetype"] or "unclassified",
        n_markets_in_event=int(r["n_markets"] or 1),
        outcome=0,
    )


async def _emit_cal_anchor_shadow(
    *,
    detector: str,
    signal_id: str,
    market_id: Any,
    raw_p: float,
    row: dict[str, Any],
    cals: dict[str, Any],
    lgb_cals: dict[str, Any],
    settings: Any,
    flat_p: float,
    flat_source: str,
) -> None:
    """Shadow-калибровка per-anchor модели — НЕ торгуемый путь.

    Задача 1.1 (roadmap аудита): читает evidence.anchor_hours сигнала и, если
    detector входит в _PER_ANCHOR_DETECTORS, повторно вызывает calibrate() с
    anchor_hours для сравнения anchor_p vs боевого flat_p. Результат только
    логируется (cal_anchor_shadow) — calibrated_p/meta_p, идущие дальше по
    two-stage pipeline, этот вызов не трогает. Ошибка здесь не должна ломать
    основной путь.
    """
    if detector not in _PER_ANCHOR_DETECTORS:
        return
    evidence = parse_jsonb(row.get("evidence"))
    if not evidence:
        return
    anchor_hours = evidence.get("anchor_hours")
    if not anchor_hours:
        return
    try:
        anchor_hours_int = int(anchor_hours)
    except (TypeError, ValueError):
        return
    try:
        anchor_p, anchor_source = await _calibrate(
            raw_p, detector, cals, row=row, lgb_cals=lgb_cals, anchor_hours=anchor_hours_int, settings=settings
        )
        logger.info(
            "cal_anchor_shadow",
            detector=detector,
            signal_id=signal_id,
            market_id=market_id,
            anchor_hours=anchor_hours_int,
            flat_p=flat_p,
            flat_source=flat_source,
            anchor_p=anchor_p,
            anchor_source=anchor_source,
        )
    except Exception as exc:
        logger.warning(
            "cal_anchor_shadow_failed",
            detector=detector,
            signal_id=signal_id,
            market_id=market_id,
            error=str(exc),
        )


async def fetch_two_stage_calibrated_signals() -> list[dict[str, Any]]:
    """V2 inference path. Возвращает свежие detector_signals с meta_p, p_event, gate flag.

    Не модифицирует state. Cold-start safe (graceful degradation).
    """
    rows = await _fetch_fresh_signals()
    if not rows:
        return []

    cals = await _load_l1_calibrators()
    lgb_cals = await _load_lgb_calibrators()
    layer_a = await _load_active_layer_a()
    layer_b, layer_a_threshold = await _load_active_layer_b()
    settings = get_settings()

    # Step 1: Calibrate raw_p → calibrated_p (G18 bin → LGB → Beta → raw_p fallback chain).
    # PROD INCIDENT 2026-05-26 (KeyError 'g18_bin'): init счётчиков разошёлся с цепочкой —
    # теперь init строго от CAL_SOURCES из fallback_chain, рассинхрон невозможен.
    enriched: list[dict[str, Any]] = []
    cal_source_counts: dict[str, int] = dict.fromkeys(CAL_SOURCES, 0)
    for r in rows:
        raw_p = float(r["raw_p"])
        calibrated_p, cal_src = await _calibrate(
            raw_p, r["detector"], cals, row=r, lgb_cals=lgb_cals, settings=settings
        )
        cal_source_counts[cal_src] = cal_source_counts.get(cal_src, 0) + 1

        # Shadow-режим Задача 1.1: per-anchor калибровка логируется, но НЕ идёт
        # в calibrated_p/meta_p (торгуемое поведение не меняется).
        await _emit_cal_anchor_shadow(
            detector=r["detector"],
            signal_id=str(r["id"]),
            market_id=r["market_id"],
            raw_p=raw_p,
            row=r,
            cals=cals,
            lgb_cals=lgb_cals,
            settings=settings,
            flat_p=calibrated_p,
            flat_source=cal_src,
        )
        enriched.append(
            {
                "detector_signal_id": str(r["id"]),
                "detector": r["detector"],
                "market_id": r["market_id"],
                "event_id": r["event_id"],
                "side": r["side"],
                "raw_p": raw_p,
                "calibrated_p": calibrated_p,
                "cal_source": cal_src,
                "meta_p": calibrated_p,  # default; overwritten below
                "p_event": None,
                "passed_layer_a_gate": True,  # default true (no gate / cold-start)
                "entry_price": float(r["entry_price"]),
                # Fix-3 (latency-аудит №3): паритет с monolith-путём — поля для
                # sizing-телеметрии и TG-нотификации live-ордера. .get() —
                # старые моки собирают строки без этих полей.
                "current_price": float(r["current_price"]) if r.get("current_price") is not None else None,
                "yes_token_id": r.get("yes_token_id"),
                "fair_price": float(r["fair_price"]) if r.get("fair_price") is not None else None,
                "expected_edge_pct": float(r["expected_edge_pct"]),
                "horizon_sec": int(r["horizon_sec"]),
                "evidence": r["evidence"],
                "generated_at": r["generated_at"],
                "category": r["category"],
                "volume_24h": float(r["volume_24h"] or 0),
                "token_id": r["token_id"],
                "market_end_date": r.get("market_end_date"),
            }
        )

    # Step 2: Layer A predict per unique event
    if layer_a is not None:
        unique_events: dict[str, dict[str, Any]] = {}
        for r in rows:
            ev = str(r["event_id"] or "")
            if ev and ev not in unique_events:
                unique_events[ev] = r
        if unique_events:
            layer_a_inputs = [_build_layer_a_input(r) for r in unique_events.values()]
            try:
                preds = layer_a.predict(layer_a_inputs)
                p_event_by: dict[str, float] = dict(zip(unique_events.keys(), [float(p) for p in preds], strict=True))
            except Exception as exc:
                logger.warning("layer_a_predict_failed", error=str(exc))
                p_event_by = {}

            for sig, r in zip(enriched, rows, strict=True):
                ev = str(r["event_id"] or "")
                sig["p_event"] = p_event_by.get(ev)
                if sig["p_event"] is not None and sig["p_event"] < layer_a_threshold:
                    sig["passed_layer_a_gate"] = False
                    # gate failed: meta_p = calibrated_p (already set)

    # Step 3: Layer B predict only for signals that passed gate
    if layer_b is not None:
        passing = [
            (i, sig, r) for i, (sig, r) in enumerate(zip(enriched, rows, strict=True)) if sig["passed_layer_a_gate"]
        ]
        if passing:
            layer_b_inputs = [
                _build_layer_b_input(r, sig["calibrated_p"], sig["p_event"] or 1.0) for _, sig, r in passing
            ]
            try:
                preds = layer_b.predict(layer_b_inputs)
                for (_, sig, _), p in zip(passing, [float(p) for p in preds], strict=True):
                    sig["meta_p"] = p
            except Exception as exc:
                logger.warning("layer_b_predict_failed", error=str(exc), n=len(passing))

    logger.info(
        "two_stage_inference_done",
        n=len(enriched),
        l1_fit=bool(cals),
        lgb_fit=bool(lgb_cals),
        layer_a_fit=layer_a is not None,
        layer_b_fit=layer_b is not None,
        passed_gate=sum(1 for s in enriched if s["passed_layer_a_gate"]),
        cal_lgb=cal_source_counts["lgb"],
        cal_beta=cal_source_counts["beta"],
        cal_raw=cal_source_counts["raw"],
        cal_g18=cal_source_counts.get("g18_bin", 0),
    )
    return enriched


async def fetch_ev_regressor_signals() -> list[dict[str, Any]]:
    """EV-regressor inference path (Shadow A/B).

    Прогоняет свежие detector_signals через EV-regressor (mode='ev_regression').
    Возвращает [{detector_signal_id, ev_pred}, ...] — predicted net_roi (continuous,
    [-1, +5] после clip).

    Cold-start safe: если регрессор не fit'нут — возвращает пустой список.
    Логика отличается от binary path:
    - Использует ту же L1 калибрацию (calibrated_p как feature)
    - Использует ту же Layer A (p_event как feature)
    - Layer B заменён на EV-regressor (выход — float, не probability)
    - Не применяет gate threshold для inference (мы хотим predictions для всех
      signals чтобы потом сравнить с binary)
    """
    rows = await _fetch_fresh_signals()
    if not rows:
        return []

    ev_regressor, _ = await _load_active_layer_b(mode="ev_regression")
    if ev_regressor is None:
        return []

    cals = await _load_l1_calibrators()
    lgb_cals = await _load_lgb_calibrators()
    layer_a = await _load_active_layer_a()

    # Layer A predict (если активна) — иначе p_event=1.0 fallback
    p_event_by: dict[str, float] = {}
    if layer_a is not None:
        unique_events: dict[str, dict[str, Any]] = {}
        for r in rows:
            ev = str(r["event_id"] or "")
            if ev and ev not in unique_events:
                unique_events[ev] = r
        if unique_events:
            try:
                preds = layer_a.predict([_build_layer_a_input(r) for r in unique_events.values()])
                p_event_by = dict(zip(unique_events.keys(), [float(p) for p in preds], strict=True))
            except Exception as exc:
                logger.warning("ev_regressor_layer_a_failed", error=str(exc))

    # Build EV inputs (same shape as Layer B binary inputs)
    layer_b_inputs: list[MarketPickerExample] = []
    for r in rows:
        raw_p = float(r["raw_p"])
        # settings= сознательно не прокинут: shadow-путь не горячий, calibrate()
        # сам возьмёт get_settings() (lru_cache — тот же singleton).
        calibrated_p, _src = await _calibrate(raw_p, r["detector"], cals, row=r, lgb_cals=lgb_cals)
        p_event = p_event_by.get(str(r["event_id"] or ""), 1.0)
        layer_b_inputs.append(_build_layer_b_input(r, calibrated_p, p_event))

    try:
        ev_preds = ev_regressor.predict(layer_b_inputs)
    except Exception as exc:
        logger.warning("ev_regressor_predict_failed", error=str(exc), n=len(layer_b_inputs))
        return []

    out: list[dict[str, Any]] = []
    for r, pred in zip(rows, [float(p) for p in ev_preds], strict=True):
        out.append(
            {
                "detector_signal_id": str(r["id"]),
                "ev_pred": pred,
            }
        )

    logger.info("ev_regressor_inference_done", n=len(out))
    return out
