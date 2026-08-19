"""D40 KalshiCrossPlatform — arb detector между Polymarket и Kalshi.

Гипотеза (A1 из prediction_markets_full_landscape.docx):
  Цены одного и того же события на двух платформах систематически расходятся
  из-за разных user bases, regulatory framework (Polymarket UMA vs Kalshi CFTC),
  и информационных лагов. Покупка дешёвой стороны на одной + дорогой стороны
  на другой = арб с positive expected value.

Strategy (Phase 1):
  Two-leg arb на YES side: buy YES on cheaper venue + buy NO on more expensive venue.
  Если cost(YES_a) + cost(NO_b) < 1.0 — гарантированный profit при идеальном fill.

  С учётом fees: net_edge = 1 - (cost_yes_a × (1 + fee_a)) - (cost_no_b × (1 + fee_b))

  Min edge gate: D40_MIN_NET_SPREAD_PCT (default 3% из PRD decisions).

Inverse markets:
  Если matcher пометил pair как inverse_side=true (Poly YES = Kalshi NO),
  то "buy YES poly + buy NO kalshi" нет смысла — обе стороны одного исхода.
  Detector skip pairs с inverse_side=true в Phase 1 (Phase 2 будет swap nogi).

Side выбирается по edge direction:
  - direction='poly_yes' если poly_yes_ask + kalshi_no_ask < 1.0 (cheap YES on Poly)
  - direction='kalshi_yes' если kalshi_yes_ask + poly_no_ask < 1.0 (cheap YES on Kalshi)
  - возвращает только один лучший signal на пару (max edge)

Phase 1 MVP: только эмитит сигнал в detector_signals.evidence содержит обе ноги.
Никакой auto-execution. Operator решает поставить ручками через UI обеих платформ.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from app.detectors.contract import Signal
from app.services.fee_calculator import estimate_trading_fee

logger = structlog.get_logger()

DETECTOR_NAME = "D40_kalshi_cross_platform"

# Default из PRD decisions (3% net spread). Override via env D40_MIN_NET_SPREAD_PCT.
DEFAULT_MIN_NET_SPREAD_PCT = 3.0

# Min volume gates (обе платформы). Phase 1: $5K из PRD.
DEFAULT_MIN_VOLUME_USD = 5000.0

# Kalshi fee: 7¢ × C × (1-C) per contract = 0.07 × p × (1-p) as fraction of stake.
KALSHI_FEE_COEF = 0.07

# Signal horizon — двусторонний arb обычно закрывается за минуты-часы.
HORIZON_SEC = 60 * 60  # 1h


@dataclass(frozen=True)
class CrossPlatformPair:
    """Input для detect(): пара matched markets с обоих side."""

    polymarket_id: str
    kalshi_ticker: str
    event_id: str | None  # Polymarket event_id для downstream joins
    category: str  # unified cohort (Sports/Politics/Economics)
    polymarket_category_raw: str  # raw из markets.category для fee_calculator
    inverse_side: bool

    # Prices в float 0..1 (YES side, ask = что мы платим чтобы купить YES)
    poly_yes_ask: float | None
    poly_no_ask: float | None  # = 1 - poly_yes_bid примерно, или явный no ask
    kalshi_yes_ask: float | None
    kalshi_no_ask: float | None

    # Volumes
    poly_volume_24h: float
    kalshi_volume_24h_usd: float

    # Token IDs для downstream (Phase 2 execution)
    poly_clob_token_ids: Any = None  # JSONB array

    # Per-market fee schedule из Gamma (markets.fee_schedule). Точнее категорийной таблицы.
    polymarket_fee_schedule: dict | None = None


def _kalshi_fee_rate(price: float) -> float:
    """Kalshi taker fee as fraction (0..1). Formula: 0.07 × p × (1-p)."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return KALSHI_FEE_COEF * price * (1.0 - price)


def _leg_cost_with_fee(price: float, fee_rate: float) -> float:
    """Effective cost per $1 payout: price + fee × price."""
    return price * (1.0 + fee_rate)


def _calc_edge(
    cheap_side_price: float,
    cheap_side_fee_rate: float,
    expensive_side_price: float,
    expensive_side_fee_rate: float,
) -> float:
    """Net edge of two-leg arb (fraction of $1 payout).

    Buy YES @ cheap_side, buy NO @ expensive_side (cheaper of the two for NO).
    Payout = 1.0 (one side wins). Cost = cheap × (1+fee_a) + expensive × (1+fee_b).
    Edge = 1 - total_cost.
    """
    cost_a = _leg_cost_with_fee(cheap_side_price, cheap_side_fee_rate)
    cost_b = _leg_cost_with_fee(expensive_side_price, expensive_side_fee_rate)
    return 1.0 - cost_a - cost_b


