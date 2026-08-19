"""MtM refresh — Phase 1 Stage 1.

Запускается cron каждую минуту. Для каждой open position (status placed/filled/
virtual_placed/virtual_filled, settled_at IS NULL):
  1. JOIN markets.current_price (canonical YES, обновляется market_sync раз в 15 мин
     из Gamma; WS в эту колонку НЕ пишет — см. research/design-audit-2026-07-02.md топ-3).
  2. Считаем unrealized_pnl через [[bankroll-mtm]] semantics.
  3. UPDATE execution_orders SET mtm_price_last_check, unrealized_pnl_last,
     mtm_updated_at = now().

Используется в check_daily_pnl_limit_mtm как preventive daily loss gate.
См. docs/superpowers/plans/2026-05-risk-engine-hardening/01-mtm-daily-pnl.md.

Conservative behaviors:
  - markets.current_price IS NULL → row skipped, mtm_updated_at не обновляется
    (stale → daily PnL check трактует консервативно).
  - fill_price <= 0 (corrupted) → skip + warning log.
  - Stale (mtm_updated_at > 5 min) для конкретной позиции → она исключается из
    aggregate sum в check_daily_pnl_limit_mtm (отвечает за это сам safety check).

Не зависит от WS connectivity, не делает live запросов. Только БД.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.bankroll_mtm import MtMPositionSnapshot, compute_bankroll_with_mtm
from app.services.database import execute, fetch
from app.services.live_prices import get_live_yes_price, seed_book_snapshot
from app.services.price_staleness_telemetry import log_price_staleness

logger = structlog.get_logger(__name__)


async def _maybe_seed_position_book(row: dict[str, Any]) -> None:
    """REST-посев книги позиции, если WS-кэш не даёт live-цену (рынок не тикает).

    Fire-and-forget, ошибки не пропагируются. Throttle — внутри seed_book_snapshot.
    """
    token = row.get("yes_token_id")
    if not token or row.get("current_price") is None:
        return
    try:
        _, is_live = await get_live_yes_price(token, float(row["current_price"]))
        if not is_live:
            await seed_book_snapshot(token)
    except Exception as e:
        logger.debug("mtm_seed_failed", token=str(token)[:16], error=str(e)[:80])


# Position statuses where MtM is meaningful: open (placed) or filled but not yet
# settled. Virtual counterparts покрываются для shadow/virtual lifecycle tracking.
OPEN_POSITION_STATUSES: tuple[str, ...] = (
    "placed",
    "filled",
    "virtual_placed",
    "virtual_filled",
)

OPEN_POSITIONS_SQL = """
SELECT
    eo.id::text              AS id,
    eo.side                  AS side,
    eo.stake::float          AS stake,
    eo.fill_price::float     AS fill_price,
    m.current_price::float   AS current_price,
    eo.market_id             AS market_id,
    eo.status                AS status,
    m.clob_token_ids->>0     AS yes_token_id
  FROM execution_orders eo
  JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.status IN ('placed', 'filled', 'virtual_placed', 'virtual_filled')
   AND eo.settled_at IS NULL
   AND eo.fill_price IS NOT NULL
   AND m.resolved = false
"""


def _row_to_snapshot(row: dict[str, Any]) -> MtMPositionSnapshot:
    """DB row → MtMPositionSnapshot. fill_price уже side-adjusted (SSoT
    position_manager.py:151-153), потому используем напрямую как entry_price."""
    return MtMPositionSnapshot(
        order_id=str(row["id"]),
        stake=float(row["stake"]),
        entry_price=float(row["fill_price"]),
        current_price=(float(row["current_price"]) if row["current_price"] is not None else None),
        side=row["side"],
    )


def _build_update_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Преобразует DB rows в writeable updates.

    Skip rules:
      - current_price IS NULL → пропускаем (нечего считать).
      - fill_price <= 0 или >= 1 → пропускаем (corrupted).
      - unknown side → пропускаем.
    """
    now = datetime.now(UTC)
    updates: list[dict[str, Any]] = []

    for row in rows:
        snap = _row_to_snapshot(row)
        if snap.current_price is None:
            continue
        if snap.entry_price <= 0 or snap.entry_price >= 1.0:
            logger.warning(
                "mtm_refresh_bad_entry_price",
                order_id=snap.order_id,
                entry_price=snap.entry_price,
            )
            continue
        if snap.side not in ("YES", "NO"):
            logger.warning("mtm_refresh_unknown_side", order_id=snap.order_id, side=snap.side)
            continue

        result = compute_bankroll_with_mtm(wallet_balance=0.0, frozen_stake=0.0, open_positions=[snap])
        updates.append(
            {
                "order_id": snap.order_id,
                "mtm_price": snap.current_price,
                "unrealized_pnl": round(result.unrealized_pnl, 4),
                "mtm_updated_at": now,
            }
        )

    return updates


