"""Detector layer for self-trading bot.

Event-driven сигналы из real-time market data.
Параллельные детекторы → unified Signal contract → strategy engine.

Architecture:
    Polymarket WebSocket → Event Bus → Detectors → Signal Queue → Strategies

Active detectors (canonical RA verified, 2026-05-03):
    D3 CrossMktArb       — арб в neg-risk events когда sum(YES) > 1.20 (canonical $4.35/day @ $5K)
    D7b NoFavoriteFade   — fade NO@[0.85,0.97] 4-72h до резолва (canonical $32.82/day @ $5K)
    D13 RetailConfidenceFade — fade NO@[0.85,0.95] когда smart_money_taker_share<5% (canonical $5.09/day @ $5K)
    D14 CryptoWinnersDistributeNO — fade NO в Crypto mid zone [0.50,0.70) на 4h когда winning takers selling (canonical $4.84/day @ $5K)

Killed detectors (canonical RA FAIL, 2026-05-03):
    D1 FadeWhale, D4 SmallMarketFade, D6/D6b RetailBullishOvershoot — gross edge
    не воспроизводится на 821-day HF parquet после fees+slippage.
    См. docs/backtests/D{1,4,6,6b}_*.md (kept as historical record).
"""

from app.detectors import (
    d3_cross_mkt_arb,
    d13_retail_confidence_fade,
    d14_crypto_winners_distribute_no,
)
from app.detectors.contract import Signal

__all__ = [
    "Signal",
    "d3_cross_mkt_arb",
    "d13_retail_confidence_fade",
    "d14_crypto_winners_distribute_no",
]
