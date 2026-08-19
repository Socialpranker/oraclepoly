"""G18 Calibration Edge — weekly refit job.

Пересчитывает detector_calibration_bins из detector_signals last 90d.

Для каждого (detector, category, price_bin):
  - n_observations, n_wins
  - wr_observed = wins / n
  - Wilson 95% CI (lower, upper)

Категория для market_id берётся из markets table, fallback на 'unknown'.
В отличие от replay scripta — здесь production cron не использует canonical
parquet (не зависим от disk mount); resolved-archived markets теряют category,
попадают в 'unknown' bin.

Только живые детекторы: INNER JOIN detector_state отсекает killed/удалённые
(их строки удалены из detector_state, но historical detector_signals остаются).

Cron: weekly Sunday 04:00 UTC (после еженедельного maintenance window).
Idempotency: TRUNCATE + INSERT (full refit), не INSERT ON CONFLICT — гарантирует
что stale rows удалены.

См. docs/specs/G18_calibration_edge.md.
"""

from __future__ import annotations

import math
from typing import Any

import structlog

from app.services.database import execute, fetch, fetchval

logger = structlog.get_logger(__name__)

# Окно для refit — 90 дней resolved signals.
REFIT_WINDOW_DAYS: int = 90

# Минимум samples в bin для inclusion. Меньше — calibration unreliable.
MIN_OBSERVATIONS: int = 30

# Price bin step. 10 bins (0.0..0.9), price floored.
N_BINS: int = 10


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI для бинарной пропорции."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


_AGGREGATE_SQL = """
SELECT
    ds.detector,
    COALESCE(m.category, 'unknown') AS category,
    FLOOR(ds.current_price * $1) / $1 AS price_bin,
    COUNT(*)::int AS n_observations,
    COUNT(*) FILTER (WHERE ds.outcome = 'WIN')::int AS n_wins,
    MIN(ds.generated_at) AS window_start,
    MAX(ds.generated_at) AS window_end
  FROM detector_signals ds
  -- INNER JOIN отсекает killed/удалённые детекторы: их строки удалены из
  -- detector_state, но historical detector_signals остаются → без фильтра
  -- refit насыпает мёртвые bin'ы (D5/D7b/D30/D31/D32/D33). Фильтр по
  -- существованию, НЕ по stage — иначе выпали бы живые shadow-детекторы
  -- (D36/D10b), которые как раз и питают G18.
  JOIN detector_state dst ON dst.detector = ds.detector
  LEFT JOIN markets m ON m.polymarket_id = ds.market_id
 WHERE ds.generated_at >= now() - ($2 || ' days')::interval
   AND ds.resolved = true
   AND ds.outcome IN ('WIN', 'LOSS')
   AND ds.current_price IS NOT NULL
   AND ds.current_price > 0
   AND ds.current_price < 1
 GROUP BY ds.detector, COALESCE(m.category, 'unknown'), FLOOR(ds.current_price * $1) / $1
HAVING COUNT(*) >= $3
"""

_TRUNCATE_SQL = "TRUNCATE TABLE detector_calibration_bins"

_INSERT_SQL = """
INSERT INTO detector_calibration_bins (
    detector, category, price_bin,
    n_observations, n_wins, wr_observed,
    wilson_lower_95, wilson_upper_95,
    sample_window_start, sample_window_end, refit_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
"""


async def run_calibration_bins_refit() -> dict[str, Any]:
    """Refit detector_calibration_bins из detector_signals last REFIT_WINDOW_DAYS."""
    rows = await fetch(_AGGREGATE_SQL, N_BINS, str(REFIT_WINDOW_DAYS), MIN_OBSERVATIONS)
    if not rows:
        logger.warning("calibration_bins_refit_no_data", window_days=REFIT_WINDOW_DAYS)
        return {"n_bins": 0, "status": "no_data"}

    logger.info("calibration_bins_refit_aggregated", n_bins=len(rows))

    # Build insert tuples с Wilson CIs
    insert_rows = []
    for r in rows:
        wr = r["n_wins"] / r["n_observations"] if r["n_observations"] > 0 else 0.0
        lo, hi = wilson_ci(r["n_wins"], r["n_observations"])
        insert_rows.append(
            (
                r["detector"],
                r["category"],
                float(r["price_bin"]),
                r["n_observations"],
                r["n_wins"],
                round(wr, 4),
                round(lo, 4),
                round(hi, 4),
                r["window_start"],
                r["window_end"],
            )
        )

    # TRUNCATE + INSERT for clean refit
    await execute(_TRUNCATE_SQL)
    inserted = 0
    for row in insert_rows:
        await execute(_INSERT_SQL, *row)
        inserted += 1

    n_total_bin_rows = await fetchval("SELECT COUNT(*)::int FROM detector_calibration_bins") or 0

    logger.info(
        "calibration_bins_refit_done",
        n_bins=inserted,
        n_total_in_table=n_total_bin_rows,
        window_days=REFIT_WINDOW_DAYS,
    )

    return {
        "n_bins": inserted,
        "n_total_in_table": int(n_total_bin_rows),
        "window_days": REFIT_WINDOW_DAYS,
        "status": "ok",
    }