def detect(
    pair: CrossPlatformPair,
    *,
    min_net_spread_pct: float = DEFAULT_MIN_NET_SPREAD_PCT,
    min_volume_usd: float = DEFAULT_MIN_VOLUME_USD,
    stake_usd: float = 100.0,  # для fee calculation на Polymarket (зависит от amount)
) -> Signal | None:
    """Emit signal если spread > threshold.

    Returns None when:
        - inverse pair (Phase 1 skip)
        - any price is None (orderbook empty)
        - volume below threshold either side
        - net edge < min_net_spread_pct
    """
    if pair.inverse_side:
        return None
    if pair.poly_yes_ask is None or pair.poly_no_ask is None:
        return None
    if pair.kalshi_yes_ask is None or pair.kalshi_no_ask is None:
        return None
    if pair.poly_volume_24h < min_volume_usd or pair.kalshi_volume_24h_usd < min_volume_usd:
        return None

    # Sanity: prices must be 0..1
    for p in (pair.poly_yes_ask, pair.poly_no_ask, pair.kalshi_yes_ask, pair.kalshi_no_ask):
        if p <= 0.0 or p >= 1.0:
            return None

    # Direction 1: cheap YES on Polymarket + cheap NO on Kalshi
    poly_yes_fee = (
        estimate_trading_fee(stake_usd, pair.poly_yes_ask, pair.polymarket_category_raw, pair.polymarket_fee_schedule)
        / (stake_usd * pair.poly_yes_ask)
        if pair.poly_yes_ask > 0
        else 0.0
    )
    kalshi_no_fee = _kalshi_fee_rate(pair.kalshi_no_ask)
    edge_dir1 = _calc_edge(pair.poly_yes_ask, poly_yes_fee, pair.kalshi_no_ask, kalshi_no_fee)

    # Direction 2: cheap YES on Kalshi + cheap NO on Polymarket
    poly_no_fee = (
        estimate_trading_fee(stake_usd, pair.poly_no_ask, pair.polymarket_category_raw, pair.polymarket_fee_schedule)
        / (stake_usd * pair.poly_no_ask)
        if pair.poly_no_ask > 0
        else 0.0
    )
    kalshi_yes_fee = _kalshi_fee_rate(pair.kalshi_yes_ask)
    edge_dir2 = _calc_edge(pair.kalshi_yes_ask, kalshi_yes_fee, pair.poly_no_ask, poly_no_fee)

    # Take the better direction
    if edge_dir1 >= edge_dir2:
        direction = "poly_yes"
        edge = edge_dir1
        cheap_side, cheap_price = "polymarket", pair.poly_yes_ask
        expensive_side, expensive_price = "kalshi", pair.kalshi_no_ask
        cheap_fee, expensive_fee = poly_yes_fee, kalshi_no_fee
    else:
        direction = "kalshi_yes"
        edge = edge_dir2
        cheap_side, cheap_price = "kalshi", pair.kalshi_yes_ask
        expensive_side, expensive_price = "polymarket", pair.poly_no_ask
        cheap_fee, expensive_fee = kalshi_yes_fee, poly_no_fee

    edge_pct = edge * 100.0
    if edge_pct < min_net_spread_pct:
        return None

    # Anchor market_id to Polymarket side — наш primary table.
    # Signal side всегда YES от perspective Polymarket side нашего markets table.
    poly_side = "YES" if direction == "poly_yes" else "NO"

    # Contract: current_price = YES price (canonical), resolve computes side entry.
    current_price = pair.poly_yes_ask
    fair_price = current_price - (edge * current_price)  # rough indicator
    fair_price = max(0.0, min(1.0, fair_price))

    return Signal(
        detector=DETECTOR_NAME,
        market_id=pair.polymarket_id,
        side=poly_side,
        generated_at=datetime.now(UTC),
        confidence=0.95,  # arb имеет высокую confidence если spread существует
        expected_edge_pct=round(edge_pct, 2),
        fair_price=round(fair_price, 4),
        current_price=round(current_price, 4),
        horizon_sec=HORIZON_SEC,
        token_id=None,  # Phase 1 не делает execution
        event_id=pair.event_id,
        evidence={
            "kalshi_ticker": pair.kalshi_ticker,
            "category": pair.category,
            "direction": direction,
            "cheap_side": cheap_side,
            "cheap_price": round(cheap_price, 4),
            "cheap_fee_rate": round(cheap_fee, 5),
            "expensive_side": expensive_side,
            "expensive_price": round(expensive_price, 4),
            "expensive_fee_rate": round(expensive_fee, 5),
            "net_edge_pct": round(edge_pct, 2),
            "poly_yes_ask": pair.poly_yes_ask,
            "poly_no_ask": pair.poly_no_ask,
            "kalshi_yes_ask": pair.kalshi_yes_ask,
            "kalshi_no_ask": pair.kalshi_no_ask,
            "poly_volume_24h": pair.poly_volume_24h,
            "kalshi_volume_24h_usd": pair.kalshi_volume_24h_usd,
        },
    )
