"""Multi-leg basket координатор (audit research-gap): D29/D40.

D29 (negrisk_basket_arb) — единственный математически гарантированный класс
арбитражей в детекторах — был выключен (settings.d29_basket_enabled=False,
проверяется в начале d29_scan.run_d29_scan), потому что execution pipeline
умела только single-leg fill: сигнал требует
buy NO на ВСЕ рынки события одновременно (n=3..6 ног), а `_process_signal`
(v2_execution.py) исполнил бы только primary-ногу, оставляя нас в exposure
1-leg вместо гарантированного basket-payout.

Статус-машина корзины: pending → all_placed → all_filled | unwinding → closed.
Вычислимая из статусов ног execution_orders с общим parent_basket_id — без
отдельной таблицы (Task 4 дизайн, docs/superpowers/plans/2026-07-02-audit-fixes-phase3.md).

Инварианты:
  1. Вход только если depth-check ВСЕХ ног прошёл (fetch_live_book, требуемый
     notional по асковой стороне ≥ stake ноги × 2 — по аналогии с
     execution_safety.check_market_book depth-логикой).
  2. Размещение — последовательные FOK-ноги с немедленным unwind при первом
     отказе (py_clob_client_v2.ClobClient.post_orders(list) существует, но
     принимает уже ПОДПИСАННЫЕ ордера (PostOrdersArgs с полем .order из
     create_order/create_market_order) и не даёт atomic-fill гарантию — каждая
     нога матчится на CLOB независимо от остальных даже в одном batch POST.
     Sequential FOK с синхронной проверкой fill-статуса после каждой ноги
     безопаснее: даёт немедленный сигнал unwind без риска частичного batch-fill,
     который всё равно пришлось бы разруливать той же unwind-логикой).
  3. Partial fill → unwinding: SELL исполненных ног через close_live_position
     (app/services/live_exit.py, Task 1), TG-алерт с деталями.
  4. Каждая нога — строка execution_orders с общим parent_basket_id.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog

from app.services.database import execute, fetchrow

logger = structlog.get_logger()

# Depth-check: требуемый notional по асковой стороне ≥ stake ноги × 2.
# По аналогии с execution_safety.LIVE_MIN_BOOK_DEPTH_MULTIPLIER (та же
# multiplier=2.0 для depth относительно stake), но basket depth-check смотрит
# ТОЛЬКО на ask-сторону (мы покупаем NO = BUY), не на bid+ask сумму — вход в
# basket требует, чтобы КАЖДАЯ нога отдельно имела достаточную ask-глубину,
# иначе одна тонкая нога рвёт всю корзину после того как остальные уже куплены.
_BASKET_DEPTH_MULTIPLIER = 2.0

_INSERT_LEG_SQL = """
INSERT INTO execution_orders (
    detector, market_id, event_id, side,
    execution_mode, signal_price, order_type, limit_price, stake,
    bankroll_at_entry, status, parent_basket_id, meta
)
VALUES ($1, $2, $3, 'NO', $4, $5, 'FOK', NULL, $6, 0, $7, $8, $9::jsonb)
RETURNING id
"""

_MARK_LEG_SQL = """
UPDATE execution_orders
   SET status = $2, order_id = $3, fill_price = $4, filled_qty = $5, placed_at = now(),
       filled_at = CASE WHEN $2 = 'filled' THEN now() ELSE NULL END,
       rejection_reason = $6
 WHERE id = $1
