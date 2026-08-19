"""D36 OBI Pressure — Orderbook Imbalance signal в mid-zone markets.

Гипотеза (research/polymarket-orthogonal-strategies/report.md, 2026-03-26):
  Orderbook Imbalance (OBI) предсказывает направление цены.
  - OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth) ∈ [-1, +1]
  - Sign(OBI) = направление informed flow.
  - На liquid equity рынках R² ~65%, OFI generalized > 85%.
  - На prediction markets тонький orderbook → OBI ещё более информативен.
  - Polymarket Kyle's λ снизилось с 0.518 до 0.01 (рынок ликвиднее),
    но OBI всё ещё signal informed activity.

Фильтры:
  - |OBI| >= 0.25 (25% asymmetry — informed pressure)
  - spread <= 0.08 (8¢ max — не toxic withdrawal)
  - total_depth >= $5,000 (защита от манипуляции тонких рынков)
  - mid-zone price [0.30, 0.85] (user 2026-05-13 focus)
  - TTR 4-72h (не слишком быстрая resolution)

Side:
  - OBI > 0.25 → bid pressure → buy YES
  - OBI < -0.25 → ask pressure → SKIPPED (inverse edge на v2_conservative,
    см. _DETECTOR_OVERRIDES note в position_manager.py)

NO-side filter (2026-05-22):
  Virtual v2_conservative split: YES n=9 PnL +$210 / NO n=7 PnL -$1,305.
  При negative OBI рынок едет +15pp YES (против NO position) — fade
  не работает на book imbalance в эту сторону. До формального backtest
  с инверсией знака — emit только YES signals.

Note: OBI вычисляется на лету через REST CLOB book fetch (не cached).
Это **microstructure** signal — короткий горизонт (24h).

Sources [research/polymarket-orthogonal-strategies/report.md]:
  [1] EmergentMind: OBI R² ~65%, Generalized OFI >85% [HIGH]
  [2] Stoikov (2018). "The Micro-Price" — depth asymmetry [HIGH]
  [3] Polymarket Election Study (2026): Kyle's λ dynamics [HIGH]
  [4] Federal Reserve (2025): order flow amplification [HIGH]
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.detectors.contract import Signal
from app.detectors.token_resolver import yes_token_id

DETECTOR_NAME = "D36_obi_pressure"

# Filters
MIN_OBI_ABS: float = 0.25  # 25% asymmetry minimum
MAX_SPREAD: float = 0.08  # 8¢ max
MIN_TOTAL_DEPTH_USD: float = 5000.0  # защита от манипуляции

# Mid-zone (tightened 2026-05-20 based on virtual analysis n=114)
# Original 0.30-0.85: favorite-fade NO trades в yes∈[0.60, 0.85] = WR 0-3%, −$1,376 на 39 trades
# Sweet spot yes∈[0.30, 0.60]: WR 42-48%, +$927 (+2.2% ROI)
MIN_YES_PRICE: float = 0.30
MAX_YES_PRICE: float = 0.60

# TTR window
MIN_TTR_HOURS: float = 4.0
MAX_TTR_HOURS: float = 72.0

# Backtest evidence (приблизительная — based on OBI R² 0.65 + thin market premium)
# Conservative WR estimate: OBI signal correct в 58-62% случаев.
EMPIRICAL_WR: float = 0.60
HORIZON_SEC: int = 24 * 3600  # 24h


def compute_obi_metrics(book: dict[str, Any]) -> dict[str, float] | None:
    """Compute OBI, spread, total depth из CLOB book.

    Args:
        book: dict with 'bids' and 'asks' lists.
              Each entry: {'price': '0.55', 'size': '1000.0'}.

              **NOTE on Polymarket book ordering**:
              - bids: ascending price (`bids[0]` = lowest, `bids[-1]` = best bid)
              - asks: descending price (`asks[0]` = highest, `asks[-1]` = best ask)
              Discovered 2026-05-13 via /tmp/d36_book_raw.py diagnostic.

    Returns:
        dict with 'obi', 'spread', 'bid_depth', 'ask_depth', 'total_depth_usd'.
        None если book invalid.
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None

    # Top-of-book = max bid (last in ascending) + min ask (last in descending)
    try:
        bid_prices = [float(b["price"]) for b in bids]
        ask_prices = [float(a["price"]) for a in asks]
    except (ValueError, TypeError, KeyError, IndexError):
        return None

    best_bid = max(bid_prices)
    best_ask = min(ask_prices)

    if not (0.0 < best_bid < 1.0 and 0.0 < best_ask < 1.0):
        return None
    if best_bid >= best_ask:
        return None

    spread = best_ask - best_bid

    # Depth aggregation: только top-N levels рядом с best (avoid extreme placeholder orders).
    # Polymarket книга может содержать "wall" orders на 0.01 и 0.99 которые искажают OBI.
    # Top 5 closest-to-mid levels на каждой стороне:
    #   bids: max 5 highest (т.е. last 5 в ascending list)
    #   asks: max 5 lowest (т.е. last 5 в descending list)
    try:
        top_bids = sorted(bids, key=lambda b: float(b["price"]), reverse=True)[:5]
        top_asks = sorted(asks, key=lambda a: float(a["price"]))[:5]
        bid_depth_usd = sum(float(b["price"]) * float(b["size"]) for b in top_bids)
        ask_depth_usd = sum(float(a["price"]) * float(a["size"]) for a in top_asks)
    except (ValueError, TypeError, KeyError):
        return None

    total_depth = bid_depth_usd + ask_depth_usd
    if total_depth <= 0:
        return None

    obi = (bid_depth_usd - ask_depth_usd) / total_depth

    return {
        "obi": obi,
        "spread": spread,
        "bid_depth_usd": bid_depth_usd,
        "ask_depth_usd": ask_depth_usd,
        "total_depth_usd": total_depth,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": (best_bid + best_ask) / 2.0,
    }


