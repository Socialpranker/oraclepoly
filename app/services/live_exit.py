"""Выход из live-позиции: SELL FOK по token_id позиции (audit топ-1: live был BUY-only).

Используется: TG-кнопкой/endpoint (аварийный ручной close) и position_manager (TP/SL/max_hold).
"""

from __future__ import annotations

import structlog

from app.core.config import get_settings
from app.services.database import execute, fetchrow

logger = structlog.get_logger()

_SELL_BID_BUFFER = 0.02  # floor: чуть ниже живого bid — FOK переживает микродвижение

_FETCH_LIVE_ORDER_SQL = """
SELECT eo.id, eo.status, eo.stake, eo.fill_price, eo.filled_qty, eo.market_id,
       ds.token_id
  FROM execution_orders eo
  JOIN detector_signals ds ON ds.id = eo.detector_signal_id
 WHERE eo.id = $1
"""

_RESERVE_EXIT_PENDING_SQL = """
UPDATE execution_orders
   SET status = 'exit_pending', exit_reason = $2
 WHERE id = $1 AND status = 'filled'
RETURNING id
"""

_MARK_EXITING_SQL = """
UPDATE execution_orders
   SET status = 'exiting', exit_order_id = $2, exit_requested_at = now()
 WHERE id = $1 AND status = 'exit_pending'
"""

_ROLLBACK_TO_FILLED_SQL = """
UPDATE execution_orders
   SET status = 'filled'
 WHERE id = $1 AND status = 'exit_pending'
"""


async def close_live_position(order_db_id: str, reason: str) -> tuple[bool, str]:
    """Разместить SELL по живой filled-позиции. Возвращает (ok, message).

    message при ok=True — CLOB orderID нового SELL-ордера. При ok=False — код причины
    отказа (order_not_found / not_closeable:<status> / already_exiting / no_shares_to_exit /
    no_book_for_exit / exit_post_failed).

    Race-condition fix (2026-07-02): между guard-check и походом на CLOB зарезервировано
    намерение атомарным UPDATE...RETURNING (status -> 'exit_pending'). Если резерв не удался
    (0 строк — статус уже сменил другой одновременный вызов), CLOB не трогаем — второй вызов
    для одной и той же позиции больше не может разместить второй реальный SELL. Любой отказ
    ПОСЛЕ успешного резерва обязан откатить exit_pending -> filled, иначе позиция зависает.
    """
    row = await fetchrow(_FETCH_LIVE_ORDER_SQL, order_db_id)
    if row is None:
        return False, "order_not_found"
    if row["status"] != "filled":
        return False, f"not_closeable:{row['status']}"

    reserved = await fetchrow(_RESERVE_EXIT_PENDING_SQL, order_db_id, reason)
    if reserved is None:
        return False, "already_exiting"

    async def _rollback() -> None:
        await execute(_ROLLBACK_TO_FILLED_SQL, order_db_id)

    fill_price = float(row["fill_price"] or 0)
    filled_qty = float(row["filled_qty"] or 0)
    shares = filled_qty if filled_qty > 0 else (float(row["stake"]) / fill_price if fill_price > 0 else 0.0)
    if shares <= 0:
        await _rollback()
        return False, "no_shares_to_exit"

    # price-floor от живого bid: fetch_live_book умеет WS-кэш → REST fallback.
    from app.services.execution_safety import fetch_live_book

    book = await fetch_live_book(row["token_id"])
    if book is None or not book.get("bid"):
        await _rollback()
        return False, "no_book_for_exit"
    floor = round(max(float(book["bid"]) * (1.0 - _SELL_BID_BUFFER), 0.01), 2)

    import asyncio

    from py_clob_client_v2 import ApiCreds, MarketOrderArgs, OrderType, PartialCreateOrderOptions

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

    order_args = MarketOrderArgs(
        token_id=row["token_id"],
        amount=round(shares, 2),
        side="SELL",
        price=floor,
    )
    try:
        resp = await asyncio.to_thread(
            client.create_and_post_market_order,
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FOK,
        )
    except Exception as exc:
        logger.warning("live_exit_post_exception", order=order_db_id, error=str(exc)[:200])
        await _rollback()
        return False, "exit_post_failed"

    if not resp or not resp.get("success"):
        logger.warning("live_exit_post_failed", order=order_db_id, resp=str(resp)[:200])
        await _rollback()
        return False, "exit_post_failed"

    exit_order_id = resp.get("orderID")
    if not exit_order_id:
        logger.warning("live_exit_no_order_id", order=order_db_id, resp=str(resp)[:200])
        await _rollback()
        return False, "exit_post_failed"

    await execute(_MARK_EXITING_SQL, order_db_id, exit_order_id)
    return True, exit_order_id
