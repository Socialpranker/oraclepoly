#!/usr/bin/env python3
"""
Diagnostic script: Check why Polymarket markets are not showing.
Tests the full pipeline: Gamma API → PostgreSQL → Data presence.

Usage:
    python scripts/debug/diagnose_markets.py
"""
import asyncio
import json
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx


def print_header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_ok(msg: str):
    print(f"  [OK] {msg}")


def print_fail(msg: str):
    print(f"  [FAIL] {msg}")


def print_info(msg: str):
    print(f"  [INFO] {msg}")


async def check_gamma_api():
    """Test 1: Can we reach the Polymarket Gamma API?"""
    print_header("1. Polymarket Gamma API")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 3,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            response.raise_for_status()
            markets = response.json()
            print_ok(f"API reachable, returned {len(markets)} markets")
            if markets:
                m = markets[0]
                print_info(f"  Top market: {m.get('question', 'N/A')[:70]}")
                print_info(f"  Volume 24h: {m.get('volume24hr', 'N/A')}")
                print_info(f"  Closed: {m.get('closed', 'N/A')}")
                print_info(f"  conditionId: {m.get('conditionId', 'N/A')[:30]}...")
                clob_ids = m.get("clobTokenIds", [])
                print_info(f"  clobTokenIds: {len(clob_ids) if isinstance(clob_ids, list) else 'N/A'} tokens")
            return markets
    except httpx.HTTPStatusError as e:
        print_fail(f"HTTP error: {e.response.status_code} {e.response.text[:200]}")
        return []
    except Exception as e:
        print_fail(f"Connection error: {e}")
        return []


async def check_database():
    """Test 2: Can we connect to PostgreSQL and read markets?"""
    print_header("2. PostgreSQL Connection & Data")

    import asyncpg

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print_fail("DATABASE_URL not set")
        return 0

    print_info(f"URL: {db_url[:40]}...")

    try:
        conn = await asyncpg.connect(db_url)

        rows = await conn.fetch("SELECT id, is_active, horizon FROM markets")
        total = len(rows)
        active = sum(1 for r in rows if r["is_active"])
        horizons = {}
        for r in rows:
            h = r.get("horizon", "unknown") or "unknown"
            horizons[h] = horizons.get(h, 0) + 1

        print_ok("Connected to PostgreSQL successfully")
        print_info(f"Total markets in DB: {total}")
        print_info(f"Active markets: {active}")
        print_info(f"Inactive markets: {total - active}")
        print_info(f"By horizon: {json.dumps(horizons)}")

        if total == 0:
            print_fail("DATABASE IS EMPTY - no markets synced!")
            print_info("Solutions:")
            print_info("  1. Call POST /api/markets/sync to trigger manual sync")
            print_info("  2. Check cron job status")
        elif active == 0:
            print_fail("All markets are INACTIVE - none will show in the app")

        await conn.close()
        return total

    except Exception as e:
        print_fail(f"Connection error: {e}")
        return 0


async def run_test_sync():
    """Test 4: Try syncing one market manually."""
    print_header("4. Manual Market Sync Test")

    import asyncpg

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print_fail("Missing DATABASE_URL")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": "true", "closed": "false", "limit": 1, "order": "volume24hr", "ascending": "false"},
            )
            res.raise_for_status()
            markets = res.json()
            if not markets:
                print_fail("No markets from Gamma API")
                return

            m = markets[0]
            polymarket_id = m.get("conditionId") or m.get("id")
            print_info(f"Attempting to upsert: {m.get('question', 'N/A')[:60]}")
            print_info(f"  polymarket_id: {polymarket_id}")

            conn = await asyncpg.connect(db_url)
            result = await conn.fetchrow(
                """INSERT INTO markets (polymarket_id, question, description, category, current_price, volume_24h, is_active, horizon)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   ON CONFLICT (polymarket_id) DO UPDATE SET
                     question = EXCLUDED.question,
                     volume_24h = EXCLUDED.volume_24h,
                     updated_at = NOW()
                   RETURNING id""",
                str(polymarket_id),
                m.get("question", ""),
                m.get("description", ""),
                m.get("category", "General"),
                0.5,
                float(m.get("volume24hr", 0) or 0),
                True,
                "mid",
            )
            print_ok(f"Successfully upserted market! ID: {result['id']}")
            await conn.close()

    except Exception as e:
        print_fail(f"Error: {e}")


async def main():
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(env_path)

    print("\n" + "=" * 60)
    print("  ORACLE - Polymarket Markets Diagnostic")
    print("=" * 60)

    await check_gamma_api()
    await check_database()
    await run_test_sync()

    print_header("SUMMARY")
    print("  If Test 1 fails: Gamma API is down or blocked on your network")
    print("  If Test 2 fails: Check DATABASE_URL in .env")
    print("  If Test 2 shows 0 markets: market_sync never ran -> use POST /api/markets/sync")
    print("  If Test 4 fails: Check DATABASE_URL and PostgreSQL connectivity")
    print()


if __name__ == "__main__":
    asyncio.run(main())
