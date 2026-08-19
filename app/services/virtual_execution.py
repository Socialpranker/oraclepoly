"""Virtual execution v2_conservative — strict price-through + adverse selection.

Phase 0 Stage 2: заменяет fill-on-touch (v1_optimistic) на conservative model.
См. docs/superpowers/plans/2026-05-honest-baseline/02-conservative-queue.md.

Key invariants v2_conservative:
1. Maker fill только когда `best_bid > limit_price` (strictly), не `>=`.
2. При placement: записываем queue_position_at_placement (для resolve check).
3. Каждый fill: AS penalty = P_INFORMED × half_spread per share.
4. Resolve: fill либо при price_through, либо при traded_volume ≥ queue (см.
   simulate_resolve_maker). Default observed_traded_volume=0 → conservative
   fallback пока не подключён trades feed (Stage 2 follow-up).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

VIRTUAL_MAKER_TIMEOUT_SEC: int = 300  # 5 минут — синхронно с реальным stale_timeout
REST_QUOTA_PER_TICK: int = 1500
SIMULATION_MODEL_VERSION: str = "v2_conservative"

# Adverse selection: P(трейдер информированный | observed fill).
# Default — single-name Polymarket markets per Bartlett & O'Hara 2026 (SSRN 6615739).
# Phase 0 Stage 2 follow-up: per-detector override через detector_state.p_informed_override.
P_INFORMED_SINGLE_NAME: float = 0.50
P_INFORMED_BROAD: float = 0.30

# Sanity guard (legacy): reject fill если price ушёл слишком далеко от signal.
# Старая бага 2026-05-13: book_ordering возвращал worst-of-book (0.999 ask),
# давая fake $20K-60K PnL. Этот guard служит double-protection даже после fix.
MAX_FILL_DRIFT_FROM_SIGNAL_REL: float = 0.50  # 50% relative


# ── REST quota ───────────────────────────────────────────────────────────────

_rest_fetch_count: dict[str, int] = {"used": 0}


def reset_rest_quota() -> None:
    """Вызвать в начале cron tick'а."""
    _rest_fetch_count["used"] = 0


# ── Book fetch (WS first, REST fallback) ─────────────────────────────────────


async def _fetch_book_ws_only(token_id: str) -> dict[str, Any] | None:
    """Hot path: только WS cache. Cache miss → None."""
    if not token_id:
        return None
    from app.core.cache import cache_get

    try:
        ws = await cache_get(f"microstructure:{token_id}")
    except Exception:
        return None
    if not ws or not isinstance(ws, dict):
        return None
    from app.services.live_prices import is_ws_snapshot_live

    # State-TTL (2026-07-03): свежесть решает epoch-гейт, протухший хит = miss
    if not await is_ws_snapshot_live(ws):
        return None
    b = ws.get("best_bid")
    a = ws.get("best_ask")
    if b is None or a is None:
        return None
    try:
        bf, af = float(b), float(a)
    except (ValueError, TypeError):
        return None
    if not (0.0 < bf < 1.0 and 0.0 < af < 1.0):
        return None
    # bid_size / ask_size опциональны (WS cache может не давать). Прокинем если есть.
    out: dict[str, Any] = {"bid": bf, "ask": af}
    for key in ("bid_size", "ask_size", "best_bid_size", "best_ask_size"):
        v = ws.get(key)
        if v is not None:
            try:
                out[key] = float(v)
            except (ValueError, TypeError):
                pass
    return out


