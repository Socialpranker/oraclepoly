"""Force settle stale virtual_filled positions через CLOB API.

Problem: market_sync не обновляет `markets.resolved` для некоторых markets,
потому что Gamma API не возвращает их (вероятно archived/never-listed). При
этом CLOB API имеет authoritative state (closed=true, tokens[*].winner).

Workflow:
  1. Найти virtual_filled с m.end_date < now()
  2. Для каждого спросить CLOB /markets/{condition_id}
  3. Если CLOB closed=true:
     a. Определить outcome: winner='Yes'|'No'|None (50/50)
     b. UPDATE markets SET resolved=true, current_price=outcome_price
     c. v2-execution-resolve cron сам settle'нет orders в следующий тик
  4. Если CLOB closed=false → log skip
  5. Если CLOB 404 → log skip

Idempotent. Только UPDATE на markets, не пишет execution_orders напрямую.

Usage:
    python scripts/force_settle_stale_opens.py --dry-run
    python scripts/force_settle_stale_opens.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


_FETCH_STALE_SQL = """
SELECT
    eo.market_id,
    m.current_price::float AS cur_yes,
    m.end_date::timestamp(0) AS end_dt,
    COUNT(eo.id) AS n_orders
  FROM execution_orders eo
  JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.status='virtual_filled'
   AND eo.execution_mode='virtual'
   AND m.end_date IS NOT NULL
   AND m.end_date < now()
   AND m.resolved = false
 GROUP BY eo.market_id, m.current_price, m.end_date
 ORDER BY m.end_date ASC
"""

_UPDATE_MARKET_RESOLVED_SQL = """
UPDATE markets
   SET resolved      = true,
       current_price = $2
 WHERE polymarket_id = $1
"""


async def _fetch_clob_market(condition_id: str) -> dict | None:
    import httpx

    url = f"https://clob.polymarket.com/markets/{condition_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        print(f"  clob_error {condition_id[:20]}..: {e}")
        return None


def _extract_outcome_price(clob_data: dict) -> float | None:
    """Return canonical YES price after resolve.

    For non-binary markets без явного Yes/No outcome: возвращаем None (skip).
    For 50-50 markets: 0.5.
    For decided markets: winner YES → 1.0, winner NO → 0.0.
    """
    if not clob_data.get("closed"):
        return None

    if clob_data.get("is_50_50_outcome"):
        return 0.5

    tokens = clob_data.get("tokens") or []
    if len(tokens) != 2:
        return None

    yes_token = None
    no_token = None
    for t in tokens:
        outcome_str = (t.get("outcome") or "").strip().lower()
        if outcome_str in ("yes",):
            yes_token = t
        elif outcome_str in ("no",):
            no_token = t

    if yes_token is None or no_token is None:
        # Non-binary outcomes (multi-choice market). Skip.
        return None

    if yes_token.get("winner") is True:
        return 1.0
    if no_token.get("winner") is True:
        return 0.0
    # Neither declared winner yet, но closed=true → 50/50 unresolved?
    return 0.5


async def main(apply: bool) -> None:
    import asyncpg

    from app.core.config import get_settings

    settings = get_settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        rows = await conn.fetch(_FETCH_STALE_SQL)
        if not rows:
            print("No stale virtual_filled positions found.")
            return

        print(f"Found {len(rows)} stale markets (past end_date, resolved=false).\n")
        print(f"{'condition_id':<22} {'end_date':<22} {'n':>3} {'cur_yes':>8} {'clob_closed':<12} {'price_set':<10}")
        print("-" * 86)

        n_resolved = 0
        n_skipped = 0
        n_not_found = 0

        for r in rows:
            cid = r["market_id"]
            n = r["n_orders"]
            cur_yes = r["cur_yes"]
            end_dt = r["end_dt"]

            clob = await _fetch_clob_market(cid)
            if clob is None:
                print(f"{cid[:20]}.. {str(end_dt):<22} {n:>3} {cur_yes:>8.4f} {'NOT FOUND':<12} {'skip':<10}")
                n_not_found += 1
                continue

            closed = clob.get("closed")
            price = _extract_outcome_price(clob)

            if not closed or price is None:
                action = "skip"
                print(
                    f"{cid[:20]}.. {str(end_dt):<22} {n:>3} {cur_yes:>8.4f} "
                    f"{'closed=' + str(closed):<12} {action:<10}"
                )
                n_skipped += 1
                continue

            action = f"yes={price}"
            print(
                f"{cid[:20]}.. {str(end_dt):<22} {n:>3} {cur_yes:>8.4f} "
                f"{'closed=True':<12} {action:<10}"
            )
            if apply:
                await conn.execute(_UPDATE_MARKET_RESOLVED_SQL, cid, price)
            n_resolved += 1

        print("-" * 86)
        print(f"Resolved: {n_resolved}, Skipped: {n_skipped}, Not found: {n_not_found}")
        if apply:
            print(f"\n✓ Updated {n_resolved} markets. v2-execution-resolve settle их в следующий cron tick.")
        else:
            print("\n(dry-run — markets table не изменена. Запусти с --apply.)")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
