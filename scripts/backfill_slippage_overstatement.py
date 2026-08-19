"""Backfill: пересчитать slippage и net_pnl для orders с buggy per-share formula.

Bug (fixed 2026-05-20): между 18-20 мая часть virtual_settled orders записаны
с формулой slippage = shares × half_spread, где half_spread это ДОЛЯ цены
(0.025=2.5%), не absolute $/share. На extreme zones (fill≈0.005) даёт
slippage > stake (D43: $3,333 на $400 stake).

Правильная формула (SSoT — pnl_formulas.compute_net_pnl post-fix):
  EARLY_EXIT (2 leg): slippage = 2 × stake × half_spread
  WIN/LOSS  (1 leg): slippage = 1 × stake × half_spread

Detection (per-share buggy vs stake-based correct):
  - Если |slip_db - shares×half_spread×legs| < tolerance → buggy
  - Иначе → уже stake-based (правильная), пропускаем

Idempotent: безопасно запускать повторно.

Usage:
    python scripts/backfill_slippage_overstatement.py --dry-run
    python scripts/backfill_slippage_overstatement.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


_FETCH_SQL = """
SELECT
    eo.id,
    eo.detector,
    eo.side,
    eo.outcome,
    eo.exit_reason,
    eo.stake::float          AS stake,
    eo.fill_price::float     AS fill_price,
    eo.filled_qty::float     AS qty,
    eo.pnl::float            AS pnl_db,
    eo.trading_fee_usd::float AS fees_db,
    eo.slippage_usd::float   AS slip_db,
    eo.net_pnl::float        AS net_db,
    COALESCE(m.category, 'other') AS category
  FROM execution_orders eo
  LEFT JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.execution_mode = 'virtual'
   AND eo.status         = 'virtual_settled'
   AND (eo.exit_reason IS NULL OR eo.exit_reason NOT LIKE '%VOIDED%')
   AND eo.filled_qty IS NOT NULL
   AND eo.fill_price IS NOT NULL
   AND eo.outcome IS NOT NULL
"""

_UPDATE_SQL = """
UPDATE execution_orders
   SET slippage_usd = $1,
       net_pnl      = $2
 WHERE id = $3
"""


# Mirror app.core.slippage._HALF_SPREAD
_HALF_SPREAD = {
    "sports": 0.020,
    "politics": 0.025,
    "crypto": 0.030,
    "culture": 0.030,
    "finance": 0.025,
    "tech & ai": 0.030,
    "weather": 0.035,
    "world & economy": 0.030,
    "geopolitics": 0.030,
}
_DEFAULT_HALF_SPREAD = 0.030


def _half_spread(category: str) -> float:
    return _HALF_SPREAD.get((category or "other").lower(), _DEFAULT_HALF_SPREAD)


def _legs_for_outcome(outcome: str) -> float:
    """2 legs для EARLY_EXIT (продажа в книгу), 1 leg для WIN/LOSS (резолюция)."""
    return 2.0 if outcome == "EARLY_EXIT" else 1.0


async def main(apply: bool) -> None:
    import asyncpg

    from app.core.config import get_settings

    settings = get_settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        rows = await conn.fetch(_FETCH_SQL)
        if not rows:
            print("No settled orders found.")
            return

        print(f"Checking {len(rows)} settled orders for slippage overstatement.\n")

        buggy: list[dict] = []
        for r in rows:
            stake = r["stake"]
            qty = r["qty"]
            half = _half_spread(r["category"])
            legs = _legs_for_outcome(r["outcome"])
            slip_correct = legs * stake * half
            slip_buggy = legs * qty * half
            slip_db = r["slip_db"] or 0.0

            # Detect: is current value closer to buggy or correct?
            diff_to_buggy = abs(slip_db - slip_buggy)
            diff_to_correct = abs(slip_db - slip_correct)
            # Tolerance: 1% of buggy or $0.50
            if diff_to_buggy < max(0.01 * slip_buggy, 0.50) and diff_to_buggy < diff_to_correct:
                # New net = old net + (slip_db - slip_correct)
                # since net = pnl - fees - slippage
                net_db = r["net_db"] or 0.0
                new_slip = slip_correct
                new_net = net_db + (slip_db - slip_correct)
                buggy.append(
                    {
                        "id": r["id"],
                        "detector": r["detector"],
                        "stake": stake,
                        "slip_db": slip_db,
                        "slip_correct": new_slip,
                        "net_db": net_db,
                        "new_net": new_net,
                    }
                )

        if not buggy:
            print("No buggy slippage found. Database is consistent.")
            return

        per_det: dict[str, dict[str, float]] = {}
        for b in buggy:
            d = per_det.setdefault(
                b["detector"],
                {"n": 0, "slip_db": 0.0, "slip_correct": 0.0, "net_db": 0.0, "new_net": 0.0},
            )
            d["n"] += 1
            d["slip_db"] += b["slip_db"]
            d["slip_correct"] += b["slip_correct"]
            d["net_db"] += b["net_db"]
            d["new_net"] += b["new_net"]

        print(f"{'detector':<32} {'n':>4} {'slip_db':>10} {'slip_new':>10} {'net_db':>10} {'new_net':>10} {'delta':>10}")
        print("-" * 92)
        total_slip_db = 0.0
        total_slip_correct = 0.0
        total_net_db = 0.0
        total_new_net = 0.0
        for det, d in sorted(per_det.items(), key=lambda x: -(x[1]["slip_db"] - x[1]["slip_correct"])):
            delta = d["new_net"] - d["net_db"]
            print(
                f"{det:<32} {int(d['n']):>4} "
                f"{d['slip_db']:>10.2f} {d['slip_correct']:>10.2f} "
                f"{d['net_db']:>10.2f} {d['new_net']:>10.2f} {delta:>+10.2f}"
            )
            total_slip_db += d["slip_db"]
            total_slip_correct += d["slip_correct"]
            total_net_db += d["net_db"]
            total_new_net += d["new_net"]
        print("-" * 92)
        print(
            f"{'TOTAL':<32} {len(buggy):>4} "
            f"{total_slip_db:>10.2f} {total_slip_correct:>10.2f} "
            f"{total_net_db:>10.2f} {total_new_net:>10.2f} "
            f"{total_new_net - total_net_db:>+10.2f}"
        )

        if apply:
            for b in buggy:
                await conn.execute(
                    _UPDATE_SQL,
                    round(b["slip_correct"], 4),
                    round(b["new_net"], 4),
                    b["id"],
                )
            print(f"\n✓ Updated {len(buggy)} rows.")
        else:
            print("\n(dry-run — БД не изменена. Запусти с --apply.)")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
