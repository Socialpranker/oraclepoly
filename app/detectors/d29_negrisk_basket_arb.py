"""D29 NegRisk Basket Arbitrage.

Гипотеза (validated on 1870 historical events, 821 days):
  В neg_risk events с >=3 active markets когда sum(yes_prices) > 1.05 →
  buy NO на ВСЕ markets event'а = guaranteed positive payout (single winner
  rule). 100% WR (mathematical), median +3.79%, mean +13.12% gross ROI.

Per category (median ROI):
  Other (Movies/Music): +9.9%   best edge
  Sports:               +4.7%
  Politics:             +3.2%
  Weather:              +2.8%
  Crypto:               +2.1%

Net after fees (1.25% × 2 sides avg) ≈ ROI - 2.5pp.

Threshold conservative: 1.05 → ~3,269 events backtested → 100% WR, net median +1.3%.
Conservative production: emit при sum>1.10 (1.882 events, net median +1.3% но больше edge).

IMPLEMENTATION NOTE:
  Это event-level detector — эмитирует ОДИН Signal на событие через
  специальный "BASKET" market_id. Signal.evidence содержит list всех
  составляющих markets и их NO entry prices. Execution layer должен
  fill каждый отдельно (или skipped if book depth insufficient).

  Signal.side = "NO" (basket sells YES = buys NO across all markets)
  Signal.market_id = primary market (lowest current_price market)
    для compatibility с existing execution pipeline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.detectors.contract import Signal

DETECTOR_NAME = "D29_negrisk_basket_arb"

# Minimum sum threshold (sum_yes > 1.05 для conservative arb)
MIN_SUM_THRESHOLD: float = 1.05

# Conservative threshold для emit (production)
EMIT_SUM_THRESHOLD: float = 1.10

# Minimum markets per event
MIN_MARKETS: int = 3

# Backtest empirical
EMPIRICAL_WR: float = 1.0  # mathematically guaranteed
EMPIRICAL_NET_ROI_PCT_MEDIAN: float = 1.3  # net at threshold 1.10

# Hold horizon — варьируется от 1 дня до 30, default 7 days
DEFAULT_HORIZON_SEC: int = 7 * 24 * 60 * 60


def detect(basket_event: dict[str, Any]) -> Signal | None:
    """Возвращает Signal если basket arb conditions met.

    Expected `basket_event` fields:
      event_id: str (Polymarket event_id)
      event_title: str
      markets: list[dict] с per-market {market_id, condition_id, yes_price,
               clob_token_ids, volume_24h, end_date, category}
    """
    markets = basket_event.get("markets") or []
    if len(markets) < MIN_MARKETS:
        return None

    sum_yes = sum(float(m.get("yes_price") or 0) for m in markets)
    if sum_yes <= EMIT_SUM_THRESHOLD:
        return None

    # Primary market = с самым низким yes_price (= highest NO entry price)
    primary = min(markets, key=lambda m: float(m.get("yes_price") or 1.0))
    primary_yes = float(primary.get("yes_price") or 0)
    if primary_yes <= 0 or primary_yes >= 1:
        return None

    # NO basket cost = sum(1 - yes_i)
    n = len(markets)
    basket_cost = n - sum_yes  # = sum(1 - yes_i)
    if basket_cost <= 0:
        return None

    # Expected guaranteed payout (если ровно 1 winner): n - 1
    # Profit per basket = (n - 1) - basket_cost = sum_yes - 1
    expected_payout = n - 1
    raw_profit = expected_payout - basket_cost  # = sum_yes - 1
    raw_roi_pct = raw_profit / basket_cost * 100.0

    # Edge для signal: net ROI per basket
    # fees ~ 2.5% drag
    net_roi_pct = raw_roi_pct - 2.5

    primary_no_entry = round(1.0 - primary_yes, 4)

    return Signal(
        detector=DETECTOR_NAME,
        market_id=str(primary["market_id"]),
        side="NO",
        generated_at=datetime.now(UTC),
        confidence=EMPIRICAL_WR,  # 1.0 — mathematical
        expected_edge_pct=round(net_roi_pct, 2),
        fair_price=round(primary_no_entry * (1.0 + raw_roi_pct / 100.0), 4),
        current_price=round(
            primary_yes, 4
        ),  # Contract: current_price = YES price (canonical); resolve computes NO entry
        horizon_sec=DEFAULT_HORIZON_SEC,
        token_id=_no_token_from_clob(primary.get("clob_token_ids")),
        event_id=basket_event.get("event_id"),
        evidence={
            "strategy": "negrisk_basket_arb",
            "event_title": basket_event.get("event_title"),
            "n_markets": n,
            "sum_yes_prices": round(sum_yes, 4),
            "basket_cost_usd_per_dollar": round(basket_cost, 4),
            "raw_roi_pct": round(raw_roi_pct, 2),
            "primary_market_id": primary["market_id"],
            "primary_yes_price": primary_yes,
            "basket_markets": [
                {
                    "market_id": m.get("market_id"),
                    "yes_price": float(m.get("yes_price") or 0),
                    "no_entry": round(1.0 - float(m.get("yes_price") or 0), 4),
                    "volume_24h": float(m.get("volume_24h") or 0),
                    "clob_token_ids": m.get("clob_token_ids"),
                }
                for m in markets
            ],
        },
    )


def _no_token_from_clob(clob_token_ids: Any) -> str | None:
    """Локальный helper — NO token = index 1."""
    from app.detectors.token_resolver import no_token_id

    return no_token_id(clob_token_ids)
