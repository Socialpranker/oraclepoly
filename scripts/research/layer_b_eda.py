"""EDA для Layer B universe + sample_weight distribution.

Stage 3 / Two-Stage ML Pipeline.

Проверяем:
- Сколько rows после Layer A фильтра (p_event > 0.5)
- Распределение event_size — sample_weight=1/event_size sanity
- 1/event_size vs 1/sqrt(event_size) total weights — какой выбрать

Usage:
    docker exec -i oraclepoly-backend python scripts/research/layer_b_eda.py
"""

from __future__ import annotations

import asyncio
import math
import statistics
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, ".")

from app.services.database import close_db, fetch, init_db  # noqa: E402

_SQL = """
SELECT ds.event_size, ds.archetype, COUNT(*) AS n_rows
  FROM detector_signals ds
 WHERE ds.resolved = true
   AND ds.outcome IN ('WIN', 'LOSS')
   AND ds.event_size IS NOT NULL
 GROUP BY ds.event_size, ds.archetype
 ORDER BY ds.event_size
"""


async def run() -> None:
    await init_db(min_size=1, max_size=2)
    try:
        rows = await fetch(_SQL)
    finally:
        await close_db()

    if not rows:
        print("No resolved signals with event_size populated. Run Stage 0 backfill first.")
        return

    sizes = []
    for r in rows:
        sizes.extend([int(r["event_size"])] * int(r["n_rows"]))
    n = len(sizes)
    print(f"Total rows: {n:,}")
    print(
        f"event_size: min={min(sizes)}, median={statistics.median(sizes)}, "
        f"mean={statistics.mean(sizes):.2f}, max={max(sizes)}"
    )

    print("\nDistribution histogram:")
    bins = [(1, 1), (2, 3), (4, 7), (8, 15), (16, 31), (32, 999)]
    for lo, hi in bins:
        n_in = sum(1 for s in sizes if lo <= s <= hi)
        bar = "#" * int(50 * n_in / n)
        print(f"  [{lo:3d}-{hi:3d}]: {n_in:6d} ({100 * n_in / n:5.1f}%) {bar}")

    print("\nWeight comparison:")
    print(f"  sum(1/event_size)         = {sum(1 / s for s in sizes):.1f}")
    print(f"  sum(1/sqrt(event_size))   = {sum(1 / math.sqrt(s) for s in sizes):.1f}")
    print(f"  sum(uniform 1.0)          = {n:.1f}")
    print("\nIf 1/event_size weights collapse too fast → consider 1/sqrt as fallback")


if __name__ == "__main__":
    asyncio.run(run())