async def _fetch_book_rest(token_id: str) -> dict[str, Any] | None:
    """REST fallback. Quota'ed: возвращает None если квота исчерпана.

    Polymarket book ordering (discovered 2026-05-13):
      - bids ascending price → best bid = max(prices)
      - asks descending price → best ask = min(prices)
    """
    if _rest_fetch_count["used"] >= REST_QUOTA_PER_TICK:
        return None
    _rest_fetch_count["used"] += 1

    try:
        from app.services.polymarket_poller_service import get_poller_service

        book = await get_poller_service().fetch_order_book_rest(token_id)
    except Exception:
        return None
    if not book:
        return None
    asks = book.get("asks") or []
    bids = book.get("bids") or []
    if not asks or not bids:
        return None
    try:
        bid_levels: dict[float, float] = {}
        for b in bids:
            p = float(b["price"])
            s = float(b["size"])
            if 0.0 < p < 1.0 and s > 0:
                bid_levels[p] = bid_levels.get(p, 0.0) + s
        ask_levels: dict[float, float] = {}
        for a in asks:
            p = float(a["price"])
            s = float(a["size"])
            if 0.0 < p < 1.0 and s > 0:
                ask_levels[p] = ask_levels.get(p, 0.0) + s
        if not bid_levels or not ask_levels:
            return None
        bid = max(bid_levels)
        ask = min(ask_levels)
    except (ValueError, TypeError, KeyError, IndexError):
        return None
    if not (0.0 < bid < 1.0 and 0.0 < ask < 1.0):
        return None
    if bid >= ask:
        return None
    return {
        "bid": bid,
        "ask": ask,
        "bid_size": bid_levels[bid],
        "ask_size": ask_levels[ask],
    }


async def _fetch_book(token_id: str) -> dict[str, Any] | None:
    """WS first, REST fallback (quota'ed)."""
    book = await _fetch_book_ws_only(token_id)
    if book is not None:
        return book
    return await _fetch_book_rest(token_id)


# ── Adverse selection penalty ────────────────────────────────────────────────


def _adverse_selection_penalty_per_share(book: dict[str, Any] | None) -> float:
    """P(informed) × half_spread. Возвращает absolute price delta для buy-side.

    P_INFORMED_SINGLE_NAME (0.5) — default для типичных Polymarket markets
    (Bartlett & O'Hara 2026). Phase 0 follow-up: per-detector override.

    Returns 0.0 если book невалиден (нет valid spread).
    """
    if book is None:
        return 0.0
    try:
        bid = float(book.get("bid", 0))
        ask = float(book.get("ask", 0))
    except (TypeError, ValueError):
        return 0.0
    if not (0 < bid < ask < 1):
        return 0.0
    half_spread = (ask - bid) / 2.0
    return P_INFORMED_SINGLE_NAME * half_spread


def _extract_queue_size_at_top(book: dict[str, Any], *, side: str = "bid") -> float:
    """Размер queue на best-bid (или best-ask) level.

    WS cache даёт top size, REST full-book аггрегирует. Возвращает 0 если data
    недоступна — это conservative (предполагаем нет queue → fill только через
    price_through).
    """
    if side == "bid":
        for key in ("bid_size", "best_bid_size"):
            v = book.get(key)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return 0.0
    # ask
    for key in ("ask_size", "best_ask_size"):
        v = book.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _is_fill_realistic(*, fill_price: float, signal_price: float) -> bool:
    """Reject fill если price ушёл слишком далеко от signal (артефакт).

    Pattern bug: fill=0.999 на signal=0.05 (NO bought "at 0.999" но реально
    market NO=0.05 — это worst-ask placeholder). Guard reject'ит когда drift > 50%.
    """
    if signal_price <= 0 or fill_price <= 0:
        return False
    rel_drift = abs(fill_price - signal_price) / signal_price
    return rel_drift <= MAX_FILL_DRIFT_FROM_SIGNAL_REL


# ── Public dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class VirtualPlaceResult:
    """Результат симуляции placement.

    status:
      - 'virtual_filled' — taker FOK / maker GTC прошёл через limit
      - 'virtual_placed' — maker GTC ждёт fill в orderbook
      - 'virtual_failed' — book пуст / недоступен / invalid args
    """

    status: str
    fill_price: float | None = None
    limit_price: float | None = None
    queue_position_at_placement: float | None = None
    as_penalty_per_share: float = 0.0
    effective_fill_price: float | None = None
    reason: str = ""
    simulation_model_version: str = SIMULATION_MODEL_VERSION


