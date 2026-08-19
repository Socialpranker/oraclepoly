"""M2 liquidity vacuum — measurement instrument (НЕ trading-детектор, shadow only).

Гипотеза: одностороннее опустошение глубины стакана (liquidity vacuum)
предсказывает forward-движение цены. asks вымели → buy-pressure → YES↑;
bids вымели → sell-pressure → YES↓.

M2 — дешёвый ТЕРМОМЕТР: confidence/edge — заглушки, исполнения нет.
Истинная метрика = forward-Δ цены T+30/60м (scripts/research/m2_forward_analysis.py),
НЕ winner hold-to-resolution. current_price = midpoint в момент детекции t0.

Детекция realtime в WSS (вакуум живёт <60s) — см. app/detectors/m2_vacuum_hook.py.
detect() чистая (без I/O) — тестируема юнитами.

Спек: docs/superpowers/specs/2026-06-21-m2-liquidity-vacuum-design.md
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.detectors.contract import Signal
from app.detectors.token_resolver import yes_token_id

DETECTOR_NAME = "M2_liquidity_vacuum"

# Mid-зона: вне краёв, где спред/ликвидность вырождены.
MIN_YES_PRICE: float = 0.10
MAX_YES_PRICE: float = 0.90

# Depletion: ratio = min(bid,ask)/max(bid,ask). <0.30 = опустевшая сторона <30% другой.
MAX_DEPLETION_RATIO: float = 0.30

# Анти-шум floor: суммарная глубина у best в SHARE-единицах (depth_bid/ask = sum size).
# СТАРТ 0.0 (не фильтрует) — собрать распределение depth_total 3-5 дней, потом порог.
MIN_TOTAL_DEPTH_SHARES: float = 0.0  # ⚠ КАЛИБРОВАТЬ после 3-5 дней shadow

# Forward-эффект до 60м → сигнал «живёт» 60м для snapshot-writer covering.
HORIZON_SEC: int = 60 * 60  # 3600s

# Measurement, НЕ trading: confidence/edge — заглушки, не используются в анализе.
FIXED_CONFIDENCE: float = 0.50
FIXED_EDGE_PCT: float = 0.0


def detect(market_event: dict[str, Any]) -> Signal | None:
    """Emit liquidity-vacuum measurement signal если депление односторонне.

    Required market_event fields (собирает _m2_emit_async из WSS-контекста):
      market_id, event_id?, token_id?, category?, yes_midpoint (ЦЕНА СИГНАЛА),
      depth_bid, depth_ask, depleted_side ('bid'|'ask'), depletion_ratio,
      whale_in_window, detected_at (ISO).
    """
    # Гейт 1: mid-зона.
    yes_mid = market_event.get("yes_midpoint")
    if yes_mid is None:
        return None
    yes_mid = float(yes_mid)
    if not (MIN_YES_PRICE <= yes_mid <= MAX_YES_PRICE):
        return None

    # Гейт 2: floor (мёртвый стакан ≠ вакуум).
    depth_bid = float(market_event.get("depth_bid", 0.0))
    depth_ask = float(market_event.get("depth_ask", 0.0))
    if depth_bid + depth_ask < MIN_TOTAL_DEPTH_SHARES:
        return None

    # Гейт 3: depletion.
    hi = max(depth_bid, depth_ask)
    lo = min(depth_bid, depth_ask)
    if hi <= 0:
        return None
    depletion_ratio = lo / hi
    if depletion_ratio >= MAX_DEPLETION_RATIO:
        return None

    # Side: asks вымели (depth_ask < depth_bid) → YES↑; bids вымели → YES↓.
    depleted_side = market_event.get("depleted_side")
    side = "YES" if depleted_side == "ask" else "NO"

    # YES-token: предпочитаем resolve из clob_token_ids (как D36:196); если
    # хук передал только WSS asset (token_id) — используем его как fallback.
    token = yes_token_id(market_event.get("clob_token_ids")) or market_event.get("token_id")

    # event_id = events.polymarket_id (text|None) — hook отдаёт его через LEFT JOIN
    # events (конвенция d38_scan.py). str()-коэрция остаётся defensive: если caller
    # передал не-str (напр. UUID), asyncpg упал бы с "expected str, got UUID".
    event_raw = market_event.get("event_id")
    event_id = str(event_raw) if event_raw is not None else None

    return Signal(
        detector=DETECTOR_NAME,
        market_id=str(market_event["market_id"]),
        side=side,
        generated_at=datetime.now(UTC),
        confidence=FIXED_CONFIDENCE,
        expected_edge_pct=FIXED_EDGE_PCT,
        fair_price=yes_mid,  # = current_price → divergence 0, пайплайн не выберет M2 как торговый
        current_price=yes_mid,  # midpoint t0 (НЕ markets.current_price) — look-ahead-фикс
        horizon_sec=HORIZON_SEC,
        token_id=token,
        event_id=event_id,
        evidence={
            "depth_bid": round(depth_bid, 1),
            "depth_ask": round(depth_ask, 1),
            "depletion_ratio": round(depletion_ratio, 4),
            "depleted_side": depleted_side,
            "mid_price": round(yes_mid, 4),
            "whale_in_window": bool(market_event.get("whale_in_window", False)),
            "detected_at": market_event.get("detected_at"),
            "category": market_event.get("category"),
            "tx_hash": market_event.get("tx_hash"),
            "strategy": "liquidity_vacuum",
        },
    )
