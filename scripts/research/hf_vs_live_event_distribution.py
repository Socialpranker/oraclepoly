"""EDA: сравнение распределений archetype/event_size между HF (pre-V2) и live (post-V2).

Stage 0 / Two-Stage ML Pipeline.

Если archetype shift > 10pp между регимами → фичи не переносятся, нужен per-regime fit.
Если live n_signals < 100 → недостаточно для distribution check, defer to Stage 5.

Запускать ПОСЛЕ scripts/backfill_detector_signals_event_meta.py.

Usage:
    docker exec -i oraclepoly-backend python scripts/research/hf_vs_live_event_distribution.py
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, "/app")
sys.path.insert(0, ".")

from app.services.database import close_db, fetch, init_db  # noqa: E402

V2_CUTOVER = datetime(2026, 4, 28, tzinfo=UTC)

_DIST_SQL = """
SELECT
    CASE WHEN generated_at < $1 THEN 'hf_pre_v2' ELSE 'live_post_v2' END AS regime,
    archetype,
    event_size,
    COUNT(*) AS n
  FROM detector_signals
 WHERE resolved = true
   AND event_size IS NOT NULL
   AND archetype IS NOT NULL
 GROUP BY regime, archetype, event_size
 ORDER BY regime, archetype, event_size
"""


async def run() -> dict[str, Any]:
    await init_db(min_size=1, max_size=2)
    try:
        rows = await fetch(_DIST_SQL, V2_CUTOVER)
    finally:
        await close_db()

    if not rows:
        print("⚠️  No resolved detector_signals with event_size+archetype populated.")
        print("    Run scripts/backfill_detector_signals_event_meta.py first.")
        return {"status": "no_data"}

    by_regime: dict[str, dict[tuple[str, int], int]] = {"hf_pre_v2": {}, "live_post_v2": {}}
    sizes_by_regime: dict[str, list[int]] = defaultdict(list)
    totals = {"hf_pre_v2": 0, "live_post_v2": 0}
    for r in rows:
        regime = r["regime"]
        archetype = r["archetype"]
        size = int(r["event_size"])
        n = int(r["n"])
        by_regime[regime][(archetype, size)] = n
        sizes_by_regime[regime].extend([size] * n)
        totals[regime] += n

    print("\n=== HF vs Live distribution check ===")
    print(f"HF (pre-V2):    n_signals = {totals['hf_pre_v2']:,}")
    print(f"Live (post-V2): n_signals = {totals['live_post_v2']:,}")

    if totals["hf_pre_v2"] == 0 and totals["live_post_v2"] == 0:
        return {"status": "no_data"}

    print("\n--- Archetype share (%) ---")
    print(f"{'archetype':<30} {'hf_pre_v2':>12} {'live_post_v2':>15} {'shift_pp':>10}")
    archetypes = sorted({k[0] for v in by_regime.values() for k in v})
    alerts: list[str] = []
    for a in archetypes:
        hf_n = sum(n for (arch, _), n in by_regime["hf_pre_v2"].items() if arch == a)
        live_n = sum(n for (arch, _), n in by_regime["live_post_v2"].items() if arch == a)
        hf_pct = 100 * hf_n / max(totals["hf_pre_v2"], 1)
        live_pct = 100 * live_n / max(totals["live_post_v2"], 1)
        shift = abs(hf_pct - live_pct)
        flag = "  ⚠️ shift>10pp" if shift > 10.0 else ""
        print(f"{a:<30} {hf_pct:>11.1f}% {live_pct:>14.1f}% {shift:>9.1f}{flag}")
        if shift > 10.0 and totals["live_post_v2"] >= 100:
            alerts.append(f"{a}: shift {shift:.1f}pp")

    print("\n--- event_size summary ---")
    for regime, sizes in sizes_by_regime.items():
        if not sizes:
            print(f"{regime}: no data")
            continue
        avg = sum(sizes) / len(sizes)
        sizes_sorted = sorted(sizes)
        median = sizes_sorted[len(sizes_sorted) // 2]
        print(f"{regime}: n={len(sizes):,}, mean={avg:.2f}, median={median}, min={min(sizes)}, max={max(sizes)}")

    if totals["live_post_v2"] < 100:
        print(f"\nℹ️  Live n_signals={totals['live_post_v2']} < 100 — distribution check deferred to Stage 5")
        return {"status": "deferred", "totals": totals, "alerts": alerts}

    if alerts:
        print(f"\n⚠️  REGIME DRIFT DETECTED: {len(alerts)} archetype(s) shifted >10pp")
        for a in alerts:
            print(f"   - {a}")
        return {"status": "drift", "totals": totals, "alerts": alerts}

    print("\n✅ Distribution stable between HF and Live regimes")
    return {"status": "ok", "totals": totals, "alerts": []}


if __name__ == "__main__":
    asyncio.run(run())