@dataclass(frozen=True)
class VirtualResolveResult:
    """Результат проверки maker GTC статуса.

    status:
      - 'virtual_filled' — price moved through OR queue cleared
      - 'virtual_cancelled' — age > 5 min stale_timeout
      - 'still_open' — продолжаем ждать
    """

    status: str
    fill_price: float | None = None
    as_penalty_per_share: float = 0.0
    effective_fill_price: float | None = None
    reason: str = ""
    simulation_model_version: str = SIMULATION_MODEL_VERSION


# ── Core: simulate_place ─────────────────────────────────────────────────────


async def simulate_place(
    *,
    token_id: str,
    mode: str,
    limit_price: float | None,
    fallback_price: float | None = None,
    stake_usd: float = 0.0,
) -> VirtualPlaceResult:
    """Conservative placement simulation.

    Принципы:
    1. Maker fill при `best_bid > limit_price` (strictly). bid == limit → placed.
    2. При placed: записываем queue_position_at_placement (= bid_size).
    3. Каждый fill: AS penalty = P_INFORMED × half_spread.

    fallback_price (legacy): synthetic fill когда book недоступен — сохранён для
    backward compat thin markets без WS coverage.
    """
    book = await _fetch_book(token_id)

    if book is None:
        # Synthetic fallback
        if fallback_price is not None and 0 < fallback_price < 1:
            return VirtualPlaceResult(
                status="virtual_filled",
                fill_price=float(fallback_price),
                effective_fill_price=float(fallback_price),
                reason="synthetic_no_book",
            )
        return VirtualPlaceResult(status="virtual_failed", reason="no_book")

    as_penalty = _adverse_selection_penalty_per_share(book)

    if mode == "taker":
        fill_price = book["ask"]
        if fallback_price is not None and not _is_fill_realistic(fill_price=fill_price, signal_price=fallback_price):
            return VirtualPlaceResult(
                status="virtual_failed",
                reason=f"fill_drift_too_large:fill={fill_price:.4f},signal={fallback_price:.4f}",
            )
        return VirtualPlaceResult(
            status="virtual_filled",
            fill_price=fill_price,
            as_penalty_per_share=as_penalty,
            effective_fill_price=fill_price + as_penalty,
            reason="taker_fok",
        )

    # maker GTC
    if limit_price is None or limit_price <= 0:
        return VirtualPlaceResult(status="virtual_failed", reason="invalid_limit")

    bid = float(book["bid"])

    # CRITICAL v2 change: strict > (not >=)
    if bid > limit_price:
        return VirtualPlaceResult(
            status="virtual_filled",
            fill_price=limit_price,
            as_penalty_per_share=as_penalty,
            effective_fill_price=limit_price + as_penalty,
            reason="price_through",
        )

    # bid == limit OR bid < limit → placed in queue
    queue_size = _extract_queue_size_at_top(book, side="bid")
    return VirtualPlaceResult(
        status="virtual_placed",
        limit_price=limit_price,
        queue_position_at_placement=queue_size,
        reason="queued",
    )


# ── Core: simulate_resolve_maker ─────────────────────────────────────────────


async def simulate_resolve_maker(
    *,
    token_id: str,
    limit_price: float,
    age_sec: float,
    queue_position_at_placement: float = 0.0,
    observed_traded_volume_at_level: float = 0.0,
) -> VirtualResolveResult:
    """Conservative resolution simulation.

    Cases:
      A — best_bid > limit (price moved through) → fill.
      B — best_bid == limit AND traded_volume ≥ queue_position → fill.
      C — age > VIRTUAL_MAKER_TIMEOUT_SEC → cancelled.
      D — else → still_open.

    Default observed_traded_volume=0 → conservative: fill только через case A.
    Trades feed integration — Phase 0 Stage 2 follow-up.
    """
    book = await _fetch_book(token_id)

    if book is None:
        if age_sec > VIRTUAL_MAKER_TIMEOUT_SEC:
            return VirtualResolveResult(status="virtual_cancelled", reason="no_book_timeout")
        return VirtualResolveResult(status="still_open", reason="no_book_still_open")

    bid = float(book["bid"])
    as_penalty = _adverse_selection_penalty_per_share(book)

    # Case A: price moved strictly through limit
    if bid > limit_price:
        return VirtualResolveResult(
            status="virtual_filled",
            fill_price=limit_price,
            as_penalty_per_share=as_penalty,
            effective_fill_price=limit_price + as_penalty,
            reason="price_through",
        )

    # Case B: still at limit, queue consumption check
    if bid == limit_price and queue_position_at_placement > 0:
        if observed_traded_volume_at_level >= queue_position_at_placement:
            return VirtualResolveResult(
                status="virtual_filled",
                fill_price=limit_price,
                as_penalty_per_share=as_penalty,
                effective_fill_price=limit_price + as_penalty,
                reason="queue_cleared",
            )

    # Case C: timeout
    if age_sec > VIRTUAL_MAKER_TIMEOUT_SEC:
        return VirtualResolveResult(status="virtual_cancelled", reason="stale_timeout")

    return VirtualResolveResult(status="still_open", reason="awaiting_fill")