async def _refresh_peak_bank(unrealized_total: float) -> None:
    """Phase 1 Stage 2: piggyback peak_bank update в mtm-refresh cron.

    MtM equity ≈ wallet + frozen_stake + unrealized. peak_bank tracking
    нужен risk_engine.check_risk для drawdown reduce. Если live bankroll
    недоступен (Redis/wallet failure) — silent skip, peak держится stale.
    """
    try:
        from app.core.peak_bank import update_peak_if_higher
        from app.core.sizing_controller import get_live_bankroll
        from app.services.database import fetchrow

        snap = await get_live_bankroll()
        # frozen_stake = sum live placed/filled stakes (исключая virtual)
        row = await fetchrow(
            """
            SELECT COALESCE(SUM(stake), 0)::float AS frozen
              FROM execution_orders
             WHERE status IN ('placed', 'filled')
               AND settled_at IS NULL
               AND execution_mode != 'virtual'
            """
        )
        frozen = float(row["frozen"]) if row else 0.0
        mtm_equity = snap.bankroll_usd + frozen + unrealized_total
        if mtm_equity > 0:
            await update_peak_if_higher(mtm_equity)
    except Exception as e:
        logger.warning("mtm_refresh_peak_bank_skip", error=str(e))


async def run_mtm_refresh() -> dict[str, Any]:
    """Refresh MtM columns для всех open positions."""
    stats: dict[str, Any] = {
        "rows_fetched": 0,
        "rows_updated": 0,
        "rows_skipped": 0,
    }

    try:
        rows = await fetch(OPEN_POSITIONS_SQL)
        rows_as_dicts = [dict(r) for r in rows]
        stats["rows_fetched"] = len(rows_as_dicts)
    except Exception as e:
        logger.error("mtm_refresh_fetch_failed", error=str(e))
        return {**stats, "error": str(e)}

    # Shadow-телеметрия (План 1) — только real-money статусы, virtual не логируем
    # (объём×шум; cap live-позиций = 10, значит ≤10 строк/мин).
    for row in rows_as_dicts:
        if row["status"] in ("placed", "filled") and row["current_price"] is not None:
            asyncio.create_task(
                log_price_staleness(
                    "mtm",
                    str(row["market_id"]),
                    row.get("yes_token_id"),
                    float(row["current_price"]),
                )
            )
            # Рынок позиции без WS-тиков не имеет live-цены — сеем книгу REST'ом
            # (throttle внутри seed_book_snapshot, ≤1 REST/4мин на токен).
            asyncio.create_task(_maybe_seed_position_book(row))

    updates = _build_update_rows(rows_as_dicts)
    stats["rows_skipped"] = stats["rows_fetched"] - len(updates)

    if not updates:
        logger.info("mtm_refresh_no_updates", **stats)
        return stats

    # Bulk UPDATE через CTE — атомарно, single round-trip.
    # Используем VALUES list для batch update.
    for upd in updates:
        try:
            await execute(
                """
                UPDATE execution_orders
                   SET mtm_price_last_check = $1,
                       unrealized_pnl_last  = $2,
                       mtm_updated_at       = $3
                 WHERE id = $4::uuid
                """,
                upd["mtm_price"],
                upd["unrealized_pnl"],
                upd["mtm_updated_at"],
                upd["order_id"],
            )
            stats["rows_updated"] += 1
        except Exception as e:
            logger.error(
                "mtm_refresh_update_failed",
                order_id=upd["order_id"],
                error=str(e),
            )

    # Phase 1 Stage 2: piggyback peak_bank update.
    unrealized_total = sum(u["unrealized_pnl"] for u in updates)
    await _refresh_peak_bank(unrealized_total)

    logger.info("mtm_refresh_complete", **stats)
    return stats
