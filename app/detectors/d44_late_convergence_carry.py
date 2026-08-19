"""D44 LateConvergenceCarry — carry-on-near-resolution.

Гипотеза (research/late-convergence-carry-2026-05/report.md):
  Markets за 2h-7d до end_date с YES_price 0.96-0.98 (или NO equivalent
  0.02-0.04) часто резолвятся в сторону near-fait цены. UMA dispute rate
  в whitelisted категориях (crypto, economics) ~1.96%. Holding до
  resolve дает carry 2-4% gross за дни.

Не торгуем:
- end_date <= now() (D38 territory)
- DTE < 2h (overlap с D38) или DTE > 7d (capital lock too long)
- Категории вне crypto/economics (insider risk, subjective resolve).
  sports убран 2026-05 (autoresearch): 66% win, но −34% net на 18 сигналах
- Question matches blacklist (geopolitics keywords, subjective verbs)
- volume_24h < $10K или liquidity < $5K (slippage съест carry)
- current_price вне bands: [0.96, 0.98] (YES) либо [0.02, 0.04] (NO)
- Annualized return < 30% или > 200% (либо нет смысла vs T-bills, либо
  подозрительно — возможно ошибка данных)

Phase 1 = shadow only. Никаких ордеров — только сигналы в detector_signals
с evidence.shadow_only=True.

См. research/late-convergence-carry-2026-05/scanner_implementation_plan.md
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.detectors.contract import Signal
from app.detectors.token_resolver import no_token_id, yes_token_id

logger = structlog.get_logger()

DETECTOR_NAME = "D44_late_convergence_carry"

# DTE window — exclude D38 territory (0-24h после end_date) + капать слишком long carry
DTE_MIN_HOURS = 2.0
DTE_MAX_HOURS = 168.0  # 7 days

# Price bands (symmetric YES/NO)
YES_BAND_FLOOR = 0.96
YES_BAND_CEILING = 0.98
NO_BAND_FLOOR = 0.02  # current_price (YES side) floor для NO entry
NO_BAND_CEILING = 0.04

# Liquidity floors
MIN_VOLUME_24H_USD = 10_000.0
MIN_LIQUIDITY_USD = 5_000.0

# Annualized return gates.
# Floor — minimum economic edge vs T-bills + 200-400 bps risk premium.
# Ceiling — короткий DTE × tight entry в наших полосах ДАЁТ очень высокую
# annualized (entry 0.97 за 24h → ~1100%). Ceiling 30x (3000%) ловит только
# реальные data anomalies (price feed bug, malformed market) — не валидные
# сигналы.
MIN_ANNUALIZED_RETURN = 0.30  # 30%
MAX_ANNUALIZED_RETURN = 30.0  # 3000% — anomaly detector, не EV gate

# Fair-value floor — Phase 1 simple prior. Reflects уверенность что in-band
# late-convergence candidates имеют true_prob ≥ 0.99.
# При entry в band [0.96, 0.98] edge = 0.99 − entry = [0.01, 0.03] = 1-3%.
# Floor должен быть строго ВЫШЕ band ceiling — иначе edge=0 на верхней границе.
FAIR_VALUE_FLOOR = 0.99

# Confidence — UMA dispute rate 1.96% в whitelisted categories
CONFIDENCE = 0.96

HORIZON_SEC = 7 * 24 * 3600  # 7 days max — capped by DTE

CATEGORY_WHITELIST = frozenset({"crypto", "economics"})

BLACKLIST_KEYWORDS = frozenset(
    {
        # Diplomatic / geopolitical
        "ceasefire",
        "peace deal",
        "agreement",
        "speak",
        "meet",
        "minerals",
        "ukraine",
        "iran",
        "russia",
        "china",
        "israel",
        "north korea",
        "syria",
        "afghanistan",
        "venezuela",
        # Subjective resolution
        "credible reporting",
        "consensus",
        "discretion",
        # Personal action / subjective
        "wear",
        "appear",
        "tweet",
        "post",
        "remove",
        "fire",
        "resign",
        "concede",
        # Conspiracy / opaque source
        "ufo",
        "alien",
        "uap",
        "audit",
        "fort knox",
        # 15-minute crypto (separate fee tier)
        "15-minute",
        "15 minute",
        "5-minute",
        "5 minute",
    }
)


def _is_allowed(category: str | None, question: str | None) -> bool:
    cat = (category or "").lower()
    q = (question or "").lower()
    if any(kw in q for kw in BLACKLIST_KEYWORDS):
        return False
    return cat in CATEGORY_WHITELIST


def detect(market_event: dict[str, Any], now: datetime | None = None) -> Signal | None:
    """Emit signal для late-convergence carry candidate.

    Expected keys:
        market_id (str), condition_id (str|None), category (str),
        question (str), current_price (float, YES-side),
        end_date (datetime tz-aware), volume_24h (float),
        liquidity (float), clob_token_ids (list|str), event_id (str|None)

    Args:
        market_event: текущее состояние market
        now: tz-aware UTC. Если None — datetime.now(UTC). Param добавлен
            для backtest replay (look-ahead bias prevention).
    """
    market_id = market_event.get("market_id")
    if not market_id:
        return None

    end_date = market_event.get("end_date")
    if end_date is None:
        return None

    if now is None:
        now = datetime.now(UTC)
    dte_seconds = (end_date - now).total_seconds()
    if dte_seconds <= 0:
        return None  # D38 territory
    dte_hours = dte_seconds / 3600.0
    if dte_hours < DTE_MIN_HOURS or dte_hours > DTE_MAX_HOURS:
        return None

    if not _is_allowed(market_event.get("category"), market_event.get("question")):
        return None

    try:
        yes_price = float(market_event.get("current_price") or 0)
        volume_24h = float(market_event.get("volume_24h") or 0)
        liquidity = float(market_event.get("liquidity") or 0)
    except (ValueError, TypeError):
        return None

    if volume_24h < MIN_VOLUME_24H_USD or liquidity < MIN_LIQUIDITY_USD:
        return None

    if YES_BAND_FLOOR <= yes_price <= YES_BAND_CEILING:
        side = "YES"
        entry = yes_price
        token = yes_token_id(market_event.get("clob_token_ids"))
    elif NO_BAND_FLOOR <= yes_price <= NO_BAND_CEILING:
        side = "NO"
        entry = round(1.0 - yes_price, 4)
        token = no_token_id(market_event.get("clob_token_ids"))
    else:
        return None

    if entry <= 0 or entry >= 1:
        return None

    # Annualized return gate
    gross_pct = (1.0 - entry) / entry
    annualized = gross_pct * 365.0 * 24.0 / dte_hours
    if annualized < MIN_ANNUALIZED_RETURN or annualized > MAX_ANNUALIZED_RETURN:
        return None

    # Fair value — Phase 1 simple: уверены что in-band signals имеют
    # implied true_prob >= FAIR_VALUE_FLOOR. Edge = fair - entry.
    fair = max(yes_price if side == "YES" else 1.0 - yes_price, FAIR_VALUE_FLOOR)
    edge = fair - entry
    if edge < 0.01:
        return None
    edge_pct = round(edge * 100, 2)

    return Signal(
        detector=DETECTOR_NAME,
        market_id=str(market_id),
        side=side,
        generated_at=now,
        confidence=CONFIDENCE,
        expected_edge_pct=edge_pct,
        fair_price=round(fair, 4),
        current_price=yes_price,
        horizon_sec=min(HORIZON_SEC, int(dte_seconds)),
        token_id=token,
        event_id=market_event.get("event_id"),
        evidence={
            "category": market_event.get("category"),
            "question": (market_event.get("question") or "")[:120],
            "dte_hours": round(dte_hours, 2),
            "volume_24h": volume_24h,
            "liquidity": liquidity,
            "annualized_return_pct": round(annualized * 100, 1),
            "yes_price": yes_price,
            "shadow_only": True,
        },
    )
