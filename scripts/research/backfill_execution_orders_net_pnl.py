"""One-off backfill: execution_orders.{trading_fee_usd, slippage_usd, net_pnl}
для legacy settled/filled orders (до Phase F).

Запускать ТОЛЬКО на проде:
    docker exec oraclepoly-backend python scripts/research/backfill_execution_orders_net_pnl.py [--dry-run]

После миграции 20260501215912_execution_orders_add_fee_net_pnl.sql старые settled
ордера (legacy live trades, ~5 шт) имеют pnl=gross но net_pnl=NULL. Дашборд через
COALESCE(net_pnl, realized_pnl_usd) показывает gross — visually корректно, но для
аналитики net_pnl должен быть populated.

virtual_filled (192 шт) — НЕ трогаем: они автоматически получат net_pnl при
settle через cron v2-execution-resolve когда привязанные detector_signals
зарезолвятся (logic в _settle_filled).

Формула — SSoT app.jobs.v2_execution_resolve._compute_settlement_pnl.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.jobs.v2_execution_resolve import _compute_settlement_pnl
from app.services.database import close_db, execute, fetch, init_db

_FETCH_SQL = """
SELECT eo.id,
       eo.outcome,
       eo.fill_price,
       eo.stake,
       COALESCE(m.category, 'other') AS category
  FROM execution_orders eo
  LEFT JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.status IN ('settled', 'virtual_settled')
   AND eo.net_pnl IS NULL
"""


_UPDATE_SQL = """
UPDATE execution_orders
   SET trading_fee_usd = data.fee,
       slippage_usd    = data.slippage,
       net_pnl         = data.net_pnl
  FROM (
    SELECT unnest($1::uuid[])    AS id,
           unnest($2::numeric[]) AS fee,
           unnest($3::numeric[]) AS slippage,
           unnest($4::numeric[]) AS net_pnl
  ) AS data
 WHERE execution_orders.id = data.id
"""


def _normalize_outcome(raw: str | None) -> str:
    """legacy outcome ('won'/'lost') → 'WIN'/'LOSS' (uppercase для _compute_settlement_pnl)."""
    if not raw:
        return ""
    r = raw.strip().lower()
    if r in ("won", "win"):
        return "WIN"
    if r in ("lost", "loss"):
        return "LOSS"
    return ""


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    await init_db()
    try:
        await _run(args.dry_run)
    finally:
        await close_db()


async def _run(dry_run: bool) -> None:
    rows = await fetch(_FETCH_SQL)
    if not rows:
        print("Nothing to backfill — all settled orders already have net_pnl.")
        return

    print(f"Found {len(rows)} settled orders without net_pnl. Computing...")

    ids: list = []
    fees: list[float] = []
    slippages: list[float] = []
    net_pnls: list[float] = []

    for r in rows:
        outcome = _normalize_outcome(r["outcome"])
        if outcome not in ("WIN", "LOSS"):
            print(f"  skip id={r['id']} outcome={r['outcome']!r} (not WIN/LOSS)")
            continue
        result = _compute_settlement_pnl(
            stake=float(r["stake"]),
            fill_price=float(r["fill_price"] or 0.0),
            outcome=outcome,
            category=r["category"],
        )
        ids.append(r["id"])
        fees.append(result["trading_fee_usd"])
        slippages.append(result["slippage_usd"])
        net_pnls.append(result["net_pnl"])
        print(
            f"  id={r['id']} outcome={outcome} stake={r['stake']} fill={r['fill_price']} "
            f"→ fee={result['trading_fee_usd']} slip={result['slippage_usd']} net={result['net_pnl']}"
        )

    if not ids:
        print("No eligible rows to update.")
        return

    if dry_run:
        print(f"\n[DRY] would update {len(ids)} rows.")
        return

    await execute(_UPDATE_SQL, ids, fees, slippages, net_pnls)
    print(f"\nUpdated {len(ids)} rows.")


if __name__ == "__main__":
    asyncio.run(main())
