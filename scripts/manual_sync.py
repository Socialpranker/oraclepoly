#!/usr/bin/env python3
"""
Manual sync scripts for local testing and debugging.

For production scheduled jobs, use scripts/cron_runner.py (VPS crontab).

Usage:
    python scripts/manual_sync.py markets    # Sync markets from Polymarket
    python scripts/manual_sync.py news       # Sync news (requires Firecrawl)
    python scripts/manual_sync.py cleanup    # Run storage cleanup
    python scripts/manual_sync.py all        # Run all syncs
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


async def sync_markets():
    """Sync markets from Polymarket Gamma API."""
    from app.jobs.market_sync import run_market_sync

    print("=" * 50)
    print("MARKET SYNC")
    print("=" * 50)
    print("Fetching active markets from Polymarket...")

    try:
        result = await run_market_sync()
        print("\nResult:")
        print(f"  Synced: {result.get('synced', 0)}")
        print(f"  Errors: {result.get('errors', 0)}")
        print(f"  Total:  {result.get('total', 0)}")
        return result
    except Exception as e:
        print(f"Error: {e}")
        return {"error": str(e)}


async def sync_news():
    """Sync news articles."""
    from app.jobs.news_sync import run_news_pipeline

    print("=" * 50)
    print("NEWS SYNC")
    print("=" * 50)
    print("Fetching and processing news articles...")

    try:
        result = await run_news_pipeline()
        print(f"\nResult: {result}")
        return result
    except Exception as e:
        print(f"Error: {e}")
        return {"error": str(e)}


async def run_cleanup():
    """Run storage cleanup."""
    from app.jobs.cleanup import run_storage_cleanup

    print("=" * 50)
    print("STORAGE CLEANUP")
    print("=" * 50)
    print("Cleaning old records...")

    try:
        result = await run_storage_cleanup()
        print(f"\nResult: {result}")
        return result
    except Exception as e:
        print(f"Error: {e}")
        return {"error": str(e)}


async def test_polymarket_api():
    """Test Polymarket API connectivity."""
    from app.services.polymarket_service import get_polymarket_service

    print("=" * 50)
    print("POLYMARKET API TEST")
    print("=" * 50)

    service = get_polymarket_service()
    markets = await service.fetch_active_markets(limit=5)

    print(f"\nFetched {len(markets)} markets:")
    for m in markets[:5]:
        question = m.get("question", "Unknown")[:60]
        volume = m.get("volume24hr", 0)
        print(f"  - {question}... (${volume:,.0f} 24h)")

    return markets


async def test_database():
    """Test PostgreSQL connectivity."""
    from app.services.database import close_db, fetch, init_db

    print("=" * 50)
    print("DATABASE TEST")
    print("=" * 50)

    await init_db()
    try:
        rows = await fetch("SELECT id, question FROM markets LIMIT 3")
        print(f"\nFound {len(rows)} markets in DB:")
        for m in rows:
            print(f"  - {m['question'][:50]}...")
        return rows
    finally:
        await close_db()


async def test_qdrant():
    """Test Qdrant connectivity."""
    from app.services.vector_service import get_vector_service

    print("=" * 50)
    print("QDRANT TEST")
    print("=" * 50)

    vector_store = get_vector_service()
    await vector_store.ensure_collections()

    collections = await vector_store.async_client.get_collections()
    print(f"\nCollections: {[c.name for c in collections.collections]}")

    return True


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "markets":
        await sync_markets()
    elif command == "news":
        await sync_news()
    elif command == "cleanup":
        await run_cleanup()
    elif command == "test":
        await test_polymarket_api()
        print()
        await test_database()
        print()
        await test_qdrant()
    elif command == "all":
        await sync_markets()
        print()
        await sync_news()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
