"""
Resolve oracle/vote outcomes when markets close.

Использует нашу таблицу markets (current_price, is_active) для определения исходов.
Gamma API conditionId lookup сломан — возвращает дефолтные данные.

При резолюции обновляет W/L и P&L метрики китов, торговавших на рынке.
"""

from datetime import UTC, datetime

import structlog

from app.services.database import execute, fetch

logger = structlog.get_logger(__name__)

# Пороги для определения исхода по current_price
PRICE_THRESHOLD_YES = 0.95  # current_price > 0.95 → resolved YES
PRICE_THRESHOLD_NO = 0.05  # current_price < 0.05 → resolved NO


async def resolve_single_market(market_id: str, outcome: str, resolve_price: float) -> int:
    """Resolve один рынок: snapshots + strategy positions + whale metrics.

    Вызывается из: WS market_resolved, market_sync (Gamma resolved), hourly sweep.
    Returns: количество resolved snapshots.
    """
    from app.repositories.oracle_snapshot_repository import OracleSnapshotRepository

    snapshot_repo = OracleSnapshotRepository()

    try:
        snap_count = await snapshot_repo.resolve_for_market(market_id, outcome, resolve_price)

        # V2 pipeline: closing execution_orders → v2_execution_resolve._settle_filled()
        # на основе detector_signals.resolved + outcome (cron */1 min).

        if snap_count > 0:
            logger.info(
                "resolved_oracle_snapshots",
                market_id=str(market_id),
                outcome=outcome,
                price=round(resolve_price, 4),
                count=snap_count,
            )

            # Whale metrics
            try:
                from app.core.cache import cache_delete_pattern

                whale_rows = await fetch(
                    """
                    SELECT DISTINCT w.id, w.address
                    FROM transactions t
                    JOIN whales w ON w.id = t.whale_id
                    WHERE t.market_id = $1 AND t.whale_id IS NOT NULL
                    """,
                    market_id,
                )
                for wr in whale_rows:
                    await cache_delete_pattern(f"whale_metrics:{wr['id']}")

                whales_updated = await _recalculate_whale_metrics(whale_rows)
                if whale_rows:
                    logger.info(
                        "whale_metrics_recalculated",
                        market_id=str(market_id),
                        whales=len(whale_rows),
                        updated=whales_updated,
                    )
            except Exception as cache_err:
                logger.warning("whale_metrics_update_failed", market_id=str(market_id), error=str(cache_err))

        return snap_count
    except Exception as e:
        logger.error("resolve_single_market_failed", market_id=str(market_id), error=str(e))
        return 0


