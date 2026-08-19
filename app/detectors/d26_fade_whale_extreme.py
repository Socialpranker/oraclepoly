"""D26 Fade-Whale Extreme detector.

Гипотеза (validated на canonical 821 days, scripts/research/backtest_d26_refined.py):
  Когда wallet из FADE_WHALE cohort (10,115 wallets с high impact + bad lifetime ROI)
  делает UP-push >=$3K в extreme_low zone (yes_price < 0.15) → price reverses 4h.

Backtest segmentation:
  Filter: fade_whale, push>=$3K, UP-direction, yes_price<0.15, hold 4h
  Result: n=394, WR 72.3%, raw +18.4%, net ROI +16.4% после 2pp costs

Cohort source: Postgres `wallet_alchemy_classes` table (synced from canonical).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.detectors.contract import Signal
from app.detectors.token_resolver import no_token_id

DETECTOR_NAME = "D26_fade_whale_extreme"

# Empirical edge per backtest (refined segmentation 2026-05-13)
EMPIRICAL_WR: float = 0.723
EMPIRICAL_NET_ROI_PCT: float = 16.4

HORIZON_SEC = 4 * 60 * 60  # 4h hold

MIN_PUSH_USD: float = 3_000.0
MIN_VOLUME_24H_USD: float = 5_000.0
EXTREME_LOW_THRESHOLD: float = 0.15  # only fade extreme YES underdog rally


def detect(market_event: dict[str, Any]) -> Signal | None:
    """Возвращает Signal если market matches D26 conditions.

    Expected `market_event` fields:
      market_id, condition_id, event_id, category, question,
      current_price, volume_24h, hours_to_resolve, clob_token_ids,
      fade_whale_push_usd_15min, fade_whale_drift_pp_15min
    """
    current_price = market_event.get("current_price")
    if current_price is None:
        return None
    current_price = float(current_price)

    # Zone gate: extreme_low only (fade YES rally при низкой YES price)
    if current_price >= EXTREME_LOW_THRESHOLD:
        return None

    vol_24h = float(market_event.get("volume_24h") or 0)
    if vol_24h < MIN_VOLUME_24H_USD:
        return None

    push_usd = float(market_event.get("fade_whale_push_usd_15min") or 0)
    drift_pp = float(market_event.get("fade_whale_drift_pp_15min") or 0)

    # Need UP-push (positive drift) и big enough
    if push_usd < MIN_PUSH_USD or drift_pp <= 0:
        return None

    # Buy NO @ (1 - yes_price). Например YES=0.10 → NO=0.90.
    entry_no = round(1.0 - current_price, 4)
    fair_no = round(min(0.99, entry_no * (1.0 + EMPIRICAL_NET_ROI_PCT / 100.0)), 4)
    edge_pct = round((EMPIRICAL_WR * (1.0 / entry_no) - 1.0) * 100.0, 2)

    return Signal(
        detector=DETECTOR_NAME,
        market_id=str(market_event["market_id"]),
        side="NO",
        generated_at=datetime.now(UTC),
        confidence=EMPIRICAL_WR,
        expected_edge_pct=edge_pct,
        fair_price=fair_no,
        current_price=current_price,  # Contract: current_price = YES price (canonical); resolve computes NO entry as 1-YES
        horizon_sec=HORIZON_SEC,
        token_id=no_token_id(market_event.get("clob_token_ids")),
        event_id=market_event.get("event_id"),
        evidence={
            "category": market_event.get("category"),
            "yes_price": current_price,
            "no_entry_price": entry_no,
            "fade_whale_push_usd_15min": push_usd,
            "fade_whale_drift_pp_15min": drift_pp,
            "vol_24h": vol_24h,
            "hours_to_resolve": market_event.get("hours_to_resolve"),
        },
    )
