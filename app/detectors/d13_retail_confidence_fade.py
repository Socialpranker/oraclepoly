"""D13 RetailConfidenceFade — fade NO на favorites когда takers только retail.

Гипотеза (canonical backtest 2026-05-03 — PR #417):
  В extreme zone YES@[0.85, 0.95) NO-fade имеет sustained edge на ALL anchors
  (4h/24h/72h) когда smart_money_taker_share < 5% (retail-only takers
  в pre-anchor 24h window). Smart money отсутствие → retail "chasing favorites"
  → easier fade.

  Per-anchor (canonical, 821 days, smart<0.05 в [0.85, 0.95)):

    anchor    n      WR      net_ROI
    --------------------------------
    4h      1214   16.3%    +9.9%   (MARGINAL)
    24h     1243   15.8%    +7.4%   (MARGINAL)
    72h     1021   16.4%   +12.0%   (PASS)

  Combined honest $/d @ $5K bankroll: $5.09 (~$1825/year).

  Per-category strong cells:
    - Politics +15-45% all anchors
    - Crypto +12-17% on 24h+72h
    - Excluding Other 24h: n=629, +23.7% net ROI

  Adversarial mirror PASS на all anchors (YES side -13%) — asymmetric edge.

Inverse signal (warning veto в D7b — separate):
  smart_money_taker_share > 0.5 → NO fade ROI -33%. Smart money buying →
  они знают что-то, не fade против них.

Не торговать:
- entry < 0.85 или entry >= 0.95 (D7b extended uses [0.85, 0.97], D13 узчее)
- horizon < 4h или > 72h
- smart_money_taker_share >= 0.05 (нужен чисто retail signal)
- n_takers_24h_before < 3 (минимум активности для статистики)
- Категория Culture (excluded)

Side: NO. Horizon signal: 4h.
Source: scripts/research/d13_retail_confidence_canonical.py canonical backtest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import InvalidOperation
from typing import Any

import structlog

from app.detectors.contract import Signal
from app.detectors.token_resolver import no_token_id

logger = structlog.get_logger()

DETECTOR_NAME = "D13_retail_confidence_fade"

MIN_HOURS_TO_RESOLVE = 4
MAX_HOURS_TO_RESOLVE = 72

YES_PRICE_FLOOR = 0.85
YES_PRICE_CEILING = 0.95

# Smart money filter — D13 emits ONLY если retail-only takers
SMART_MONEY_SHARE_CAP = 0.05

# Минимум takers для статистической значимости
MIN_TAKERS_24H = 3

HORIZON_SEC = 4 * 60 * 60

EXCLUDED_CATEGORIES: set[str] = {"Culture"}

# Per-anchor empirical WR for NO side (canonical 821-day backtest).
# anchor_band: '4h' = [4, 12], '24h' = [12, 36], '72h' = [36, 72]
BUCKET_WR: dict[str, float] = {
    "4h": 0.163,
    "24h": 0.158,
    "72h": 0.164,
}


def _anchor_band(hours_to_resolve: float) -> str | None:
    if hours_to_resolve < MIN_HOURS_TO_RESOLVE or hours_to_resolve > MAX_HOURS_TO_RESOLVE:
        return None
    if hours_to_resolve < 12:
        return "4h"
    if hours_to_resolve < 36:
        return "24h"
    return "72h"


def detect(market_event: dict[str, Any]) -> Signal | None:
    """Эмитит fade-NO сигнал когда retail-only takers в pre-anchor 24h.

    Вход: dict с keys:
        market_id (str), category (str), entry_price (float, YES-side),
        hours_to_resolve (float),
        smart_money_taker_share (float, 0..1) — share от taker volume
        n_takers_24h_before (int) — n distinct takers в last 24h
        Optional: tx_hash, source, event_id, question

    Возвращает None если:
    - market_id отсутствует
    - Категория excluded (Culture)
    - YES price вне [0.85, 0.95)
    - hours_to_resolve вне [4, 72]
    - smart_money_taker_share не указан или >= 0.05
    - n_takers_24h_before < 3
    """
    market_id = market_event.get("market_id")
    if not market_id:
        return None

    category = (market_event.get("category") or "").strip()
    if category in EXCLUDED_CATEGORIES:
        return None

    try:
        entry_yes = float(market_event.get("entry_price") or 0)
        hours_to_resolve = float(market_event.get("hours_to_resolve") or 0)
    except (ValueError, TypeError, InvalidOperation):
        return None

    if not (YES_PRICE_FLOOR <= entry_yes < YES_PRICE_CEILING):
        return None

    band = _anchor_band(hours_to_resolve)
    if band is None:
        return None

    # Wallet quality filter — REQUIRED для D13 emission
    raw_smart = market_event.get("smart_money_taker_share")
    raw_n_takers = market_event.get("n_takers_24h_before")
    if raw_smart is None or raw_n_takers is None:
        return None
    try:
        smart_share = float(raw_smart)
        n_takers = int(raw_n_takers)
    except (ValueError, TypeError, InvalidOperation):
        return None

    if n_takers < MIN_TAKERS_24H:
        return None
    if smart_share >= SMART_MONEY_SHARE_CAP:
        return None

    baseline_wr = BUCKET_WR.get(band)
    if baseline_wr is None:
        return None

    entry_no = round(1.0 - entry_yes, 4)
    if entry_no <= 0 or entry_no >= 1:
        return None

    payout = 1.0 / entry_no
    edge_pct = round((baseline_wr * payout - 1) * 100, 2)
    confidence = round(baseline_wr, 4)

    fair_no = round(max(0.0, min(1.0, baseline_wr)), 4)

    return Signal(
        detector=DETECTOR_NAME,
        market_id=str(market_id),
        side="NO",
        generated_at=datetime.now(UTC),
        confidence=confidence,
        expected_edge_pct=edge_pct,
        fair_price=fair_no,
        current_price=entry_yes,  # Contract: current_price = YES price (canonical); resolve computes NO entry as 1-YES
        horizon_sec=HORIZON_SEC,
        token_id=no_token_id(market_event.get("clob_token_ids")),
        event_id=market_event.get("event_id"),
        evidence={
            "category": category,
            "yes_price": entry_yes,
            "hours_to_resolve": hours_to_resolve,
            "anchor_band": band,
            "baseline_wr": baseline_wr,
            "smart_money_taker_share": smart_share,
            "n_takers_24h_before": n_takers,
            "tx_hash": market_event.get("tx_hash"),
        },
    )
