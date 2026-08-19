#!/usr/bin/env python3
"""Polymarket order book snapshot job — собирает /book для всех active markets.

Запускается на VPS через cron каждые 5 минут.
Накапливает в /mnt/data/orderbook_snapshots/YYYY-MM-DD.parquet.

Через 30 дней — backfill orderbook features (best_bid, best_ask, spread, depth)
в ml_features.parquet для post-V2 markets.

Schema (per snapshot):
  snapshot_ts        BIGINT
  condition_id       VARCHAR
  token_id_yes       VARCHAR
  best_bid_yes       DOUBLE
  best_ask_yes       DOUBLE
  spread_bps         DOUBLE  (ask - bid) * 10000 / mid
  bid_depth_$1k      DOUBLE  cumulative size at bid for first $1k
  ask_depth_$1k      DOUBLE
  bid_depth_$10k     DOUBLE
  ask_depth_$10k     DOUBLE
  total_bid_size     DOUBLE
  total_ask_size     DOUBLE

Cron: каждые 5 мин для top-1000 active markets.
БЕЗ proxy — read-only.

Запуск:
    /root/oracle/.venv/bin/python /root/oracle/scripts/research/polymarket_orderbook_snapshot.py

Cron entry (на VPS):
    */5 * * * * /root/oracle/.venv/bin/python /root/oracle/scripts/research/polymarket_orderbook_snapshot.py >> /var/log/orderbook_snapshot.log 2>&1
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"

OUTPUT_DIR = Path("/mnt/data/orderbook_snapshots")
TOP_MARKETS_LIMIT = 1000
CONCURRENCY = 5
REQUEST_TIMEOUT = 10.0
MIN_VOLUME_24H = 100.0  # дешёвый фильтр


async def fetch_active_markets(client: httpx.AsyncClient) -> list[dict]:
    """Получить top active markets отсортированных по volume_24hr."""
    r = await client.get(
        GAMMA_URL,
        params={
            "active": "true",
            "closed": "false",
            "limit": TOP_MARKETS_LIMIT,
            "order": "volume24hr",
            "ascending": "false",
        },
    )
    r.raise_for_status()
    data = r.json()
    out: list[dict] = []
    for m in data:
        if not isinstance(m, dict):
            continue
        if m.get("volumeNum", 0) < MIN_VOLUME_24H:
            continue
        cond = m.get("conditionId")
        if not cond:
            continue
        token_ids = m.get("clobTokenIds", "")
        if isinstance(token_ids, str):
            try:
                import json
                token_ids = json.loads(token_ids)
            except Exception:
                continue
        if not token_ids or len(token_ids) < 1:
            continue
        out.append({
            "condition_id": cond,
            "token_id_yes": token_ids[0],
        })
    return out


async def fetch_book(client: httpx.AsyncClient, token_id: str) -> dict | None:
    """GET /book?token_id=..."""
    try:
        r = await client.get(CLOB_BOOK_URL, params={"token_id": token_id})
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def compute_book_features(book: dict) -> dict:
    """Извлекает spread, depth метрики из /book ответа."""
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    best_bid = float(bids[-1]["price"]) if bids else 0.0
    best_ask = float(asks[0]["price"]) if asks else 1.0
    mid = (best_bid + best_ask) / 2 if (best_bid > 0 and best_ask < 1) else 0.5

    spread_bps = (best_ask - best_bid) * 10000.0 / mid if mid > 0 else 0.0

    def depth_for(side_levels: list, target_usd: float, reverse: bool) -> float:
        """Cumulative size до достижения target_usd."""
        cum_size = 0.0
        cum_usd = 0.0
        levels = list(reversed(side_levels)) if reverse else side_levels
        for lvl in levels:
            try:
                price = float(lvl["price"])
                size = float(lvl["size"])
            except (KeyError, TypeError, ValueError):
                continue
            cum_usd += price * size
            cum_size += size
            if cum_usd >= target_usd:
                return cum_size
        return cum_size

    bid_depth_1k = depth_for(bids, 1000.0, reverse=True)
    ask_depth_1k = depth_for(asks, 1000.0, reverse=False)
    bid_depth_10k = depth_for(bids, 10000.0, reverse=True)
    ask_depth_10k = depth_for(asks, 10000.0, reverse=False)

    total_bid_size = sum(float(b.get("size", 0) or 0) for b in bids)
    total_ask_size = sum(float(a.get("size", 0) or 0) for a in asks)

    return {
        "best_bid_yes": best_bid,
        "best_ask_yes": best_ask,
        "spread_bps": spread_bps,
        "bid_depth_1k": bid_depth_1k,
        "ask_depth_1k": ask_depth_1k,
        "bid_depth_10k": bid_depth_10k,
        "ask_depth_10k": ask_depth_10k,
        "total_bid_size": total_bid_size,
        "total_ask_size": total_ask_size,
    }


async def snapshot_market(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    market: dict,
    snapshot_ts: int,
) -> dict | None:
    async with sem:
        book = await fetch_book(client, market["token_id_yes"])
        if not book:
            return None
        feats = compute_book_features(book)
        return {
            "snapshot_ts": snapshot_ts,
            "condition_id": market["condition_id"],
            "token_id_yes": market["token_id_yes"],
            **feats,
        }


async def main_async() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_ts = int(time.time())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = OUTPUT_DIR / f"{today}.parquet"

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        trust_env=False,
    ) as client:
        markets = await fetch_active_markets(client)
        logger.info("orderbook_snapshot_start markets=%d", len(markets))

        sem = asyncio.Semaphore(CONCURRENCY)
        tasks = [snapshot_market(client, sem, m, snapshot_ts) for m in markets]
        results = await asyncio.gather(*tasks)
        rows = [r for r in results if r is not None]

    if not rows:
        logger.warning("no_rows_collected")
        return

    import pandas as pd
    df = pd.DataFrame(rows)

    if out_path.exists():
        existing = pd.read_parquet(out_path)
        df = pd.concat([existing, df], ignore_index=True)
    df.to_parquet(out_path, compression="zstd", index=False)
    logger.info("orderbook_snapshot_done file=%s rows_added=%d total=%d",
                out_path.name, len(rows), len(df))


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
