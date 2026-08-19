"""Daily price snapshot — ежедневный snapshot всех active markets для
rolling-window detectors.

Cron: ежедневно 00:05 UTC через scripts/cron_runner.py daily-price-snapshot.

Source of truth: markets.current_price (обновляется market-sync каждые 15 мин).
Single INSERT … SELECT на ~30K markets выполняется за <1 секунду.

Idempotency: PRIMARY KEY (condition_id, snapshot_date) → ON CONFLICT DO UPDATE.
Если cron сработает дважды в один день — последний run перезапишет yes_price.

Storage: ~30K rows × 50 bytes ≈ 1.5 MB/day, ~550 MB/year.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.services.database import fetchval

logger = structlog.get_logger(__name__)

# Minimum volume_24h для inclusion. Markets с очень низкой ликвидностью
# не нужны для mean-reversion (не реализуем такие сигналы), храним только
# markets с реальной активностью. $100 — мягкий floor, не $5K (мы не хотим
# терять history для markets которые могли стать ликвидными).
MIN_VOLUME_24H_USD: float = 100.0


_SNAPSHOT_SQL = """
INSERT INTO daily_price_snapshots (condition_id, snapshot_date, yes_price, volume_24h, captured_at)
SELECT
    m.condition_id,
    CURRENT_DATE AS snapshot_date,
    m.current_price::numeric(6, 4) AS yes_price,
    m.volume_24h,
    now() AS captured_at
  FROM markets m
 WHERE m.is_active = true
   AND m.resolved = false
   AND m.condition_id IS NOT NULL
   AND m.current_price IS NOT NULL
   AND m.current_price > 0
   AND m.current_price < 1
   AND COALESCE(m.volume_24h, 0) >= $1
ON CONFLICT (condition_id, snapshot_date)
DO UPDATE SET
    yes_price   = EXCLUDED.yes_price,
    volume_24h  = EXCLUDED.volume_24h,
    captured_at = EXCLUDED.captured_at
RETURNING 1
"""

# Counting через COUNT(*) после INSERT … RETURNING было бы лучше, но
# asyncpg возвращает только последнюю строку. Используем фоллбек на отдельный
# COUNT.
_COUNT_INSERTED_SQL = """
SELECT COUNT(*)::bigint
  FROM markets m
 WHERE m.is_active = true
   AND m.resolved = false
   AND m.condition_id IS NOT NULL
   AND m.current_price IS NOT NULL
   AND m.current_price > 0
   AND m.current_price < 1
   AND COALESCE(m.volume_24h, 0) >= $1
"""


async def run_daily_price_snapshot() -> dict[str, Any]:
    """Snapshot today's yes_price for every active market.

    Returns:
        Dict с metrics: n_snapshotted, min_volume, status.
    """
    # Expected row count первым (для логирования + ratio check).
    expected = await fetchval(_COUNT_INSERTED_SQL, MIN_VOLUME_24H_USD) or 0
    if expected == 0:
        logger.warning("daily_price_snapshot_no_active_markets")
        return {"n_snapshotted": 0, "min_volume": MIN_VOLUME_24H_USD, "status": "no_active"}

    # Execute upsert.
    await fetchval(_SNAPSHOT_SQL, MIN_VOLUME_24H_USD)

    logger.info(
        "daily_price_snapshot_done",
        n_snapshotted=expected,
        min_volume=MIN_VOLUME_24H_USD,
    )

    return {
        "n_snapshotted": int(expected),
        "min_volume": MIN_VOLUME_24H_USD,
        "status": "ok",
    }
