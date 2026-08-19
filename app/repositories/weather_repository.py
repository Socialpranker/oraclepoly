"""Weather arbitrage signals repository."""

from __future__ import annotations

import structlog

from app.services.database import execute, fetch, fetchrow

logger = structlog.get_logger()


async def insert_signal(data: dict) -> str | None:
    """
    Записать сигнал в objective_signals.

    Возвращает UUID новой записи или None при конфликте/ошибке.
    """
    try:
        row = await fetchrow(
            """
            INSERT INTO objective_signals (
                market_id, question, category,
                city, airport_station, temp_threshold, target_date,
                market_price, meteo_prob, divergence,
                metar_temp_c, forecast_temp_c, resolution_source,
                side, stake, enqueued
            ) VALUES (
                $1, $2, $3,
                $4, $5, $6, $7,
                $8, $9, $10,
                $11, $12, $13,
                $14, $15, $16
            )
            ON CONFLICT (market_id, signal_date) DO NOTHING
            RETURNING id
            """,
            data["market_id"],
            data["question"],
            data.get("category", "weather"),
            data.get("city"),
            data.get("airport_station"),
            data.get("temp_threshold"),
            data.get("target_date"),
            data["market_price"],
            data["meteo_prob"],
            data["divergence"],
            data.get("metar_temp_c"),
            data.get("forecast_temp_c"),
            data.get("resolution_source"),
            data["side"],
            data.get("stake"),
            data.get("enqueued", False),
        )
        if row:
            return str(row["id"])
        return None
    except Exception as e:
        logger.error("weather_signal_insert_error", error=str(e), market_id=data.get("market_id"))
        return None


async def mark_enqueued(market_id: str) -> None:
    """Пометить сигнал как отправленный в execution queue."""
    try:
        await execute(
            "UPDATE objective_signals SET enqueued = true WHERE market_id = $1 AND signal_date = CURRENT_DATE",
            market_id,
        )
    except Exception as e:
        logger.error("weather_signal_mark_error", error=str(e), market_id=market_id)


async def get_recent_signals(hours: int = 6) -> list[dict]:
    """Получить последние сигналы для мониторинга дублей."""
    try:
        rows = await fetch(
            """
            SELECT market_id, city, divergence, side, enqueued, created_at
            FROM objective_signals
            WHERE category = 'weather'
              AND created_at > now() - ($1 || ' hours')::interval
            ORDER BY created_at DESC
            LIMIT 100
            """,
            str(hours),
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("weather_signals_fetch_error", error=str(e))
        return []
