"""D42 Event-driven ML — ML predicts short-term price movement.

STATUS: SHADOW (с 2026-06-11). Scan job `app/jobs/d42_scan.py` крутится :17,:47
(crontab), грузит 3 per-window модели из /mnt/data/canonical/models/, читает
live_ml_features, эмитит NO-сигналы в detector_signals (source='live_v2').
Fit: scripts/research/d42_event_driven_ml_fit.py. Модели замёрзли 14 мая
(canonical stale, re-fit отменён) — но edge структурный, не модельный (см. ниже).

Гипотеза D3 (event-driven ML) из prediction_markets_full_landscape.docx:
  Не предсказываем outcome, а **изменение цены за N часов**.
  LGBM Regressor предсказывает `delta = yes_price(at=target_anchor) − yes_price(at=current_anchor)`.

  Models trained per-window (anchor_from → anchor_to):
    168→24h: 144h forward prediction (long horizon)
     72→24h:  48h forward
     24→ 4h:  20h forward (short horizon)

Signal emission (predict-time) — NO-only с 2026-06-21:
  - predicted_delta < -THRESHOLD → NO (price will fall → ride down)
  - predicted_delta > +THRESHOLD → no signal (YES-плечо отрезано, см. ниже)
  - |delta| < THRESHOLD → no signal

THRESHOLD = 5pp (0.05) — soft floor, calibrator decides actual edge.

NO-only rationale (memory detector_triage_2026-06-20): на live YES-плечо системно
убыточно (WR 23%, sum_roi −1512 / 124 сигналов), весь edge в NO (WR 53%, +1896;
окно 168_to_24 Wilson lower 45.6%, median +37%). corr(predicted_delta, realized)≈0
→ модель = триггер |delta|≥5pp, не направление. Окна 168/72/24 все сохранены.

Source: D3 hypothesis class, docx.
Fit script: scripts/research/d42_event_driven_ml_fit.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import InvalidOperation
from typing import Any

import structlog

from app.detectors.contract import Signal
from app.detectors.token_resolver import no_token_id

logger = structlog.get_logger()

DETECTOR_NAME = "D42_event_driven_ml"

# Min predicted delta для emit (sub-threshold predictions = no signal)
MIN_PREDICTED_DELTA_PP = 0.05  # 5pp

# Mid-zone yes — на extremes momentum часто exhaustion (как у D41)
MIN_YES_PRICE = 0.15
MAX_YES_PRICE = 0.85

# Empirical placeholder — calibrator scale based on backfill validation
EMPIRICAL_WR = 0.55

HORIZON_SEC = 24 * 60 * 60  # 24h re-emit window


def detect(market_event: dict[str, Any]) -> Signal | None:
    """Emit ride-prediction сигнал из ML predicted_delta.

    Required fields:
        market_id (str), category (str),
        current_price (float, YES-side, at anchor_from),
        hours_to_resolve (float, current ttr),
        predicted_delta_pp (float) — ML output: signed Δ price prediction
        window_name (str) — e.g. "168_to_24" (for evidence/ML versioning)
        Optional: event_id, clob_token_ids, tx_hash, model_version
    """
    market_id = market_event.get("market_id")
    if not market_id:
        return None

    try:
        yes_price = float(market_event.get("current_price") or 0)
        delta_pp = float(market_event.get("predicted_delta_pp") or 0)
    except (ValueError, TypeError, InvalidOperation):
        return None

    if not (MIN_YES_PRICE <= yes_price <= MAX_YES_PRICE):
        return None

    if abs(delta_pp) < MIN_PREDICTED_DELTA_PP:
        return None

    # NO-only с 2026-06-21: на live YES-плечо системно убыточно (WR 23%, sum_roi
    # −1512 на 124 сигналах), весь edge в NO-плече (WR 53%, sum_roi +1896, окно
    # 168_to_24 Wilson lower 45.6%). corr(predicted_delta, realized_roi)≈0 — модель
    # служит триггером |delta|≥5pp, направление берётся из структуры (fade short-term
    # drift). См. memory detector_triage_2026-06-20. YES-предсказания (price will
    # rise) больше не эмитятся.
    if delta_pp > 0:
        return None

    # Price will fall → buy NO (entry = 1 - yes_price)
    side = "NO"
    entry = round(1.0 - yes_price, 4)
    token = no_token_id(market_event.get("clob_token_ids"))
    # NO entry = 1 - yes_now; predicted yes_target = yes_now + delta_pp (negative)
    # NO target = 1 - (yes_now + delta_pp) = entry - delta_pp = entry + |delta|
    fair_target = min(0.99, entry + abs(delta_pp))
    fair = round(fair_target, 4)

    if not (0.0 < entry < 1.0):
        return None

    payout = 1.0 / entry
    edge_pct = round((EMPIRICAL_WR * payout - 1.0) * 100.0, 2)

    return Signal(
        detector=DETECTOR_NAME,
        market_id=str(market_id),
        side=side,
        generated_at=datetime.now(UTC),
        confidence=EMPIRICAL_WR,
        expected_edge_pct=edge_pct,
        fair_price=fair,
        current_price=yes_price,  # Contract: current_price = YES price (canonical); resolve computes side entry
        horizon_sec=HORIZON_SEC,
        token_id=token,
        event_id=market_event.get("event_id"),
        evidence={
            "category": market_event.get("category"),
            "predicted_delta_pp": round(delta_pp, 4),
            "window_name": market_event.get("window_name"),
            "model_version": market_event.get("model_version"),
            "yes_price_at_anchor": yes_price,
            "hours_to_resolve": market_event.get("hours_to_resolve"),
            "tx_hash": market_event.get("tx_hash"),
        },
    )
