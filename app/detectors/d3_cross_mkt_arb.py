"""D3 CrossMktArb — арб в neg-risk events когда sum(YES_prices) > 1.20.

Backtest H3 (TRUE neg-risk: neg_risk=true, neg_risk_other=false, n_yes_winners=1):
    bucket sum > 1.20:  ROI +41.17%, n=229   ← clear edge
    остальные buckets:  ~0%

Идея: в TRUE neg-risk событии ровно один YES может выиграть, поэтому
sum(YES_prices) должна стремиться к 1.0. Если sum > 1.20 — рынок
перегрет, продаём (≈ покупаем NO) самый дорогой YES.

Не торговать если:
- event не neg-risk
- есть catch-all market (neg_risk_other=true)
- n_yes_winners != 1 (разрешено больше одного победителя — sum может быть >1)
- sum(YES) <= 1.20 (по бэктесту edge только в этом ведре)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from app.detectors.contract import Signal

logger = structlog.get_logger()

DETECTOR_NAME = "D3_cross_mkt_arb"

SUM_TRIGGER = 1.20
HORIZON_SEC = 6 * 3600
EDGE_PCT = 41.17


@dataclass(frozen=True)
class EventMarket:
    """Один market внутри neg-risk event."""

    market_id: str
    yes_price: float
    token_id: str | None = None


def detect(
    event_id: str,
    markets: Sequence[EventMarket],
    *,
    neg_risk: bool,
    neg_risk_other: bool,
    n_yes_winners: int,
) -> list[Signal]:
    """Эмитит NO-сигналы по самым перегретым YES-маркетам в neg-risk event.

    Возвращает пустой список если event не подходит под фильтры или sum<=1.20.
    Иначе — один сигнал на самый дорогой YES-market (max yes_price).
    """
    if not (neg_risk and not neg_risk_other and n_yes_winners == 1):
        return []
    if len(markets) < 2:
        return []

    yes_sum = sum(m.yes_price for m in markets)
    if yes_sum <= SUM_TRIGGER:
        return []

    target = max(markets, key=lambda m: m.yes_price)
    # 2026-05-20: tightened to target.yes_price ∈ [0.40, 0.80] based on virtual n=47.
    # Edge только в этом окне:
    # target_yes < 0.40 (large basket noise) → WR 14-20%, -$1,890 на 12 trades
    # target_yes > 0.80 (deep favorite) → favorite побеждает, NO crash, WR 20%, -$775 на 5
    # Sweet spot [0.40, 0.80]: n=35, WR 57%, +$2,563 (+21.3% ROI)
    if target.yes_price < 0.40 or target.yes_price > 0.80:
        return []

    overhang = yes_sum - 1.0
    no_current = 1.0 - target.yes_price
    no_fair = max(0.02, min(0.98, no_current + overhang * (target.yes_price / yes_sum)))

    confidence = round(min(0.95, 0.55 + (yes_sum - SUM_TRIGGER) * 1.5), 4)

    return [
        Signal(
            detector=DETECTOR_NAME,
            market_id=target.market_id,
            side="NO",
            generated_at=datetime.now(UTC),
            confidence=confidence,
            expected_edge_pct=EDGE_PCT,
            fair_price=round(no_fair, 4),
            current_price=round(target.yes_price, 4),  # Contract: current_price = YES price (canonical)
            horizon_sec=HORIZON_SEC,
            event_id=event_id,
            token_id=target.token_id,
            evidence={
                "yes_sum": round(yes_sum, 4),
                "n_markets": len(markets),
                "target_yes_price": round(target.yes_price, 4),
                "overhang": round(overhang, 4),
            },
        )
    ]
