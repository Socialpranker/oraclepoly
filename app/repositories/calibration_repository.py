"""
Calibration Repository — CRUD для calibration_configs.

Предоставляет доступ к конфигурациям весов Bayesian Oracle:
- Активный конфиг по категории
- История конфигов
- Создание / активация
- Resolved snapshots для grid search

asyncpg-based, uses global connection pool from app.services.database.
"""

from typing import Any

from app.repositories.base import BaseRepository
from app.services.database import fetch, fetchrow, record_to_dict, records_to_list


class CalibrationRepository(BaseRepository):
    def __init__(self) -> None:
        pass

    async def get_active(self, category: str = "global") -> dict[str, Any] | None:
        """Получить активный конфиг для категории."""
        row = await fetchrow(
            "SELECT * FROM calibration_configs WHERE category = $1 AND is_active = true LIMIT 1",
            category,
        )
        return record_to_dict(row)

    async def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Получить историю конфигов (последние N)."""
        rows = await fetch(
            "SELECT * FROM calibration_configs ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return records_to_list(rows)

    async def create(self, config_data: dict[str, Any]) -> dict[str, Any]:
        """Создать новый конфиг (по умолчанию неактивный)."""
        return await self._create("calibration_configs", config_data)

    async def activate(self, config_id: str, category: str = "global") -> dict[str, Any]:
        """Деактивировать текущий активный конфиг и активировать новый.

        Выполняется в одной транзакции — невозможно состояние без активного конфига.
        """
        from app.services.database import transaction

        async with transaction() as conn:
            # Деактивировать текущий
            await conn.execute(
                "UPDATE calibration_configs SET is_active = false WHERE category = $1 AND is_active = true",
                category,
            )

            # Активировать новый
            row = await conn.fetchrow(
                "UPDATE calibration_configs SET is_active = true WHERE id = $1 RETURNING *",
                config_id,
            )

        return record_to_dict(row) or {}

    async def get_resolved_snapshots_for_calibration(
        self,
        max_age_days: int = 90,
        max_samples: int = 200000,
        price_range: tuple[float, float] | None = None,
    ) -> list[dict[str, Any]]:
        """Получить resolved snapshots с raw contributions для grid search.

        Возвращает только snapshots с заполненными raw колонками и resolved outcome.
        JOIN с markets для category.
        Ограничено max_age_days (default 90) и max_samples (default 200K) для OOM-safety.
        price_range — фильтр market_price (например (0.10, 0.90) для pre-resolution данных).
        Берёт последние max_samples записей (самые свежие), возвращает в ASC порядке.
        """
        price_filter = ""
        params: list = ["pending", max_age_days, max_samples]
        if price_range:
            price_filter = "AND os.market_price BETWEEN $4 AND $5"
            params.extend([price_range[0], price_range[1]])

        rows = await fetch(
            f"""
            SELECT * FROM (
                SELECT os.id, os.market_id, os.oracle_score, os.market_price, os.risk_score,
                       os.whale_contribution_raw, os.news_contribution_raw, os.z_posterior,
                       os.oi_factor_raw, os.concentration_factor_raw, os.micro_factor_raw,
                       os.sm_fresh_raw, os.sm_cluster_raw, os.sm_registry_raw,
                       os.brier_score, os.prediction_correct, os.outcome, os.created_at,
                       os.volume_24h, os.resolved_at,
                       COALESCE(m.category, 'Unknown') AS category,
                       m.end_date, m.event_id,
                       os.divergence,
                       EXISTS(
                           SELECT 1 FROM transactions t
                           WHERE t.market_id = os.market_id AND t.amount >= 5000
                       ) AS has_whale_activity
                FROM oracle_snapshots os
                LEFT JOIN markets m ON m.id = os.market_id
                WHERE os.outcome != $1
                  AND os.whale_contribution_raw IS NOT NULL
                  AND os.news_contribution_raw IS NOT NULL
                  AND os.created_at > now() - make_interval(days => $2)
                  {price_filter}
                ORDER BY os.created_at DESC
                LIMIT $3
            ) sub
            ORDER BY created_at ASC
            """,
            *params,
        )
        return records_to_list(rows)
