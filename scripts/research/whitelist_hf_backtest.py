"""Whitelist HF backtest — сравнение paper-edge vs HF historical.

PR4 из pre-live evidence pipeline. Закрывает риск historical: live_v2 paper-edge
может быть survivorship bias или модельные artifacts. Кросс-чек:

- live_v2: detector_signals.source='live_v2' (последние 30 дней с детекторов)
- hf_backfill: detector_signals.source='hf_backfill' (4 года HF parquet
  retrofit'нутых через скрипты в backfill_detector_signals.py)

Если whitelist bucket показывает похожие WR и net_ROI на live_v2 vs hf_backfill →
edge стабилен исторически. Расхождение >15pp WR → recent regime change или
overfitting на live данные.

Запуск:
  cd ~/oracle && docker exec oraclepoly-backend python scripts/research/whitelist_hf_backtest.py

Output: per-bucket таблица live_n / hf_n / live_WR / hf_WR / live_ROI / hf_ROI /
delta_WR / verdict (stable/unstable).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.database import close_db, fetch, init_db

WHITELIST = [
    ("D5_long_horizon_extreme_fade", "Politics", "lt40"),
    ("D4_small_market_fade", "Sports", "lt40"),
    ("D4_small_market_fade", "Sports", "60-80"),
    ("D4_small_market_fade", "Sports", "80-95"),
]


_BUCKET_SQL = """
WITH classified AS (
  SELECT
    ds.detector,
    COALESCE(m.category, 'unknown') AS category,
    CASE WHEN ds.current_price < 0.40 THEN 'lt40'
         WHEN ds.current_price < 0.60 THEN '40-60'
         WHEN ds.current_price < 0.80 THEN '60-80'
         WHEN ds.current_price < 0.95 THEN '80-95'
         ELSE 'gte95_blocked' END AS entry_zone,
    ds.event_id,
    ds.outcome,
    ds.realized_roi_net,
    ds.source
  FROM detector_signals ds
  LEFT JOIN markets m ON m.polymarket_id = ds.market_id
  WHERE ds.resolved = true AND ds.outcome IN ('WIN','LOSS')
)
SELECT
  detector, category, entry_zone, source,
  COUNT(*) AS n_signals,
  COUNT(DISTINCT event_id) AS n_events,
  AVG(CASE WHEN outcome='WIN' THEN 1.0 ELSE 0.0 END) AS wr,
  AVG(realized_roi_net) AS avg_roi
FROM classified
WHERE detector = $1 AND category = $2 AND entry_zone = $3
GROUP BY 1,2,3,4
"""


async def backtest_bucket(detector: str, category: str, zone: str) -> dict:
    rows = await fetch(_BUCKET_SQL, detector, category, zone)
    by_source = {r["source"]: dict(r) for r in rows}

    live = by_source.get("live_v2") or {}
    hf = by_source.get("hf_backfill") or {}

    live_n = int(live.get("n_signals") or 0)
    hf_n = int(hf.get("n_signals") or 0)
    live_n_ev = int(live.get("n_events") or 0)
    hf_n_ev = int(hf.get("n_events") or 0)
    live_wr = float(live.get("wr") or 0) * 100
    hf_wr = float(hf.get("wr") or 0) * 100
    live_roi = float(live.get("avg_roi") or 0)
    hf_roi = float(hf.get("avg_roi") or 0)
    delta_wr = round(live_wr - hf_wr, 1) if hf_n > 0 and live_n > 0 else None
    delta_roi = round(live_roi - hf_roi, 1) if hf_n > 0 and live_n > 0 else None

    if live_n == 0:
        verdict = "no_live_data"
    elif hf_n == 0:
        verdict = "no_hf_data"
    elif delta_wr is None:
        verdict = "no_comparison"
    elif abs(delta_wr) <= 10:
        verdict = "stable"
    elif abs(delta_wr) <= 20:
        verdict = "drift_warn"
    else:
        verdict = "unstable"

    return {
        "detector": detector,
        "category": category,
        "zone": zone,
        "live_n": live_n,
        "live_n_ev": live_n_ev,
        "hf_n": hf_n,
        "hf_n_ev": hf_n_ev,
        "live_wr": round(live_wr, 1),
        "hf_wr": round(hf_wr, 1),
        "live_roi": round(live_roi, 1),
        "hf_roi": round(hf_roi, 1),
        "delta_wr": delta_wr,
        "delta_roi": delta_roi,
        "verdict": verdict,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Все buckets, не только whitelist")
    args = parser.parse_args()

    await init_db()
    try:
        if args.full:
            buckets_to_check = await fetch(
                """
                SELECT DISTINCT
                  ds.detector,
                  COALESCE(m.category, 'unknown') AS category,
                  CASE WHEN ds.current_price < 0.40 THEN 'lt40'
                       WHEN ds.current_price < 0.60 THEN '40-60'
                       WHEN ds.current_price < 0.80 THEN '60-80'
                       WHEN ds.current_price < 0.95 THEN '80-95'
                       ELSE 'gte95_blocked' END AS zone
                FROM detector_signals ds
                LEFT JOIN markets m ON m.polymarket_id = ds.market_id
                WHERE ds.resolved=true AND ds.outcome IN ('WIN','LOSS')
                """
            )
            buckets = [(r["detector"], r["category"], r["zone"]) for r in buckets_to_check]
        else:
            buckets = WHITELIST

        results = []
        for det, cat, z in buckets:
            res = await backtest_bucket(det, cat, z)
            results.append(res)

        # Print
        print()
        print(f"{'Bucket':<55} {'live_n_ev':>10} {'hf_n_ev':>10} "
              f"{'live_WR':>8} {'hf_WR':>8} {'ΔWR':>6} {'verdict':>14}")
        print("=" * 130)
        for r in sorted(results, key=lambda x: -(x["live_n"] + x["hf_n"])):
            label = f"{r['detector']:<32}/{r['category']:<10}/{r['zone']}"
            delta_str = f"{r['delta_wr']:+.1f}" if r["delta_wr"] is not None else "-"
            print(
                f"{label[:55]:<55} {r['live_n_ev']:>10} {r['hf_n_ev']:>10} "
                f"{r['live_wr']:>7.1f}% {r['hf_wr']:>7.1f}% "
                f"{delta_str:>6} {r['verdict']:>14}"
            )
        print()
        print("Verdict legend:")
        print("  stable       — ΔWR ≤ 10pp, edge consistent live vs HF")
        print("  drift_warn   — 10pp < ΔWR ≤ 20pp, требуется внимание")
        print("  unstable     — ΔWR > 20pp, regime change OR overfitting")
        print("  no_*_data    — недостаточно данных для сравнения")

        # Whitelist gate summary
        if not args.full:
            print()
            print("Whitelist gate:")
            stable = sum(1 for r in results if r["verdict"] == "stable")
            print(f"  {stable}/{len(results)} buckets are 'stable' (live ≈ HF)")
            for r in results:
                marker = "✓" if r["verdict"] == "stable" else "✗"
                print(f"  {marker} {r['detector']}/{r['category']}/{r['zone']} → {r['verdict']}")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
