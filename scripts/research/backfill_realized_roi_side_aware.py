"""Backfill realized_roi и realized_roi_net по side-aware формуле.

Формула до фикса (баг):
  WIN: roi = (1-current_price)/current_price * 100      # YES-формула, применялась ко ВСЕМ
  LOSS: roi = -100

После фикса (side-aware):
  entry  = current_price (YES) | 1 - current_price (NO)
  WIN: roi = (1/entry - 1) * 100
  LOSS: roi = -100

Запуск (на VPS):
  cd ~/oracle && python scripts/research/backfill_realized_roi_side_aware.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import asyncpg  # noqa: E402

# Загружаем .env
env_path = Path(__file__).parent.parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set", file=sys.stderr)
    sys.exit(1)


def _entry_price_for_side(current_price: float, side: str) -> float:
    if side == "YES":
        return current_price
    return 1.0 - current_price


def _realized_roi_pct(outcome: str, current_price: float, side: str) -> float:
    if outcome == "WIN":
        entry = _entry_price_for_side(current_price, side)
        if entry <= 0:
            return 0.0
        return round((1.0 / entry - 1.0) * 100.0, 3)
    if outcome == "LOSS":
        return -100.0
    return 0.0


def _realized_roi_net_pct(
    outcome: str, current_price: float, side: str, category: str | None
) -> float:
    """Дублирует логику app/jobs/detector_signals_resolve.py:_realized_roi_net_pct.

    Canonical formula: ROI_net = net_pnl / notional × 100
    где notional = stake / entry (при stake=1: notional = 1/entry).

    BUG FIX 2026-05-14: ранее знаменатель был /1.0 (per-stake), что несовместимо
    с production resolve. Приводило к ROI compression mismatch на extreme entries.
    """
    from app.core.slippage import get_spread_estimate
    from app.services.fee_calculator import estimate_trading_fee

    if outcome not in ("WIN", "LOSS"):
        return 0.0

    entry = _entry_price_for_side(current_price, side)
    if entry <= 0:
        return 0.0

    cat = (category or "other").lower()
    eff = max(entry, 0.001)
    payout = 1.0 / eff - 1.0
    notional = 1.0 / eff  # = stake × (1/entry), stake = $1
    gross_notional = 1.0 * (1.0 + payout)
    fee_one_leg = estimate_trading_fee(gross_notional, eff, cat)
    slippage = 1.0 * get_spread_estimate(cat)

    if outcome == "WIN":
        net_pnl = 1.0 * payout - 2.0 * fee_one_leg - slippage
    else:
        net_pnl = -(1.0 + fee_one_leg + slippage)

    return round(net_pnl / notional * 100.0, 3)


_SELECT_SQL = """
SELECT ds.id, ds.outcome, ds.current_price, ds.side, m.category
  FROM detector_signals ds
  LEFT JOIN markets m ON m.polymarket_id = ds.market_id
 WHERE ds.outcome IN ('WIN', 'LOSS')
   AND ds.id > $1
 ORDER BY ds.id
 LIMIT $2
"""

_UPDATE_SQL = """
UPDATE detector_signals
   SET realized_roi      = data.roi,
       realized_roi_net  = data.roi_net
  FROM (
    SELECT
      unnest($1::uuid[])    AS id,
      unnest($2::numeric[]) AS roi,
      unnest($3::numeric[]) AS roi_net
  ) AS data
 WHERE detector_signals.id = data.id
"""


async def backfill(dry_run: bool, limit: int | None) -> None:
    dsn = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
        "asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(dsn)

    total_updates = 0
    total_changes = 0
    last_id: str | None = None
    batch_size = 5000
    t0 = time.time()

    try:
        while True:
            rows = await conn.fetch(
                _SELECT_SQL,
                last_id if last_id else "00000000-0000-0000-0000-000000000000",
                batch_size,
            )
            if not rows:
                break

            ids: list = []
            rois: list[float] = []
            rois_net: list[float] = []
            for r in rows:
                outcome = r["outcome"]
                current_price = float(r["current_price"])
                side = r["side"]
                category = r["category"]

                roi = _realized_roi_pct(outcome, current_price, side)
                roi_net = _realized_roi_net_pct(outcome, current_price, side, category)

                ids.append(r["id"])
                rois.append(roi)
                rois_net.append(roi_net)

            if dry_run:
                print(
                    f"  DRY-RUN: would update {len(ids)} rows, "
                    f"sample: id={ids[0]} roi={rois[0]:.2f} net={rois_net[0]:.2f}",
                    flush=True,
                )
            else:
                await conn.execute(_UPDATE_SQL, ids, rois, rois_net)
                total_updates += len(ids)

            total_changes += len(ids)
            last_id = str(rows[-1]["id"])

            if total_changes % (batch_size * 10) == 0 or len(rows) < batch_size:
                elapsed = time.time() - t0
                print(
                    f"  Processed {total_changes:,} rows in {elapsed:.1f}s "
                    f"({total_changes/elapsed:.0f}/s)",
                    flush=True,
                )

            if limit is not None and total_changes >= limit:
                print(f"  Reached --limit {limit}, stopping", flush=True)
                break

    finally:
        await conn.close()

    elapsed = time.time() - t0
    print(
        f"\nDone. {total_changes:,} rows scanned, "
        f"{total_updates:,} updated in {elapsed:.1f}s "
        f"(dry_run={dry_run})"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Max rows to process")
    args = parser.parse_args()

    asyncio.run(backfill(args.dry_run, args.limit))


if __name__ == "__main__":
    main()