# ── Legacy: fill probability estimate ────────────────────────────────────────
# Используется как информационная метрика в dashboards, не блокирует execution.


@dataclass(frozen=True)
class FillProbability:
    fill_rate: float
    book_depth_at_or_better: float
    book_top_size: float
    reason: str = ""


def estimate_maker_fill_probability(
    *,
    book: dict[str, Any] | None,
    side: str,
    limit_price: float,
    stake_usd: float,
) -> FillProbability:
    """Информационная метрика — fill_rate для maker GTC заявки на текущем book.

    Используется dashboard'ом для построения paper vs expected live divergence.
    Не влияет на симуляцию fill в simulate_place / simulate_resolve_maker.
    """
    if book is None:
        return FillProbability(0.0, 0.0, 0.0, "no_book")

    bids_list = book.get("bids") if isinstance(book.get("bids"), list) else None
    asks_list = book.get("asks") if isinstance(book.get("asks"), list) else None

    if bids_list is None or asks_list is None:
        bid = float(book.get("bid") or 0.0)
        if side.upper() in ("YES", "BUY"):
            if bid >= limit_price:
                return FillProbability(1.0, stake_usd, stake_usd, "instant_top_only")
            return FillProbability(0.0, 0.0, 0.0, "no_depth_data")
        return FillProbability(0.0, 0.0, 0.0, "side_unsupported_light")

    side_u = side.upper()
    if side_u in ("YES", "BUY", "NO"):
        levels = asks_list
        usable_size = 0.0
        usable_usdc = 0.0
        for lv in levels:
            try:
                lv_price = float(lv.get("price") or 0)
                lv_size = float(lv.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if lv_price > limit_price + 1e-6:
                break
            usable_size += lv_size
            usable_usdc += lv_size * lv_price

        target_shares = stake_usd / max(limit_price, 0.001)
        if target_shares <= 0:
            return FillProbability(0.0, 0.0, 0.0, "zero_target")

        fill_rate = min(1.0, usable_size / target_shares) if target_shares > 0 else 0.0
        top_size = 0.0
        if levels:
            try:
                top_size = float(levels[0].get("size") or 0.0)
            except (TypeError, ValueError):
                top_size = 0.0

        reason = "full" if fill_rate >= 1.0 else ("partial" if fill_rate > 0 else "no_match")
        return FillProbability(
            fill_rate=round(fill_rate, 3),
            book_depth_at_or_better=round(usable_usdc, 2),
            book_top_size=round(top_size, 2),
            reason=reason,
        )

    return FillProbability(0.0, 0.0, 0.0, f"unknown_side_{side_u}")


async def fetch_full_book(token_id: str) -> dict[str, Any] | None:
    """Получить полный book (depth) через REST. WS cache недостаточен — там только top."""
    if _rest_fetch_count["used"] >= REST_QUOTA_PER_TICK:
        return None
    _rest_fetch_count["used"] += 1
    try:
        from app.services.polymarket_poller_service import get_poller_service

        return await get_poller_service().fetch_order_book_rest(token_id)
    except Exception:
        return None
