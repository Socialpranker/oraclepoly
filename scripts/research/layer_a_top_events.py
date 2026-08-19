"""Top-100 events by p_event с реальными outcomes — sanity check Layer A.

Stage 2 / Two-Stage ML Pipeline.

Если top-10 events имеют 9 wins → модель работает.
Если 5/5 → монета (модель не научилась).

Usage:
    docker exec -i oraclepoly-backend python scripts/research/layer_a_top_events.py
"""

from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, ".")

from app.calibration.event_selector import EventSelectorExample, deserialize_from_json  # noqa: E402
from app.services.database import close_db, fetch, fetchrow, init_db  # noqa: E402

_HOLDOUT_SQL = """
WITH event_signals AS (
    SELECT ds.event_id              AS event_polymarket_id,
           COUNT(*)                  AS n_detector_signals,
           MIN(ds.generated_at)      AS first_signal_at
      FROM detector_signals ds
     WHERE ds.event_id IS NOT NULL AND ds.resolved = true
     GROUP BY ds.event_id
)
SELECT e.polymarket_id              AS event_polymarket_id,
       e.category,
       e.archetype,
       COALESCE(e.volume_24h, 0)     AS volume_24h,
       COALESCE(e.liquidity, 0)      AS liquidity,
       COALESCE(e.neg_risk, false)   AS neg_risk,
       COALESCE(e.neg_risk_other, false) AS neg_risk_other,
       COALESCE(e.n_yes_winners, 1)  AS n_yes_winners,
       COUNT(m.id)                   AS n_markets,
       AVG(m.current_price)          AS mean_market_price,
       STDDEV_POP(m.current_price)   AS market_price_dispersion,
       es.n_detector_signals,
       EXTRACT(EPOCH FROM (e.end_date - es.first_signal_at)) / 86400.0 AS days_to_resolution,
       mvo.event_roi
  FROM events e
  JOIN markets m ON m.event_id = e.id
  JOIN event_signals es ON es.event_polymarket_id = e.polymarket_id
  JOIN mv_event_outcome mvo ON mvo.event_id = e.polymarket_id
 GROUP BY e.id, e.polymarket_id, e.category, e.archetype, e.volume_24h,
          e.liquidity, e.neg_risk, e.neg_risk_other, e.n_yes_winners, e.end_date,
          es.n_detector_signals, es.first_signal_at, mvo.event_roi
"""


async def run() -> None:
    await init_db(min_size=1, max_size=2)
    try:
        active = await fetchrow("SELECT model_payload FROM ml_event_selector WHERE is_active = true")
        if active is None:
            print("⚠️  No active Layer A model. Run ml-event-selector-fit first.")
            return

        payload = active["model_payload"]
        payload_json = payload if isinstance(payload, str) else json.dumps(payload)
        model = deserialize_from_json(payload_json)

        rows = await fetch(_HOLDOUT_SQL)
    finally:
        await close_db()

    if not rows:
        print("No events in mv_event_outcome")
        return

    examples = []
    metas = []
    for r in rows:
        ex = EventSelectorExample(
            event_id=str(r["event_polymarket_id"]),
            category=r["category"],
            archetype=r["archetype"] or "unclassified",
            n_markets=int(r["n_markets"] or 1),
            volume_24h=float(r["volume_24h"] or 0),
            liquidity=float(r["liquidity"] or 0),
            days_to_resolution=float(r["days_to_resolution"] or 0),
            mean_market_price=float(r["mean_market_price"] or 0),
            market_price_dispersion=float(r["market_price_dispersion"] or 0),
            neg_risk=bool(r["neg_risk"]),
            neg_risk_other=bool(r["neg_risk_other"]),
            n_yes_winners=int(r["n_yes_winners"]),
            n_detector_signals=int(r["n_detector_signals"]),
            outcome=1 if (r["event_roi"] or 0) > 0 else 0,
        )
        examples.append(ex)
        metas.append({
            "event_id": ex.event_id,
            "category": ex.category,
            "archetype": ex.archetype,
            "real_outcome": ex.outcome,
            "event_roi": float(r["event_roi"] or 0),
        })

    preds = model.predict(examples)
    enriched = sorted(zip(metas, preds, strict=True), key=lambda x: -x[1])[:100]
    base_rate = sum(1 for m in metas if m["real_outcome"] == 1) / len(metas)

    n_top = len(enriched)
    n_correct = sum(1 for m, _ in enriched if m["real_outcome"] == 1)
    print(f"\nTop-{n_top} events by p_event:")
    print(f"  Predictions correct: {n_correct}/{n_top} ({100 * n_correct / n_top:.1f}%)")
    print(f"  Vs base rate: {100 * base_rate:.1f}%")
    if 100 * n_correct / n_top > 100 * base_rate + 5:
        print("  ✅ Model precision > base rate (Layer A работает)")
    else:
        print("  ⚠️  Model precision close to base rate (модель почти-монета)")

    print("\nTop-10 events:")
    for m, p in enriched[:10]:
        cat = m["category"] or "?"
        arch = m["archetype"] or "?"
        roi = m["event_roi"]
        print(
            f"  p_event={p:.3f}  outcome={m['real_outcome']}  "
            f"roi={roi:+.3f}  {cat:12s}  {arch:25s}  {m['event_id']}"
        )


if __name__ == "__main__":
    asyncio.run(run())
