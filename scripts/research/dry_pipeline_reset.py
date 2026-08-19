#!/usr/bin/env python3
"""Dry Pipeline Reset — void все open позиции, reset bankroll к initial.

One-time script. Запускать ОДИН раз при переходе на honest dry architecture.
После запуска dry pipeline начинает с чистого листа.

Usage:
    python scripts/research/dry_pipeline_reset.py [--dry-run]

Options:
    --dry-run       Show what would be changed without modifying data
"""

import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dry_pipeline_reset")


async def main(dry_run: bool = False) -> None:
    """Reset dry pipeline state."""
    from app.services.database import execute, fetch, fetchval

    logger.info(f"Starting dry pipeline reset (dry_run={dry_run})")

    # 1. Count and void all open dry positions
    open_count = await fetchval(
        "SELECT COUNT(*) FROM strategy_positions WHERE status = 'open' AND mode = 'dry'"
    )
    logger.info(f"Found {open_count} open dry positions")

    if open_count > 0:
        if not dry_run:
            await execute(
                """
                UPDATE strategy_positions
                SET status = 'void', pnl = 0, closed_at = now()
                WHERE status = 'open' AND mode = 'dry'
                """
            )
            logger.info(f"  → {open_count} positions voided")
        else:
            logger.info(f"  [DRY RUN] Would void {open_count} positions")

    # 2. Reset bankroll to initial_bankroll
    strategies = await fetch(
        "SELECT strategy_key, bankroll, initial_bankroll FROM strategy_bankroll"
    )
    logger.info(f"Found {len(strategies)} strategies in bankroll table")

    reset_count = 0
    for row in strategies:
        key = row["strategy_key"]
        current = float(row["bankroll"])
        initial = float(row["initial_bankroll"])
        diff = current - initial

        if abs(diff) > 0.01:
            logger.info(f"  {key}: {current:.2f} → {initial:.2f} (diff {diff:+.2f})")
            if not dry_run:
                await execute(
                    "UPDATE strategy_bankroll SET bankroll = initial_bankroll, updated_at = now() WHERE strategy_key = $1",
                    key,
                )
                reset_count += 1
            else:
                reset_count += 1

    if reset_count > 0:
        logger.info(f"  → {reset_count} strategy bankrolls reset")

    # 3. Clear ledger for current period (from 2026-03-25 onwards)
    if not dry_run:
        ledger_count = await fetchval(
            "SELECT COUNT(*) FROM strategy_ledger WHERE created_at >= '2026-03-25'"
        )
        await execute(
            "DELETE FROM strategy_ledger WHERE created_at >= '2026-03-25'"
        )
        logger.info(f"  → Ledger entries deleted: {ledger_count}")
    else:
        ledger_count = await fetchval(
            "SELECT COUNT(*) FROM strategy_ledger WHERE created_at >= '2026-03-25'"
        )
        logger.info(f"  [DRY RUN] Would delete {ledger_count} ledger entries")

    logger.info("Done!")


async def run() -> None:
    """Initialize DB and run reset."""
    from app.services.database import close_db, init_db

    dry_run = "--dry-run" in sys.argv

    await init_db(min_size=1, max_size=3)
    try:
        await main(dry_run=dry_run)
    finally:
        await close_db()


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        sys.exit(0)

    asyncio.run(run())
