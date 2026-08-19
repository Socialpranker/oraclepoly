"""Top-1 per-event accuracy на active Layer B модели.

Stage 3 / Two-Stage ML Pipeline.

Sanity check: top1 acc должен быть > 1/n_markets_avg
(better than random within event).

Usage:
    docker exec -i oraclepoly-backend python scripts/research/layer_b_top1_per_event.py
"""

from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, ".")

from app.services.database import close_db, fetchrow, init_db  # noqa: E402


async def run() -> None:
    await init_db(min_size=1, max_size=2)
    try:
        row = await fetchrow(
            """SELECT version, top1_per_event_accuracy, n_events_holdout, per_category_metrics,
                      hf_holdout_brier, live_holdout_brier, hf_n_events, live_n_events,
                      holdout_per_event_brier
                 FROM ml_market_picker WHERE is_active = true"""
        )
    finally:
        await close_db()

    if row is None:
        print("⚠️  No active Layer B model. Run ml-market-picker-fit first.")
        return

    print(f"Version: {row['version']}")
    print(f"holdout_per_event_brier: {float(row['holdout_per_event_brier']):.4f}")
    print(f"top1 per-event accuracy: {float(row['top1_per_event_accuracy']):.4f}")
    print(f"n_events holdout: {row['n_events_holdout']}")
    print()
    print("HF vs Live (если HF gate активен):")
    hf = row["hf_holdout_brier"]
    live = row["live_holdout_brier"]
    if hf is not None:
        print(f"  HF:   brier={float(hf):.4f}, n={row['hf_n_events']}")
    if live is not None:
        print(f"  Live: brier={float(live):.4f}, n={row['live_n_events']}")
        if hf is not None and row["live_n_events"] >= 30:
            ratio = float(live) / float(hf) if float(hf) > 0 else float("inf")
            print(f"  ratio: {ratio:.3f} ({'OK' if ratio <= 1.10 else 'BLOCKS PROMOTION'})")

    cats = row["per_category_metrics"]
    if isinstance(cats, str):
        cats = json.loads(cats)
    print("\nPer-category brier:")
    for cat, m in sorted(cats.items(), key=lambda x: x[1]["brier"]):
        flag = ""
        if cat in ("Tech & AI", "World & Economy") and m["brier"] > 0.25 and m["n_events"] >= 10:
            flag = "  ⚠️ unsafe (blocks promotion)"
        print(
            f"  {cat:20s} brier={m['brier']:.4f}  n_events={m['n_events']:4d}  "
            f"win_rate={m['win_rate']:.3f}{flag}"
        )


if __name__ == "__main__":
    asyncio.run(run())
