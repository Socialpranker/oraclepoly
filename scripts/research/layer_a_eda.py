"""EDA для Layer A universe.

Stage 2 / Two-Stage ML Pipeline.

Проверяем:
- Сколько events в universe (mv_event_outcome)
- % положительных (event_roi > 0) — class balance
- Распределение n_markets, archetype, category

Usage:
    docker exec -i oraclepoly-backend python scripts/research/layer_a_eda.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, "/app")
sys.path.insert(0, ".")

from app.services.database import close_db, fetch, init_db  # noqa: E402

_SQL = """
SELECT mvo.event_id,
       mvo.event_roi,
       mvo.n_markets,
       mvo.archetype,
       e.volume_24h,
       e.liquidity,
       e.category
  FROM mv_event_outcome mvo
  JOIN events e ON e.polymarket_id = mvo.event_id
"""


async def run() -> None:
    await init_db(min_size=1, max_size=2)
    try:
        rows = await fetch(_SQL)
    finally:
        await close_db()

    n = len(rows)
    if n == 0:
        print("No events in universe (mv_event_outcome empty)")
        return

    n_pos = sum(1 for r in rows if (r["event_roi"] or 0) > 0)
    print(f"Universe size: {n:,} events")
    print(f"Positive (event_roi > 0): {n_pos:,} ({100 * n_pos / n:.1f}%)")
    print(f"Negative: {n - n_pos:,} ({100 * (n - n_pos) / n:.1f}%)")

    if 0.05 < (n_pos / n) < 0.95:
        print("✅ Class balance OK (5%-95%)")
    else:
        print(f"⚠️  Class imbalance: {100 * n_pos / n:.1f}% positive — adjust threshold or use class_weight")

    n_markets_list = [int(r["n_markets"] or 0) for r in rows]
    if n_markets_list:
        print(
            f"\nn_markets: min={min(n_markets_list)}, "
            f"median={statistics.median(n_markets_list)}, "
            f"mean={statistics.mean(n_markets_list):.1f}, "
            f"max={max(n_markets_list)}"
        )

    by_arch: dict[str, int] = defaultdict(int)
    for r in rows:
        a = r["archetype"] or "unknown"
        by_arch[a] += 1
    print("\nArchetype distribution:")
    for a, k in sorted(by_arch.items(), key=lambda x: -x[1]):
        print(f"  {a:30s}: {k:5d} ({100 * k / n:5.1f}%)")

    by_cat: dict[str, int] = defaultdict(int)
    for r in rows:
        c = r["category"] or "Unknown"
        by_cat[c] += 1
    print("\nCategory distribution:")
    for c, k in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"  {c:30s}: {k:5d} ({100 * k / n:5.1f}%)")


if __name__ == "__main__":
    asyncio.run(run())