async def run_resolve_outcomes() -> dict:
    """
    6-уровневый resolve sweep (hourly cron).

    Уровень 1 (WebSocket market_resolved) — обрабатывается в polymarket_ws_service.py, не здесь.
    Уровень 2: markets WHERE resolved=true (Gamma/WS пометили) + pending snapshots.
    Уровень 3: The Graph on-chain redemption check (is_active=false).
    Уровень 4: Price proxy 0.95/0.05 (legacy safety net).
    Уровень 5: strategy positions на рынках с истёкшим end_date (Polymarket задерживает резолюцию).
    Уровень 6: paper positions orphaned после retention cleanup.
    """
    oracle_resolved_total = 0
    resolved_market_ids: set[str] = set()

    # --- Уровень 2: Gamma/WS resolved=true ---
    gamma_resolved = await fetch(
        """
        SELECT DISTINCT m.id, m.current_price, m.is_active, m.question, m.winner
        FROM markets m
        INNER JOIN oracle_snapshots os ON os.market_id = m.id AND os.outcome = $1
        WHERE m.resolved = true
        """,
        "pending",
    )

    for market in gamma_resolved:
        market_id = market["id"]
        winner = market.get("winner") or ""
        current_price = float(market["current_price"])

        if winner:
            outcome = "yes" if winner.lower() in ("yes", "1", "true") else "no"
        elif current_price > PRICE_THRESHOLD_YES:
            outcome = "yes"
        elif current_price < PRICE_THRESHOLD_NO:
            outcome = "no"
        else:
            continue

        snap_count = await resolve_single_market(market_id, outcome, current_price)
        oracle_resolved_total += snap_count
        resolved_market_ids.add(str(market_id))

    # --- Уровень 3: The Graph on-chain ---
    remaining_pending = await fetch(
        """
        SELECT DISTINCT m.id, m.current_price, m.condition_id
        FROM markets m
        INNER JOIN oracle_snapshots os ON os.market_id = m.id AND os.outcome = $1
        WHERE m.resolved = false AND m.is_active = false
        """,
        "pending",
    )

    try:
        from app.services.thegraph_service import TheGraphService

        thegraph = TheGraphService()

        for market in remaining_pending:
            mid_str = str(market["id"])
            if mid_str in resolved_market_ids:
                continue

            condition_id = market.get("condition_id") or ""
            if not condition_id:
                continue

            if await thegraph.check_market_resolved(condition_id):
                current_price = float(market["current_price"])
                outcome = "yes" if current_price > 0.5 else "no"
                snap_count = await resolve_single_market(market["id"], outcome, current_price)
                oracle_resolved_total += snap_count
                resolved_market_ids.add(mid_str)
                await execute("UPDATE markets SET resolved = true WHERE id = $1", market["id"])
    except Exception as e:
        logger.warning("thegraph_sweep_failed", error=str(e))

    # --- Уровень 4: Price proxy (legacy safety net) ---
    price_proxy_markets = await fetch(
        """
        SELECT DISTINCT m.id, m.current_price, m.is_active, m.question
        FROM markets m
        INNER JOIN oracle_snapshots os ON os.market_id = m.id AND os.outcome = $1
        WHERE (m.current_price > $2 OR m.current_price < $3)
          AND m.resolved = false
          AND (m.is_active = false
               OR m.id IN (SELECT DISTINCT market_id::uuid FROM execution_orders WHERE status IN ('intent', 'placed', 'filled') AND market_id ~ '^[0-9a-f-]{36}$'))
        """,
        "pending",
        PRICE_THRESHOLD_YES,
        PRICE_THRESHOLD_NO,
    )

    for market in price_proxy_markets:
        mid_str = str(market["id"])
        if mid_str in resolved_market_ids:
            continue

        current_price = float(market["current_price"])
        outcome = "yes" if current_price > PRICE_THRESHOLD_YES else "no"
        snap_count = await resolve_single_market(market["id"], outcome, current_price)
        oracle_resolved_total += snap_count

    # Refresh materialized view.
    # CONCURRENTLY требует чтобы matview был уже populated. После migration
    # matview пустой → CONCURRENTLY падает → fallback на обычный REFRESH.
    try:
        await execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_market_resolved_outcome")
        logger.info("refreshed_mv_market_resolved_outcome", mode="concurrent")
    except Exception as e:
        err_msg = str(e)
        if "not populated" in err_msg or "CONCURRENTLY cannot" in err_msg:
            # Initial populate без CONCURRENTLY
            try:
                await execute("REFRESH MATERIALIZED VIEW mv_market_resolved_outcome")
                logger.info("refreshed_mv_market_resolved_outcome", mode="initial_populate")
            except Exception as e2:
                logger.warning("mv_refresh_failed", error=str(e2))
        else:
            logger.warning("mv_refresh_failed", error=err_msg)

    markets_checked = len(gamma_resolved) + len(remaining_pending) + len(price_proxy_markets)
    return {
        "status": "success",
        "oracle_resolved": oracle_resolved_total,
        "markets_checked": markets_checked,
    }


async def _recalculate_whale_metrics(whale_rows: list[dict]) -> int:
    """Пересчитать W/L и P&L для списка китов после резолюции рынка."""
    if not whale_rows:
        return 0

    from app.handlers.whale_analytics import WhaleAnalyticsEngine

    engine = WhaleAnalyticsEngine()
    updated = 0

    for wr in whale_rows:
        address = wr.get("address", "")
        if not address:
            continue
        try:
            wins, losses = await engine.calculate_win_loss_from_db(address)
            pnl, total_invested = await engine.calculate_pnl_from_db(address)
            roi = (pnl / total_invested * 100) if total_invested > 0 else 0

            win_rate = wins / max(wins + losses, 1)
            await execute(
                """
                UPDATE whales
                SET wins = $1, losses = $2, pnl_usd = $3, roi_percent = $4,
                    total_invested = $5, metrics_updated_at = $6,
                    win_rate = $8
                WHERE id = $7
                """,
                wins,
                losses,
                round(float(pnl), 2),
                round(float(roi), 2),
                round(float(total_invested), 2),
                datetime.now(UTC),
                wr["id"],
                win_rate,
            )
            updated += 1
        except Exception as e:
            logger.warning("whale_wl_recalc_failed", address=address[:10], error=str(e))

    return updated