"""


async def _get_clob_client() -> Any:
    """Аутентифицированный CLOB-клиент — тот же паттерн, что v2_execution._place_order
    и live_exit.close_live_position (ApiCreds из settings, set_api_creds перед POST)."""
    from py_clob_client_v2 import ApiCreds

    from app.core.config import get_settings
    from app.services.polymarket_service import PolymarketService

    settings = get_settings()
    svc = PolymarketService()
    client = svc.clob_client
    creds = ApiCreds(
        api_key=settings.polymarket_api_key,
        api_secret=settings.polymarket_api_secret,
        api_passphrase=settings.polymarket_api_passphrase,
    )
    client.set_api_creds(creds)
    return client


async def _check_leg_depth(token_id: str, stake_usd: float) -> tuple[bool, str]:
    """Depth-check одной ноги: ask-сторона книги должна выдержать stake×2 (invariant 1).

    По аналогии с execution_safety.check_market_book, но смотрим только на
    ask_size (мы всегда BUY NO на входе в basket) — bid-глубина нерелевантна
    для entry-check (unwind — отдельная SELL-операция через close_live_position,
    которая делает свой bid-check в fetch_live_book в момент unwind).
    """
    from app.services.execution_safety import fetch_live_book

    book = await fetch_live_book(token_id)
    if book is None:
        return False, "market_closed_or_empty_book"
    ask = float(book.get("ask") or 0)
    ask_size_usd = float(book.get("ask_size") or 0) * ask
    required = stake_usd * _BASKET_DEPTH_MULTIPLIER
    if ask_size_usd < required:
        return False, f"thin_leg_book:depth_{ask_size_usd:.1f}_need_{required:.1f}"
    return True, ""


async def _insert_leg(
    *,
    detector: str,
    market_id: str,
    event_id: str | None,
    signal_price: float,
    stake_usd: float,
    basket_id: uuid.UUID,
    status: str,
) -> str:
    import json

    row = await fetchrow(
        _INSERT_LEG_SQL,
        detector,
        market_id,
        event_id,
        "taker",
        signal_price,
        stake_usd,
        status,
        str(basket_id),
        json.dumps({"basket_leg": True}),
    )
    return str(row["id"])


async def _place_leg_fok(client, token_id: str, stake_usd: float, price_cap: float) -> tuple[bool, str, dict]:
    """Разместить одну FOK-ногу (BUY NO) и синхронно проверить fill.

    FOK матчится немедленно на CLOB, но create_and_post_market_order response
    содержит только {success, orderID} (см. v2_execution._place_order,
    live_exit.close_live_position — оба НЕ читают fill-статус из POST response).
    Fill-подтверждение требует follow-up client.get_order(order_id) — тот же
    паттерн, что v2_execution_resolve._poll_placed_orders использует для
    интерпретации MATCHED/CANCELLED/EXPIRED.

    Returns: (filled, order_id_or_reason, {fill_price, filled_qty}).
    """
    from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions

    order_args = MarketOrderArgs(token_id=token_id, amount=round(stake_usd, 2), side="BUY", price=price_cap)
    try:
        resp = await asyncio.to_thread(
            client.create_and_post_market_order,
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FOK,
        )
    except Exception as exc:
        logger.warning("basket_leg_post_exception", token_id=token_id[:16], error=str(exc)[:200])
        return False, f"place_error:{str(exc)[:150]}", {}

    if not resp or not resp.get("success"):
        reason = (resp or {}).get("errorMsg", "unknown")
        return False, f"place_rejected:{str(reason)[:150]}", {}

    order_id = resp.get("orderID")
    if not order_id:
        return False, "place_rejected:no_order_id", {}

    # Синхронный fill-check: FOK matches instantly, но резолвится через отдельный
    # запрос (POST response не несёт fill-статус — см. docstring). Один retry с
    # коротким sleep покрывает CLOB matching latency; если всё ещё не MATCHED/
    # CANCELLED/EXPIRED после этого — трактуем как non-fill (safe default: unwind
    # раньше, чем оставить позицию в неопределённом состоянии).
    for attempt in range(2):
        try:
            order_resp = await asyncio.to_thread(client.get_order, order_id)
        except Exception as exc:
            logger.warning("basket_leg_get_order_error", order_id=order_id, error=str(exc)[:200])
            order_resp = None
        if order_resp:
            pm_status = (order_resp.get("status") or "").upper()
            size_matched = float(order_resp.get("size_matched") or 0)
            original_size = float(order_resp.get("original_size") or 0)
            if pm_status == "MATCHED" or (size_matched > 0 and size_matched >= max(original_size, 1e-9) * 0.95):
                fill_price = float(order_resp.get("price") or price_cap)
                return True, order_id, {"fill_price": fill_price, "filled_qty": size_matched, "order_id": order_id}
            if pm_status in ("CANCELLED", "EXPIRED") and size_matched <= 0:
                return False, f"fok_not_filled:{pm_status}", {"order_id": order_id}
        if attempt == 0:
            await asyncio.sleep(0.5)

    # Не смогли подтвердить fill после retry — консервативно считаем non-fill,
    # чтобы unwind сработал вместо того чтобы оставить неизвестную позицию открытой.
    return False, "fok_status_unknown", {"order_id": order_id}


async def place_basket(legs: list[dict], detector: str, basket_id: uuid.UUID) -> str:
    """Разместить multi-leg basket (все ноги BUY NO на все рынки события).

    legs: [{token_id, stake_usd, price_cap, market_id, event_id?, signal_price?}].
    Возвращает: 'filled' | 'unwound' | 'rejected_depth'.

    Инвариант 1 (все depth-checks ДО первого POST): если хотя бы одна нога не
    проходит depth-check — ни один ордер не размещается (rejected_depth), корзина
    закрывается без единого POST. Инвариант 2/3: последовательные FOK, первый
    non-fill → unwind всех уже налитых ног через close_live_position + TG-алерт.
    """
    from app.services.live_exit import close_live_position
    from app.tasks.notifications import send_admin_message

    if not legs:
        return "rejected_depth"

    # Invariant 1: depth-check ВСЕХ ног до единого POST.
    depth_failures: list[str] = []
    for leg in legs:
        ok, reason = await _check_leg_depth(leg["token_id"], float(leg["stake_usd"]))
        if not ok:
            depth_failures.append(f"{leg.get('market_id', leg['token_id'])[:16]}:{reason}")
    if depth_failures:
        logger.info("basket_rejected_depth", basket_id=str(basket_id), detector=detector, failures=depth_failures)
        try:
            await send_admin_message(
                "❌ <b>Basket rejected: тонкая книга</b>\n"
                f"Детектор: <code>{detector}</code>\n"
                f"Корзина: <code>{str(basket_id)[:8]}</code>\n"
                f"Ноги: {', '.join(depth_failures)}"
            )
        except Exception as notify_exc:
            logger.warning("basket_depth_reject_notify_failed", error=str(notify_exc)[:200])
        return "rejected_depth"

    client = await _get_clob_client()
    filled_legs: list[dict] = []  # {leg, order_db_id, fill_price, filled_qty}

    for leg in legs:
        order_db_id = await _insert_leg(
            detector=detector,
            market_id=leg["market_id"],
            event_id=leg.get("event_id"),
            signal_price=float(leg.get("signal_price") or 0.0) or None,
            stake_usd=float(leg["stake_usd"]),
            basket_id=basket_id,
            status="intent",
        )
        filled, order_id_or_reason, fill_info = await _place_leg_fok(
            client, leg["token_id"], float(leg["stake_usd"]), float(leg["price_cap"])
        )
        if not filled:
            await execute(
                _MARK_LEG_SQL,
                order_db_id,
                "rejected",
                fill_info.get("order_id"),
                None,
                None,
                order_id_or_reason,
            )
            logger.warning(
                "basket_leg_not_filled",
                basket_id=str(basket_id),
                detector=detector,
                market_id=leg["market_id"],
                reason=order_id_or_reason,
            )
            break
        await execute(
            _MARK_LEG_SQL,
            order_db_id,
            "filled",
            order_id_or_reason,
            fill_info["fill_price"],
            fill_info["filled_qty"],
            None,
        )
        filled_legs.append({"leg": leg, "order_db_id": order_db_id, **fill_info})

    if len(filled_legs) == len(legs):
        logger.info("basket_filled", basket_id=str(basket_id), detector=detector, n_legs=len(legs))
        return "filled"

    # Invariant 3: partial fill → unwind всех налитых ног.
    unwind_results = []
    for fl in filled_legs:
        ok, msg = await close_live_position(fl["order_db_id"], reason="basket_unwind_partial_fill")
        unwind_results.append((fl["leg"]["market_id"], ok, msg))

    n_unwound_ok = sum(1 for _, ok, _ in unwind_results if ok)
    logger.warning(
        "basket_unwound",
        basket_id=str(basket_id),
        detector=detector,
        n_filled=len(filled_legs),
        n_total=len(legs),
        n_unwound_ok=n_unwound_ok,
    )
    try:
        detail_lines = "\n".join(
            f"• {mid[:20]}: {'✅ SELL sent' if ok else f'❌ {msg}'}" for mid, ok, msg in unwind_results
        )
        await send_admin_message(
            "🔴 <b>Basket unwind: partial fill</b>\n"
            f"Детектор: <code>{detector}</code>\n"
            f"Корзина: <code>{str(basket_id)[:8]}</code>\n"
            f"Налито {len(filled_legs)}/{len(legs)} ног. Unwind:\n{detail_lines}"
        )
    except Exception as notify_exc:
        logger.warning("basket_unwind_notify_failed", error=str(notify_exc)[:200])

    return "unwound"