def detect(market_event: dict[str, Any]) -> Signal | None:
    """Emit OBI pressure signal если все filters пройдены.

    Required fields in market_event:
      - market_id, category, event_id, clob_token_ids
      - current_price (yes), hours_to_resolve
      - book_metrics: dict from compute_obi_metrics (precomputed in scan job)
    """
    yes_price = market_event.get("current_price")
    if yes_price is None:
        return None
    yes_price = float(yes_price)

    if not (MIN_YES_PRICE <= yes_price <= MAX_YES_PRICE):
        return None

    ttr = float(market_event.get("hours_to_resolve") or 0)
    if not (MIN_TTR_HOURS <= ttr <= MAX_TTR_HOURS):
        return None

    book_metrics = market_event.get("book_metrics")
    if not book_metrics:
        return None

    obi = book_metrics["obi"]
    if abs(obi) < MIN_OBI_ABS:
        return None

    spread = book_metrics["spread"]
    if spread > MAX_SPREAD:
        return None

    total_depth = book_metrics["total_depth_usd"]
    if total_depth < MIN_TOTAL_DEPTH_USD:
        return None

    # Direction: positive OBI = bid pressure = buy YES.
    # NO-side filter (2026-05-22): при negative OBI virtual v2 показал inverse
    # edge (NO -$1,305 на n=7 vs YES +$210 на n=9). До backtest с инверсией —
    # skip negative OBI полностью. Это убирает 50% volume но прекращает
    # катастрофические losses.
    if obi <= 0:
        return None
    side = "YES"
    entry_price = yes_price
    if entry_price <= 0.0 or entry_price >= 1.0:
        return None

    # Edge estimate: OBI magnitude × WR confidence × adjustment for entry price.
    # Conservative: 60% WR × 1/entry_price gives expected payout multiplier.
    fair_p = round(EMPIRICAL_WR, 4)
    edge_pct = round((EMPIRICAL_WR / entry_price - 1.0) * 100.0, 2)
    # Stiff floor: refuse if edge < 5% (мелочь)
    if edge_pct < 5.0:
        return None

    token = yes_token_id(market_event.get("clob_token_ids"))

    return Signal(
        detector=DETECTOR_NAME,
        market_id=str(market_event["market_id"]),
        side=side,
        generated_at=datetime.now(UTC),
        confidence=EMPIRICAL_WR,
        expected_edge_pct=edge_pct,
        fair_price=fair_p,
        current_price=yes_price,  # Contract: current_price = YES price (canonical); resolve computes side entry
        horizon_sec=HORIZON_SEC,
        token_id=token,
        event_id=market_event.get("event_id"),
        evidence={
            "category": market_event.get("category"),
            "yes_price": yes_price,
            "obi": round(obi, 4),
            "spread": round(spread, 4),
            "bid_depth_usd": round(book_metrics["bid_depth_usd"], 2),
            "ask_depth_usd": round(book_metrics["ask_depth_usd"], 2),
            "total_depth_usd": round(total_depth, 2),
            "best_bid": round(book_metrics["best_bid"], 4),
            "best_ask": round(book_metrics["best_ask"], 4),
            "hours_to_resolve": ttr,
            "strategy": "obi_pressure",
        },
    )
