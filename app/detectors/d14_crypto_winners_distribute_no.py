"""D14 CryptoWinnersDistributeNO — fade NO в Crypto mid zone когда winners distribute.

Гипотеза (canonical backtest 2026-05-03):
  В Crypto markets YES@[0.50, 0.70) NO-side имеет sustained edge на 4h anchor когда:
    - winning takers в market (`taker_pnl_weighted_usd_24h > 0`)
    - track record positive (`taker_roi_weighted_24h > 10%`)
    - selling pressure dominant (`buy_imbalance_asof < 0.45`)

  Logic: takers с positive PnL track record selling YES при mid prices →
  distribute / take profit → market drift down → NO win.

  Canonical numbers (821-day backtest, ml_features_v2):
    n=2,291, WR 60.2%, net_ROI +13.9%
    Sub-zones: 99% сигналов в [0.50, 0.60)
    Tighter buy_imb<0.30: WR 63.7%, ROI +20.5%
    Tighter taker_roi>50: WR 69.5%, ROI +30.5%
    Year split: 2025 +18%, 2026 +7.6% (sustained но weakening, post-V2 risk)

  Adversarial mirror: YES side -26.8% (strong asymmetric edge)
  Daily honest @ $5K: $4.84 (~$1,767/year)

Trigger window: [4, 72]h (full band, opened 2026-05-28 — 4h-only band пуст на
текущем Polymarket; см. detect() и docstring scan-джоба). canonical 2026-05-03
валидировал только 4h; 12-72h НЕ re-validated (backtest заблокирован отсутствием
wallet-PnL фич). Detector в shadow/virtual — demote по net_pnl страхует.

Не торговать:
- Категория != Crypto (per-category breakdown showed only Crypto works)
- entry < 0.50 или entry >= 0.70 (mid zone only)
- horizon < 4h или > 72h (out of trigger window)
- buy_imbalance_asof >= 0.45 (требуется selling pressure)
- taker_pnl_weighted_usd_24h <= 0 (no winning takers in market)
- taker_roi_weighted_24h <= 10 (track record too weak)
- n_takers < 3 (statistical significance)

Side: NO. Horizon signal: 4h.
Source: scripts/research/d14_crypto_winners_4h_canonical.py canonical backtest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import InvalidOperation
from typing import Any

import structlog

from app.detectors.contract import Signal
from app.detectors.token_resolver import no_token_id

logger = structlog.get_logger()

DETECTOR_NAME = "D14_crypto_winners_distribute_no"

MIN_HOURS_TO_RESOLVE = 4
MAX_HOURS_TO_RESOLVE = 72  # full trigger window [4,72]h (4h-only band пуст на текущем Polymarket, opened 2026-05-28)

YES_PRICE_FLOOR = 0.50
YES_PRICE_CEILING = 0.70

# Wallet quality filters
BUY_IMBALANCE_CEILING = 0.45  # selling pressure required
MIN_TAKER_ROI_PCT = 10.0  # winning takers track record
MIN_TAKERS_24H = 3

# Category filter — Crypto-only (per-category canonical breakdown)
ALLOWED_CATEGORIES: set[str] = {"Crypto"}

HORIZON_SEC = 4 * 60 * 60

# Empirical WR (canonical 821-day, anchor 4h, all filters applied)
BASELINE_WR_NO = 0.602


def detect(market_event: dict[str, Any]) -> Signal | None:
    """Эмитит fade-NO сигнал в Crypto mid zone когда winners distribute.

    Required keys в market_event:
        market_id (str), category (str), entry_price (float, YES-side),
        hours_to_resolve (float),
        smart_money_taker_share (unused в D14 but expected),
        taker_pnl_weighted_usd_24h (float, $),
        taker_roi_weighted_24h (float, %),
        buy_imbalance_asof (float, 0..1),
        n_takers_24h_before (int)

    Возвращает None если:
    - market_id отсутствует
    - Категория не Crypto
    - YES price вне [0.50, 0.70)
    - hours_to_resolve < 4 или > 72 (вне trigger window)
    - buy_imbalance_asof >= 0.45
    - taker_pnl_weighted_usd_24h <= 0
    - taker_roi_weighted_24h <= 10
    - n_takers_24h_before < 3
    """
    market_id = market_event.get("market_id")
    if not market_id:
        return None

    category = (market_event.get("category") or "").strip()
    if category not in ALLOWED_CATEGORIES:
        return None

    try:
        entry_yes = float(market_event.get("entry_price") or 0)
        hours_to_resolve = float(market_event.get("hours_to_resolve") or 0)
    except (ValueError, TypeError, InvalidOperation):
        return None

    if not (YES_PRICE_FLOOR <= entry_yes < YES_PRICE_CEILING):
        return None

    # Trigger window [4, 72]h (full band, opened 2026-05-28).
    # Background: canonical 2026-05-03 found 24h/72h FAIL vs 4h-only. Но 4h-band
    # ([4,12)h) на текущем Polymarket структурно пуст — Crypto-рынки резолвятся
    # либо <4h ("Up/Down" daily, кратно часам), либо >24h ("price above X on date",
    # 16:00 UTC). Окно расширено до 72h чтобы детектор снова получал кандидатов.
    # РИСК: 12-72h не валидирован на canonical (wallet-PnL фичи backtest недоступны,
    # см. detector_adapter.py:199-200). Detector в shadow/virtual — demote по net_pnl
    # при N>=30 поймает убыток если broader band не работает.
    if hours_to_resolve < MIN_HOURS_TO_RESOLVE or hours_to_resolve > MAX_HOURS_TO_RESOLVE:
        return None

    # Wallet quality filters — REQUIRED для D14
    raw_buy_imb = market_event.get("buy_imbalance_asof")
    raw_pnl = market_event.get("taker_pnl_weighted_usd_24h")
    raw_roi = market_event.get("taker_roi_weighted_24h")
    raw_n_takers = market_event.get("n_takers_24h_before")

    if raw_buy_imb is None or raw_pnl is None or raw_roi is None or raw_n_takers is None:
        return None

    try:
        buy_imbalance = float(raw_buy_imb)
        taker_pnl = float(raw_pnl)
        taker_roi = float(raw_roi)
        n_takers = int(raw_n_takers)
    except (ValueError, TypeError, InvalidOperation):
        return None

    if n_takers < MIN_TAKERS_24H:
        return None
    if buy_imbalance >= BUY_IMBALANCE_CEILING:
        return None
    if taker_pnl <= 0:
        return None
    if taker_roi <= MIN_TAKER_ROI_PCT:
        return None

    entry_no = round(1.0 - entry_yes, 4)
    if entry_no <= 0 or entry_no >= 1:
        return None

    payout = 1.0 / entry_no
    edge_pct = round((BASELINE_WR_NO * payout - 1) * 100, 2)
    confidence = round(BASELINE_WR_NO, 4)
    fair_no = round(max(0.0, min(1.0, BASELINE_WR_NO)), 4)

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
            "baseline_wr": BASELINE_WR_NO,
            "buy_imbalance_asof": buy_imbalance,
            "taker_pnl_weighted_usd_24h": taker_pnl,
            "taker_roi_weighted_24h": taker_roi,
            "n_takers_24h_before": n_takers,
            "tx_hash": market_event.get("tx_hash"),
        },
    )
