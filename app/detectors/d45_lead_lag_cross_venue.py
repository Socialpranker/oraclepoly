"""D45 LeadLagCrossVenue — Polymarket↔Kalshi lead-lag.

PDF §7.1 (strategy_10k_capital.pdf):
  Polymarket быстрее всех на crypto/political twitter события.
  Betfair быстрее на спорт.
  Kalshi медленнее на всём (institutional, ждут confirmation).
  Когда venue A двинулся первым — ставишь на convergence venue B.
  Hold 30 сек - 30 мин, exit на convergence либо time stop.
  Sharpe 2-4 при правильной настройке.

Этот detector — **Polymarket ↔ Kalshi pair only** (Betfair connector ещё нет).

Phase 1 = SHADOW ONLY. evidence.shadow_only=True. Никакого execution.

Гипотеза:
  Если Polymarket двинулся ≥3pp за 5-30 мин окно, а Kalshi двинулся <1pp за
  то же окно — Kalshi с высокой вероятностью догонит. Bet:
    - Polymarket leader UP, Kalshi laggar UP → buy YES on Kalshi (lag venue)
    - Polymarket leader DOWN, Kalshi laggar DOWN → buy NO on Kalshi
    - И симметрично если Kalshi leader, Polymarket laggar.

Но Kalshi credentials ещё не подняты → 0 paired data на проде. Detector
готов к моменту когда pipeline дотащит данные.

Не торгуем:
- inverse_side pair (Poly YES = Kalshi NO — Phase 2 swap)
- lookback_sec вне окна [300, 1800] (5-30 min)
- leader delta < 3pp ИЛИ lag delta > 1pp (не lead-lag, а оба двинулись)
- volume_24h < $1K на любой стороне
- Любая None price (orderbook empty)

См. research/cross-venue-lead-lag-2026-05/{report,implementation-plan}.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from app.detectors.contract import Signal
from app.detectors.token_resolver import no_token_id, yes_token_id

logger = structlog.get_logger()

DETECTOR_NAME = "D45b_lead_lag_cross_venue"

# Lookback window — должен быть достаточно свеж чтобы lag ещё держался
LOOKBACK_MIN_SEC = 300  # 5 min
LOOKBACK_MAX_SEC = 1800  # 30 min

# Threshold leader move (per percentage point of YES price)
# 3pp — достаточно чтобы перекрыть Kalshi taker fee (7¢ × p × (1-p) ≈ 1.7pp at p=0.5)
LEADER_MIN_DELTA_PP = 3.0

# Threshold lag — laggar должен почти не двинуться чтобы был edge.
# Eps tolerance чтобы 0.46 - 0.45 == 1.0pp (float-safe).
LAG_MAX_DELTA_PP = 1.0
_FLOAT_EPS = 1e-6

# Liquidity floors — bid-ask depth important для convergence execution
MIN_VOLUME_24H_USD = 1_000.0

# Hold horizon — PDF: 30 sec - 30 min, default 30 min upper bound
HORIZON_SEC = 30 * 60

# Confidence — lead-lag на whitelisted паре исторически 60-80% convergence
# (см. research/cross-venue-lead-lag-2026-05/report.md). 0.70 conservative.
CONFIDENCE = 0.70

# Fair value floor для edge calculation — наша оценка где laggar должен быть.
# Linear catch-up assumption: fair = leader_now (полная convergence).
# Edge = |fair − lag_now|.

# Whitelist categories по PDF §7.1 (cross-venue pairs that work):
# US politics, Fed/macro, sports когда Kalshi покрывает.
CATEGORY_WHITELIST = frozenset({"politics", "economics", "sports", "crypto", "macro"})


@dataclass(frozen=True)
class LeadLagPair:
    """Input для detect() — paired snapshots Polymarket + Kalshi.

    Поля _old = price ровно на (now - lookback_sec).
    Поля _now = текущий top-of-book ask (или mid если ask нет).
    """

    polymarket_id: str
    kalshi_ticker: str
    event_id: str | None
    category: str | None
    inverse_side: bool

    poly_yes_old: float | None
    poly_yes_now: float | None
    kalshi_yes_old: float | None
    kalshi_yes_now: float | None

    poly_volume_24h: float
    kalshi_volume_24h_usd: float

    lookback_sec: int
    snapshot_at: datetime

    poly_clob_token_ids: Any = None


def _is_allowed_category(category: str | None) -> bool:
    return (category or "").lower() in CATEGORY_WHITELIST


def detect(pair: LeadLagPair) -> Signal | None:
    """Emit signal если paired price diverge — laggar bet on convergence.

    Anchor market_id = laggar (Polymarket-side primary, наш execution venue).
    Если Kalshi laggar — anchor Polymarket id всё равно (для downstream).
    """
    if pair.inverse_side:
        return None
    if not _is_allowed_category(pair.category):
        return None
    if pair.lookback_sec < LOOKBACK_MIN_SEC or pair.lookback_sec > LOOKBACK_MAX_SEC:
        return None
    if pair.poly_volume_24h < MIN_VOLUME_24H_USD:
        return None
    if pair.kalshi_volume_24h_usd < MIN_VOLUME_24H_USD:
        return None

    prices = (pair.poly_yes_old, pair.poly_yes_now, pair.kalshi_yes_old, pair.kalshi_yes_now)
    if any(p is None for p in prices):
        return None
    for p in prices:
        if p <= 0.0 or p >= 1.0:
            return None

    # Deltas в percentage points (e.g. 0.45 → 0.60 = +15pp)
    poly_delta_pp = (pair.poly_yes_now - pair.poly_yes_old) * 100.0
    kalshi_delta_pp = (pair.kalshi_yes_now - pair.kalshi_yes_old) * 100.0

    # Determine leader/lagger by abs(delta)
    if abs(poly_delta_pp) > abs(kalshi_delta_pp):
        leader_venue = "polymarket"
        lag_venue = "kalshi"
        leader_delta = poly_delta_pp
        lag_delta = kalshi_delta_pp
        leader_now = pair.poly_yes_now
        lag_now = pair.kalshi_yes_now
    else:
        leader_venue = "kalshi"
        lag_venue = "polymarket"
        leader_delta = kalshi_delta_pp
        lag_delta = poly_delta_pp
        leader_now = pair.kalshi_yes_now
        lag_now = pair.poly_yes_now

    # Trigger: leader moved enough AND lag stayed put
    if abs(leader_delta) < LEADER_MIN_DELTA_PP - _FLOAT_EPS:
        return None
    if abs(lag_delta) > LAG_MAX_DELTA_PP + _FLOAT_EPS:
        return None
    if leader_delta * (1.0 if lag_delta >= 0 else -1.0) < 0:
        # Movements в противоположных направлениях — не lead-lag (mean reversion / divergence)
        # Edge case: if lag_delta == 0, sign() = 1, so leader_delta != 0 is fine
        if lag_delta != 0:
            return None

    # Side determination by leader direction
    if leader_delta > 0:
        side = "YES"
        token_id = yes_token_id(pair.poly_clob_token_ids)
    else:
        side = "NO"
        token_id = no_token_id(pair.poly_clob_token_ids)

    # Edge = expected convergence move (laggar к leader)
    # Conservative: assume 50% of leader move будет docked by laggar
    fair_lag_price = leader_now
    expected_move = abs(fair_lag_price - lag_now)
    edge_pct = round(expected_move * 100.0 * CONFIDENCE, 2)
    if edge_pct <= 0:
        return None

    # Always anchor market_id to Polymarket (наш primary execution venue)
    market_id = pair.polymarket_id

    return Signal(
        detector=DETECTOR_NAME,
        market_id=market_id,
        side=side,
        generated_at=pair.snapshot_at,
        confidence=CONFIDENCE,
        expected_edge_pct=edge_pct,
        fair_price=round(fair_lag_price, 4),
        current_price=round(lag_now, 4),
        horizon_sec=HORIZON_SEC,
        token_id=token_id,
        event_id=pair.event_id,
        evidence={
            "leader_venue": leader_venue,
            "lag_venue": lag_venue,
            "leader_delta_pp": round(leader_delta, 2),
            "lag_delta_pp": round(lag_delta, 2),
            "lookback_sec": pair.lookback_sec,
            "poly_yes_old": pair.poly_yes_old,
            "poly_yes_now": pair.poly_yes_now,
            "kalshi_yes_old": pair.kalshi_yes_old,
            "kalshi_yes_now": pair.kalshi_yes_now,
            "polymarket_id": pair.polymarket_id,
            "kalshi_ticker": pair.kalshi_ticker,
            "category": pair.category,
            "poly_volume_24h": pair.poly_volume_24h,
            "kalshi_volume_24h_usd": pair.kalshi_volume_24h_usd,
            "shadow_only": True,
        },
    )
