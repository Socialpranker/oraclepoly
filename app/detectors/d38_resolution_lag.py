"""D38 ResolutionLag — arb на лаге между фактическим исходом и resolved=true.

Гипотеза (A3 из prediction_markets_full_landscape.docx):
  Маркет с end_date < now() но resolved=false ещё не закрыт нашим cron
  resolve_outcomes (работает раз в 4ч). Рынок ВЦЕЛОМ уже знает исход — цена
  ушла к 0.85+ (YES near-fait) или 0.15- (NO near-fait). Покупка нужной
  стороны до момента resolved=true даёт +5..15% gross за окно часов-сутки.

Prod snapshot (2026-05-14):
  active_pending end_date < now() last 24h:   317
  edge zone (NO 0.05-0.20 OR YES 0.80-0.95):    25
  yes_zone (0.85-0.95):                          10
  no_zone  (0.05-0.15):                          13
  → ~23 кандидата в snapshot, оборот ~30-50 сигналов/неделю

Side выбирается ПО ЦЕНЕ (рынок уже движется в одну сторону), не по winner —
winner мы не знаем до резолюции. Ставим на сторону которую market уже
зацементировал.

Не торговать:
- end_date > now() (рынок ещё открыт, нет основания считать "уже произошло")
- now() - end_date > 24h ("stuck" маркеты — void/dispute, деньги залочатся)
- volume_24h < $500 (нет ликвидности для входа)
- current_price вне зон [0.05-0.15] (NO side) и [0.80-0.95] (YES side)
  - <0.05 или >0.95: уже всё, edge мизерный, slippage съест
  - 0.15-0.80: либо рынок не уверен (mid-zone) либо breakeven подкошен
    (0.15-0.20 NO bucket был WR 78% при breakeven 82% — см. tightening 2026-05-19)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import InvalidOperation
from typing import Any

import structlog

from app.detectors.contract import Signal
from app.detectors.token_resolver import no_token_id, yes_token_id

logger = structlog.get_logger()

DETECTOR_NAME = "D38_resolution_lag"

# Lag window: end_date должен быть в этом окне относительно now()
MAX_LAG_HOURS = 24.0
MIN_LAG_HOURS = 0.0  # включаем маркеты которые только что просрочились

# YES near-fait zone (buy YES, expect → 1.0)
YES_PRICE_FLOOR = 0.80
YES_PRICE_CEILING = 0.95

# NO near-fait zone (buy NO, expect → 1.0 on NO side)
# 2026-05-19 tightening: NO_PRICE_CEILING 0.20 → 0.15.
# Эмпирика на 30d resolved (out-of-sample WR 91.4% подтверждён):
#   yes_price 0.05-0.10 (n=52): WR 96.2%, EV +$3/trade   ✅
#   yes_price 0.10-0.15 (n=44): WR 100%,  EV +$14/trade  ✅✅
#   yes_price 0.15-0.20 (n=69): WR 78.3%, EV -$5.5/trade ❌ — отрезаем
# В bucket 0.15-0.20 NO entry ≈ 82c, payout 22%, breakeven WR = 82% > actual 78%.
# Tightening удаляет ~40% NO-сигналов но переводит остаток из -$5.5 в +$10 EV.
NO_PRICE_FLOOR = 0.05  # current_price (YES side) floor
NO_PRICE_CEILING = 0.15  # current_price (YES side) ceiling — tightened 2026-05-19 (was 0.20)

# Liquidity gate
MIN_VOLUME_24H_USD = 500.0

# Signal horizon: ожидаем resolved=true в ближайшие 6 часов (наш cron каждые 4ч)
HORIZON_SEC = 6 * 60 * 60

# Confidence — посередине между 1.0 (oracle уже всё знает) и 0.85 (UMA dispute / void
# фактор). 0.95 = в 95% случаев цена в near-fait зоне после end_date соответствует
# финальному исходу. Дискаунт 5pp на dispute / void / data lag.
CONFIDENCE = 0.95


def detect(market_event: dict[str, Any]) -> Signal | None:
    """Эмитит signal для маркета в resolution lag zone.

    Expected keys:
        market_id (str), condition_id (str), category (str),
        current_price (float, YES-side), end_date (datetime, tz-aware),
        volume_24h (float), clob_token_ids (list|str), event_id (str|None)

    Возвращает None если:
    - end_date вне окна (-24h, now)
    - current_price вне арб-зон
    - volume_24h < $500
    """
    market_id = market_event.get("market_id")
    if not market_id:
        return None

    end_date = market_event.get("end_date")
    if end_date is None:
        return None

    now = datetime.now(UTC)
    lag_seconds = (now - end_date).total_seconds()
    if lag_seconds <= 0:
        return None  # рынок ещё не закончился
    lag_hours = lag_seconds / 3600.0
    if lag_hours > MAX_LAG_HOURS:
        return None  # stuck market, не торгуем

    try:
        current_price = float(market_event.get("current_price") or 0)
        volume_24h = float(market_event.get("volume_24h") or 0)
    except (ValueError, TypeError, InvalidOperation):
        return None

    if volume_24h < MIN_VOLUME_24H_USD:
        return None

    if YES_PRICE_FLOOR <= current_price < YES_PRICE_CEILING:
        side = "YES"
        entry_price = current_price
        token_id = yes_token_id(market_event.get("clob_token_ids"))
        zone = "yes_near_fait"
    elif NO_PRICE_FLOOR <= current_price < NO_PRICE_CEILING:
        side = "NO"
        entry_price = round(1.0 - current_price, 4)
        token_id = no_token_id(market_event.get("clob_token_ids"))
        zone = "no_near_fait"
    else:
        return None

    if entry_price <= 0 or entry_price >= 1:
        return None

    payout = 1.0 / entry_price
    edge_pct = round((CONFIDENCE * payout - 1) * 100, 2)
    if edge_pct <= 0:
        return None

    delta = (edge_pct / 100.0) * entry_price
    fair = round(max(0.0, min(1.0, entry_price - delta)), 4)

    return Signal(
        detector=DETECTOR_NAME,
        market_id=str(market_id),
        side=side,
        generated_at=now,
        confidence=CONFIDENCE,
        expected_edge_pct=edge_pct,
        fair_price=fair,
        current_price=current_price,  # Contract: current_price = YES price (canonical); resolve computes side entry
        horizon_sec=HORIZON_SEC,
        token_id=token_id,
        event_id=market_event.get("event_id"),
        evidence={
            "category": market_event.get("category"),
            "zone": zone,
            "lag_hours": round(lag_hours, 2),
            "volume_24h": volume_24h,
            "raw_yes_price": current_price,
            "end_date": end_date.isoformat() if end_date else None,
            "tx_hash": market_event.get("tx_hash"),
        },
    )
