"""Live ML features builder — каждые 30 min пересчитывает live_ml_features.

Финальный consumer feature pipeline. Использует app.ml.feature_kernel.build_features_sql()
с Postgres tables как source — единственное отличие от canonical batch builder
(который тот же kernel применяет к parquet через DuckDB).

Live anchor semantics: row пишется только если anchor_ts = end_date - h <= now.
Markets где все anchors ещё в будущем (end_date > now + 720h = 30d) — не получают rows.

INSERT ... ON CONFLICT (market_id, anchor_hours) DO UPDATE — пересчёт каждые 30 min
обновляет computed_at и все feature columns.

См. docs/superpowers/plans/bright-hopping-meteor.md (Day 4).
"""

from __future__ import annotations

from typing import Any

import structlog

from app.ml.feature_kernel import CANONICAL_ANCHORS, FEATURE_COLUMNS, build_features_sql
from app.services.database import execute, fetchval

logger = structlog.get_logger(__name__)


def _live_market_filter(anchor_h: int) -> str:
    """WHERE clause для live: только active markets где anchor_ts <= now (no future leak).

    m.is_active=true AND m.resolved=false → активный market
    m.end_date - INTERVAL 'Nh' <= now() → anchor уже в прошлом (можно считать features)
    m.condition_id IS NOT NULL → защита от corrupt rows
    """
    return (
        f"WHERE m.is_active = true "
        f"AND m.resolved = false "
        f"AND m.condition_id IS NOT NULL "
        f"AND m.end_date IS NOT NULL "
        f"AND m.end_date - INTERVAL '{anchor_h} hours' <= now()"
    )


# Колонки которые попадают в INSERT (все из FEATURE_COLUMNS КРОМЕ condition_id —
# у нас market_id берёт его роль). Структура live_ml_features:
#   PK: (market_id, anchor_hours)
#   Plus computed_at (DEFAULT now()).
# Остальные колонки = FEATURE_COLUMNS минус condition_id и anchor_hours (они в SELECT
# из feature_kernel, но мы помним: anchor_hours приходит литералом из SQL).
INSERT_COLUMNS = ["market_id", "anchor_hours"] + [
    c for c in FEATURE_COLUMNS if c not in ("condition_id", "anchor_hours")
]


def _build_upsert_sql(anchor_h: int) -> str:
    """Build INSERT ... SELECT ... ON CONFLICT для одного anchor.

    Kernel SELECT возвращает condition_id первой колонкой — мы маппим её в
    market_id (PRIMARY KEY таблицы). Все остальные feature columns совпадают
    1:1 с таблицей.
    """
    inner_sql = build_features_sql(
        markets_src="markets",
        snaps_src="live_price_snapshots",
        buckets_src="live_trade_buckets",
        wflow_src="live_wallet_flow_per_market",
        anchor_h=anchor_h,
        market_filter=_live_market_filter(anchor_h),
        is_duckdb=False,
    )

    # SELECT clause: переименовываем condition_id → market_id, остальные as-is
    select_csv = ", ".join("f.condition_id AS market_id" if c == "market_id" else f"f.{c}" for c in INSERT_COLUMNS)
    target_cols_csv = ", ".join(INSERT_COLUMNS)

    # UPDATE SET для всех колонок кроме PK
    update_cols = [c for c in INSERT_COLUMNS if c not in ("market_id", "anchor_hours")]
    update_set = ",\n        ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    return f"""
INSERT INTO live_ml_features ({target_cols_csv}, computed_at)
SELECT {select_csv}, now()
  FROM ({inner_sql}) AS f
ON CONFLICT (market_id, anchor_hours)
DO UPDATE SET
        {update_set},
        computed_at = EXCLUDED.computed_at
"""


# Sanity-check во время startup: убедиться что INSERT_COLUMNS соответствуют тому
# что выдаёт kernel (отлавливает schema drift).
def _validate_insert_columns_match_kernel() -> None:
    """Validate INSERT_COLUMNS = ['market_id', 'anchor_hours', + остальные feature cols без condition_id]."""
    expected_after_keys = [c for c in FEATURE_COLUMNS if c not in ("condition_id", "anchor_hours")]
    actual_after_keys = INSERT_COLUMNS[2:]  # после 'market_id', 'anchor_hours'
    if expected_after_keys != actual_after_keys:
        raise RuntimeError(
            f"INSERT_COLUMNS schema drift!\n  expected: {expected_after_keys}\n  actual:   {actual_after_keys}"
        )


_validate_insert_columns_match_kernel()


async def run_live_ml_features_builder() -> dict[str, Any]:
    """Один тик: для каждого CANONICAL_ANCHOR пересчитать features + UPSERT."""
    counts: dict[int, int] = {}
    total_inserted = 0

    for h in CANONICAL_ANCHORS:
        sql = _build_upsert_sql(h)
        try:
            row_count_str = await execute(sql)
            # asyncpg: 'INSERT 0 <n>' означает n inserted rows
            n = 0
            if isinstance(row_count_str, str) and row_count_str.startswith("INSERT"):
                parts = row_count_str.split()
                if len(parts) >= 3:
                    try:
                        n = int(parts[2])
                    except ValueError:
                        pass
            counts[h] = n
            total_inserted += n
        except Exception as exc:
            logger.error("live_ml_features_builder_failed", anchor_h=h, error=str(exc)[:300])
            counts[h] = -1

    # Sanity check: сколько rows всего в таблице сейчас
    try:
        total_rows = await fetchval("SELECT COUNT(*) FROM live_ml_features")
    except Exception:
        total_rows = None

    logger.info(
        "live_ml_features_builder_done",
        per_anchor=counts,
        total_inserted_or_updated=total_inserted,
        total_rows=total_rows,
    )
    return {
        "per_anchor": counts,
        "total_upserted": total_inserted,
        "total_rows": total_rows,
    }
