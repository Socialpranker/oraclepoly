"""Backfill: пересчитать PnL для NO virtual EARLY_EXIT orders.

Bug (fixed 2026-05-17 в position_manager.py): _compute_early_exit_pnl и
_unrealized_pnl_pct трактовали fill_price для NO позиций как YES координату,
тогда как в БД (per detector_signal_service.py:42-47 + virtual_execution.py:135)
fill_price для NO = NO entry cost per share = 1 − yes_at_fill.

Эффект: PnL/net_pnl для 424 NO orders с outcome='EARLY_EXIT' переоценили
убытки и переоценили прибыли. Этот скрипт пересчитывает фиксированной формулой:
  shares    = stake / fill_price
  no_exit   = 1 − exit_price (exit_price = YES current_price из markets)
  gross_pnl = shares × no_exit − stake
  fees      = entry_fee(stake, fill_price) + exit_fee(exit_notional, no_exit)
  slippage  = 2 × stake × spread(category)
  net_pnl   = gross_pnl − fees − slippage

YES orders с EARLY_EXIT (n=68) НЕ затронуты багом — формула YES была корректной.
Settlement orders (WIN/LOSS, n=10) НЕ затронуты — _compute_settlement_pnl корректна.

Idempotent: безопасно запускать повторно, формула детерминирована.

Usage:
    python scripts/backfill_position_manager_no_pnl.py --dry-run
    python scripts/backfill_position_manager_no_pnl.py --apply
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
    eo.stake::float           AS stake,
    eo.fill_price::float      AS fill_price,
    eo.exit_price::float      AS exit_price,
    eo.pnl::float             AS old_pnl,
    eo.net_pnl::float         AS old_net_pnl,
    eo.trading_fee_usd::float AS old_fee,
    eo.slippage_usd::float    AS old_slip,
    COALESCE(m.category, 'other') AS category
  FROM execution_orders eo
  LEFT JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.execution_mode = 'virtual'
   AND eo.status         = 'virtual_settled'
   AND eo.outcome        = 'EARLY_EXIT'
   AND eo.side           = 'NO'
   AND eo.fill_price IS NOT NULL
   AND eo.exit_price IS NOT NULL
"""


_UPDATE_SQL = """
UPDATE execution_orders
   SET pnl              = $1,
       trading_fee_usd  = $2,
       slippage_usd     = $3,
       net_pnl          = $4
 WHERE id = $5
"""


def _compute_correct_pnl(
    *, stake: float, fill_price: float, exit_price: float, category: str
) -> dict[str, float]:
    """Корректная NO formula (SSoT — position_manager._compute_early_exit_pnl post-fix)."""
    from app.core.slippage import get_spread_estimate
    from app.services.fee_calculator import estimate_trading_fee

    cat = (category or "other").lower()
    eff_fill = max(fill_price, 0.001)
    shares = stake / eff_fill
    no_exit = max(1.0 - exit_price, 0.0)
    gross_pnl = shares * no_exit - stake

    entry_notional = stake
    exit_notional = shares * no_exit
    entry_price_for_fee = eff_fill
    exit_price_for_fee = max(no_exit, 0.001)

    fee_entry = estimate_trading_fee(entry_notional, entry_price_for_fee, cat)
    fee_exit = estimate_trading_fee(exit_notional, exit_price_for_fee, cat)
    fee_total = fee_entry + fee_exit

    slippage = 2.0 * stake * get_spread_estimate(cat)
    net_pnl = gross_pnl - fee_total - slippage

    return {
        "pnl": round(gross_pnl, 4),
        "fee": round(fee_total, 4),
        "slippage": round(slippage, 4),
        "net_pnl": round(net_pnl, 4),
    }


async def main(apply: bool) -> None:
    import asyncpg

    from app.core.config import get_settings

    settings = get_settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        rows = await conn.fetch(_FETCH_SQL)
        if not rows:
            print("No NO EARLY_EXIT orders found. Nothing to backfill.")
            return

        print(f"Found {len(rows)} NO EARLY_EXIT orders to backfill.\n")

        total_old_net = 0.0
        total_new_net = 0.0
        diffs = []
        per_detector: dict[str, dict[str, float]] = {}

        for r in rows:
            corrected = _compute_correct_pnl(
                stake=r["stake"],
                fill_price=r["fill_price"],
                exit_price=r["exit_price"],
                category=r["category"],
            )
            old_net = r["old_net_pnl"] or 0.0
            new_net = corrected["net_pnl"]
            delta = new_net - old_net

            total_old_net += old_net
            total_new_net += new_net
            diffs.append((r["detector"], r["id"], old_net, new_net, delta))

            d = per_detector.setdefault(
                r["detector"], {"n": 0, "old_net": 0.0, "new_net": 0.0, "delta": 0.0}
            )
            d["n"] += 1
            d["old_net"] += old_net
            d["new_net"] += new_net
            d["delta"] += delta

            if apply:
                await conn.execute(
                    _UPDATE_SQL,
                    corrected["pnl"],
                    corrected["fee"],
                    corrected["slippage"],
                    corrected["net_pnl"],
                    r["id"],
                )

        print(f"{'detector':<32} {'n':>4} {'old_net':>12} {'new_net':>12} {'delta':>12}")
        print("-" * 76)
        for det, d in sorted(per_detector.items(), key=lambda x: -x[1]["n"]):
            print(
                f"{det:<32} {int(d['n']):>4} "
                f"{d['old_net']:>12.2f} {d['new_net']:>12.2f} {d['delta']:>+12.2f}"
            )
        print("-" * 76)
        print(
            f"{'TOTAL':<32} {len(rows):>4} "
            f"{total_old_net:>12.2f} {total_new_net:>12.2f} "
            f"{total_new_net - total_old_net:>+12.2f}"
        )

        if apply:
            print(f"\n✓ Updated {len(rows)} rows.")
        else:
            print("\n(dry-run — БД не изменена. Запусти с --apply для записи.)")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Только показать диff, не записывать")
    group.add_argument("--apply", action="store_true", help="Применить UPDATE на проде")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
