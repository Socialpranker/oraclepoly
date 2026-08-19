#!/usr/bin/env python3
"""
Goldsky Backfill — Phase 1 of maker-flow research pipeline.

Шаги:
1. Скачать S3 snapshot (orderFilled_complete.csv.xz) → стриминг через DuckDB → parquet
2. Catch-up через Goldsky GraphQL для событий после даты снапшота (Feb 12 2026)
3. Enrich с market_id через token→market lookup (gamma-api)
4. Фильтровать PLATFORM_WALLETS
5. Финальный parquet: /mnt/data/maker_flow/maker_flow.parquet

Запуск на VPS:
    python3 scripts/goldsky_backfill.py [--skip-s3] [--skip-graphql] [--dry-run]

Требования: duckdb, httpx (pip3 show duckdb httpx)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path

import duckdb
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("goldsky_backfill")

# ── Constants ─────────────────────────────────────────────────────────────────

GOLDSKY_URL = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/0.0.1/gn"
)
S3_URL = "https://polydata-archive.s3.us-east-1.amazonaws.com/orderFilled_complete.csv.xz"
GAMMA_URL = "https://gamma-api.polymarket.com/markets"

OUTPUT_DIR = Path("/mnt/data/maker_flow")
OUTPUT_PARQUET = OUTPUT_DIR / "maker_flow.parquet"
CHECKPOINT_FILE = OUTPUT_DIR / "graphql_cursor.json"
TOKEN_CACHE_FILE = OUTPUT_DIR / "token_to_market.json"

# S3 snapshot covers up to Feb 12 2026 → UNIX timestamp
S3_SNAPSHOT_TS = 1739318400  # 2026-02-12 00:00:00 UTC

PLATFORM_WALLETS = {
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
}

PAGE_SIZE = 1000  # Goldsky max per query
GRAPHQL_CONCURRENCY = 3  # Parallel GraphQL pages
MARKET_BATCH = 20  # gamma-api markets per page (small: GET URI limit)


# ── DuckDB schema ──────────────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS maker_flow (
    id              VARCHAR PRIMARY KEY,
    transaction_hash VARCHAR,
    timestamp       BIGINT,
    maker           VARCHAR,
    taker           VARCHAR,
    maker_direction VARCHAR,   -- BUY or SELL (from maker perspective)
    taker_direction VARCHAR,   -- BUY or SELL (from taker perspective)
    maker_asset_id  VARCHAR,
    taker_asset_id  VARCHAR,
    maker_amount    DOUBLE,    -- raw units (USDC: 6 decimals, token: 6 decimals)
    taker_amount    DOUBLE,
    price           DOUBLE,    -- USDC per token
    usdc_amount     DOUBLE,    -- USDC side (6 decimals → /1e6 for real value)
    token_amount    DOUBLE,    -- token side
    condition_id    VARCHAR,
    market_id       VARCHAR,
    is_platform     BOOLEAN,   -- maker or taker is PLATFORM_WALLET
)
"""


def _direction(taker_asset_id: str) -> tuple[str, str]:
    """
    takerAssetId == "0" означает taker платит USDC → taker BUYs tokens.
    Возвращает (taker_direction, maker_direction).
    """
    if taker_asset_id == "0":
        return "BUY", "SELL"
    else:
        return "SELL", "BUY"


def _parse_event(ev: dict) -> dict | None:
    """Распарсить raw GraphQL event → dict для вставки."""
    try:
        taker_asset = ev.get("takerAssetId", "")
        maker_asset = ev.get("makerAssetId", "")
        maker_amt = float(ev.get("makerAmountFilled", 0))
        taker_amt = float(ev.get("takerAmountFilled", 0))
        ts = int(ev.get("timestamp", 0))
        maker = (ev.get("maker") or "").lower()
        taker = (ev.get("taker") or "").lower()
        tx_hash = ev.get("transactionHash", ev.get("id", "").split("_")[0])
        ev_id = ev.get("id", "")

        if not ev_id or maker_amt <= 0 or taker_amt <= 0 or ts == 0:
            return None

        taker_dir, maker_dir = _direction(taker_asset)

        # USDC side has 6 decimals, token side has 6 decimals
        # Price = USDC_amount / token_amount (both in raw units → ratio is same)
        if taker_dir == "BUY":
            usdc_amount = taker_amt  # taker pays USDC
            token_amount = maker_amt  # maker provides tokens
        else:
            usdc_amount = maker_amt  # maker pays USDC
            token_amount = taker_amt  # taker provides tokens

        price = usdc_amount / token_amount if token_amount > 0 else 0.0

        is_platform = maker in PLATFORM_WALLETS or taker in PLATFORM_WALLETS

        # condition_id: the non-USDC asset id is the token_id
        # token_id maps to condition_id via market lookup
        token_id = maker_asset if taker_asset == "0" else taker_asset

        return {
            "id": ev_id,
            "transaction_hash": tx_hash,
            "timestamp": ts,
            "maker": maker,
            "taker": taker,
            "maker_direction": maker_dir,
            "taker_direction": taker_dir,
            "maker_asset_id": maker_asset,
            "taker_asset_id": taker_asset,
            "maker_amount": maker_amt,
            "taker_amount": taker_amt,
            "price": price,
            "usdc_amount": usdc_amount,
            "token_amount": token_amount,
            "condition_id": "",       # filled in by enrich step
            "market_id": "",          # filled in by enrich step
            "is_platform": is_platform,
            "_token_id": token_id,    # temp field for lookup, dropped before insert
        }
    except Exception as e:
        log.debug(f"parse_event error: {e} ev={ev}")
        return None


# ── Token → Market cache ───────────────────────────────────────────────────────

class TokenMarketCache:
    """In-memory + file cache: token_id → (condition_id, market_id)."""

    def __init__(self):
        self._cache: dict[str, tuple[str, str]] = {}
        self._missing: set[str] = set()
        if TOKEN_CACHE_FILE.exists():
            try:
                raw = json.loads(TOKEN_CACHE_FILE.read_text())
                self._cache = {k: tuple(v) for k, v in raw.items()}
                log.info(f"Loaded token cache: {len(self._cache)} entries")
            except Exception as e:
                log.warning(f"Token cache load error: {e}")

    def get(self, token_id: str) -> tuple[str, str] | None:
        return self._cache.get(token_id)

    def set(self, token_id: str, condition_id: str, market_id: str):
        self._cache[token_id] = (condition_id, market_id)
        self._missing.discard(token_id)

    def save(self):
        TOKEN_CACHE_FILE.write_text(json.dumps(self._cache))
        log.info(f"Saved token cache: {len(self._cache)} entries")

    async def _fetch_one_token(self, token_id: str, client: httpx.AsyncClient) -> None:
        """Fetch a single token_id from gamma-api and populate cache."""
        try:
            resp = await client.get(
                GAMMA_URL,
                params={"clob_token_id": token_id},
                timeout=15,
            )
            if resp.status_code != 200:
                return
            markets = resp.json()
            if not isinstance(markets, list):
                markets = [markets] if markets else []
            for mkt in markets:
                condition_id = mkt.get("conditionId", "")
                market_id = str(mkt.get("id", ""))
                tokens = mkt.get("clobTokenIds", []) or []
                if isinstance(tokens, str):
                    try:
                        tokens = json.loads(tokens)
                    except Exception:
                        tokens = [tokens]
                for tok in tokens:
                    self.set(str(tok), condition_id, market_id)
                if condition_id and not self.get(token_id):
                    self.set(token_id, condition_id, market_id)
        except Exception as e:
            log.debug(f"gamma-api error {token_id[:12]}: {e}")

    async def fetch_missing(self, token_ids: set[str], client: httpx.AsyncClient):
        """Fetch unknown token_ids from gamma-api with concurrency=10."""
        unknown = list(token_ids - set(self._cache.keys()))
        if not unknown:
            return

        log.info(f"Fetching {len(unknown)} unknown token_ids from gamma-api (concurrent)...")

        semaphore = asyncio.Semaphore(10)

        async def guarded(tid: str) -> None:
            async with semaphore:
                await self._fetch_one_token(tid, client)
                await asyncio.sleep(0.03)

        await asyncio.gather(*[guarded(tid) for tid in unknown if tid])

        still_unknown = set(unknown) - set(self._cache.keys())
        if still_unknown:
            log.debug(f"{len(still_unknown)} token_ids not resolved")


# ── GraphQL pagination ─────────────────────────────────────────────────────────

PLATFORM_FILTER = (
    '["{a}","{b}"]'.format(
        a="0xc5d563a36ae78145c45a50134d48a1215220f80a",
        b="0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    )
)


async def fetch_graphql_page(
    client: httpx.AsyncClient,
    after_ts: int,
    last_id: str | None = None,
    retries: int = 3,
) -> list[dict]:
    """
    Fetch one page of orderFilledEvents.
    Sticky cursor: if last_id set → same timestamp, paginate by id.
    Filters out PLATFORM_WALLETS at query level to reduce volume (~41% less data).
    """
    if last_id:
        # Sticky mode: same timestamp, different ids
        where = (
            f'timestamp: "{after_ts}", id_gt: "{last_id}", '
            f'maker_not_in: {PLATFORM_FILTER}, taker_not_in: {PLATFORM_FILTER}'
        )
    else:
        where = (
            f'timestamp_gt: "{after_ts}", '
            f'maker_not_in: {PLATFORM_FILTER}, taker_not_in: {PLATFORM_FILTER}'
        )

    query = f"""{{
        orderFilledEvents(
            first: {PAGE_SIZE},
            orderBy: timestamp,
            orderDirection: asc,
            where: {{{where}}}
        ) {{
            id
            timestamp
            maker
            taker
            makerAmountFilled
            takerAmountFilled
            makerAssetId
            takerAssetId
            transactionHash
        }}
    }}"""

    for attempt in range(retries):
        try:
            resp = await client.post(GOLDSKY_URL, json={"query": query}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                log.warning(f"GraphQL errors: {data['errors']}")
                return []
            return data.get("data", {}).get("orderFilledEvents", [])
        except (httpx.ConnectTimeout, httpx.ConnectError) as e:
            log.warning(f"GraphQL connect retry {attempt+1}: {e}")
            await asyncio.sleep(2 ** attempt)
        except Exception as e:
            log.warning(f"GraphQL error: {e}")
            await asyncio.sleep(1)
    return []


async def graphql_catchup(
    db: duckdb.DuckDBPyConnection,
    token_cache: TokenMarketCache,
    start_ts: int,
    dry_run: bool = False,
) -> int:
    """
    Paginate Goldsky from start_ts to now.
    Uses sticky cursor to handle same-timestamp events.
    Returns total rows inserted.
    """
    log.info(f"GraphQL catch-up from ts={start_ts} ({time.strftime('%Y-%m-%d', time.gmtime(start_ts))})")

    # Load checkpoint
    cursor_ts = start_ts
    cursor_id = None
    if CHECKPOINT_FILE.exists():
        try:
            ckpt = json.loads(CHECKPOINT_FILE.read_text())
            cursor_ts = ckpt.get("ts", start_ts)
            cursor_id = ckpt.get("id")
            log.info(f"Resuming from checkpoint ts={cursor_ts} id={cursor_id}")
        except Exception:
            pass

    total_inserted = 0
    pages = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            events = await fetch_graphql_page(client, cursor_ts, cursor_id)
            if not events:
                log.info("GraphQL: no more events")
                break

            pages += 1
            parsed = [_parse_event(ev) for ev in events]
            parsed = [p for p in parsed if p is not None]

            if not dry_run and parsed:
                # Enrich token_ids
                token_ids = {p["_token_id"] for p in parsed if p["_token_id"]}
                await token_cache.fetch_missing(token_ids, client)
                for p in parsed:
                    tok = p.pop("_token_id", "")
                    entry = token_cache.get(tok)
                    if entry:
                        p["condition_id"], p["market_id"] = entry

                inserted = _bulk_insert(db, parsed)
                total_inserted += inserted

            # Update cursor
            last_ev = events[-1]
            last_ts = int(last_ev["timestamp"])
            last_id = last_ev["id"]

            # Detect sticky: if all events have same timestamp
            all_ts = {int(e["timestamp"]) for e in events}
            if len(all_ts) == 1:
                # Sticky mode: next page same timestamp, advance id
                cursor_ts = last_ts
                cursor_id = last_id
            else:
                cursor_ts = last_ts
                cursor_id = None

            if pages % 10 == 0:
                log.info(f"GraphQL pages={pages} cursor_ts={cursor_ts} inserted={total_inserted}")
                # Save checkpoint
                CHECKPOINT_FILE.write_text(json.dumps({"ts": cursor_ts, "id": cursor_id}))
                if not dry_run:
                    token_cache.save()

            if len(events) < PAGE_SIZE:
                break

            await asyncio.sleep(0.05)  # gentle rate limit

    # Final checkpoint save
    CHECKPOINT_FILE.write_text(json.dumps({"ts": cursor_ts, "id": cursor_id}))
    if not dry_run:
        token_cache.save()

    log.info(f"GraphQL done: pages={pages} inserted={total_inserted}")
    return total_inserted


def _bulk_insert(db: duckdb.DuckDBPyConnection, rows: list[dict]) -> int:
    """Insert rows into maker_flow table, skip conflicts."""
    if not rows:
        return 0
    try:
        # Build values for INSERT
        db.executemany(
            """
            INSERT OR IGNORE INTO maker_flow VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                (
                    r["id"], r["transaction_hash"], r["timestamp"],
                    r["maker"], r["taker"],
                    r["maker_direction"], r["taker_direction"],
                    r["maker_asset_id"], r["taker_asset_id"],
                    r["maker_amount"], r["taker_amount"],
                    r["price"], r["usdc_amount"], r["token_amount"],
                    r["condition_id"], r["market_id"], r["is_platform"],
                )
                for r in rows
            ],
        )
        return len(rows)
    except Exception as e:
        log.error(f"bulk_insert error: {e}")
        return 0


# ── S3 streaming via DuckDB ────────────────────────────────────────────────────

async def stream_s3_snapshot(
    db: duckdb.DuckDBPyConnection,
    token_cache: TokenMarketCache,
    dry_run: bool = False,
) -> int:
    """
    Stream S3 xz snapshot via subprocess | DuckDB read_csv.
    Memory-safe: no full materialization.
    """
    log.info("Starting S3 snapshot streaming...")
    log.info(f"URL: {S3_URL}")

    # First: download to /tmp compressed (6.24 GB), then stream decompress
    # DuckDB can't stream xz natively, so: curl | xz -d | duckdb read_csv stdin
    # Alternative: download to /mnt/data/tmp/ first then process

    tmp_xz = OUTPUT_DIR / "orderFilled_tmp.csv.xz"
    tmp_csv = OUTPUT_DIR / "orderFilled_tmp.csv"

    # Check if already downloaded
    if not tmp_xz.exists():
        log.info("Downloading S3 snapshot (6.24 GB)...")
        ret = subprocess.run(
            ["curl", "-L", "-o", str(tmp_xz), S3_URL],
            check=False,
        )
        if ret.returncode != 0:
            log.error("S3 download failed")
            return 0
        log.info("Download complete")
    else:
        log.info(f"S3 snapshot already downloaded: {tmp_xz}")

    if dry_run:
        log.info("[DRY RUN] Skip S3 processing")
        return 0

    # Decompress to CSV if not done
    if not tmp_csv.exists():
        log.info("Decompressing xz archive...")
        ret = subprocess.run(["xz", "-d", "-k", str(tmp_xz)], check=False)
        if ret.returncode != 0:
            log.error("xz decompress failed")
            return 0
        log.info("Decompression done")

    csv_size = tmp_csv.stat().st_size if tmp_csv.exists() else 0
    log.info(f"CSV size: {csv_size / 1e9:.2f} GB")

    # Use DuckDB to stream CSV → insert into maker_flow
    # First peek at columns
    # Peek at columns: execute populates db.description (result rows themselves unused).
    db.execute(f"SELECT * FROM read_csv_auto('{tmp_csv}', sample_size=100) LIMIT 3").fetchall()
    cols = [d[0] for d in db.description]
    log.info(f"S3 CSV columns: {cols}")

    # Map columns to our schema
    # Expected from poly_data: id, timestamp, maker, taker, makerAmountFilled, takerAmountFilled, makerAssetId, takerAssetId, transactionHash
    # (column names may vary — adapt based on actual CSV)

    log.info("Processing CSV via DuckDB streaming (batch 100K)...")

    # Count total rows
    total_rows = db.execute(f"SELECT COUNT(*) FROM read_csv_auto('{tmp_csv}')").fetchone()[0]
    log.info(f"Total CSV rows: {total_rows:,}")

    batch_size = 100_000
    inserted_total = 0
    offset = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while offset < total_rows:
            rows_raw = db.execute(
                f"SELECT * FROM read_csv_auto('{tmp_csv}') LIMIT {batch_size} OFFSET {offset}"
            ).fetchall()
            if not rows_raw:
                break

            # Parse into dicts using actual column names
            parsed_batch = []
            for row in rows_raw:
                ev = dict(zip(cols, row))
                # Normalize field names (poly_data CSV may use different casing)
                ev_normalized = {
                    "id": ev.get("id", ""),
                    "transactionHash": ev.get("transactionHash", ev.get("transaction_hash", "")),
                    "timestamp": ev.get("timestamp", 0),
                    "maker": ev.get("maker", ""),
                    "taker": ev.get("taker", ""),
                    "makerAmountFilled": ev.get("makerAmountFilled", ev.get("maker_amount_filled", 0)),
                    "takerAmountFilled": ev.get("takerAmountFilled", ev.get("taker_amount_filled", 0)),
                    "makerAssetId": ev.get("makerAssetId", ev.get("maker_asset_id", "")),
                    "takerAssetId": ev.get("takerAssetId", ev.get("taker_asset_id", "")),
                }
                p = _parse_event(ev_normalized)
                if p:
                    parsed_batch.append(p)

            if parsed_batch:
                # Enrich
                token_ids = {p["_token_id"] for p in parsed_batch if p["_token_id"]}
                await token_cache.fetch_missing(token_ids, client)
                for p in parsed_batch:
                    tok = p.pop("_token_id", "")
                    entry = token_cache.get(tok)
                    if entry:
                        p["condition_id"], p["market_id"] = entry

                inserted = _bulk_insert(db, parsed_batch)
                inserted_total += inserted

            offset += batch_size
            pct = offset / total_rows * 100
            if offset % (batch_size * 10) == 0:
                log.info(f"S3 progress: {offset:,}/{total_rows:,} ({pct:.1f}%) inserted={inserted_total:,}")
                token_cache.save()

    log.info(f"S3 streaming done: inserted={inserted_total:,}")
    return inserted_total


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Goldsky maker-flow backfill")
    parser.add_argument("--skip-s3", action="store_true", help="Skip S3 snapshot processing")
    parser.add_argument("--skip-graphql", action="store_true", help="Skip GraphQL catch-up")
    parser.add_argument("--dry-run", action="store_true", help="Fetch only, don't write to DB")
    parser.add_argument("--graphql-start-ts", type=int, default=S3_SNAPSHOT_TS,
                        help=f"GraphQL start timestamp (default: {S3_SNAPSHOT_TS} = Feb 12 2026)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Open DuckDB
    db_path = ":memory:" if args.dry_run else str(OUTPUT_DIR / "maker_flow.duckdb")
    log.info(f"Opening DuckDB: {db_path}")
    db = duckdb.connect(db_path)
    db.execute(CREATE_TABLE_SQL)
    db.execute("PRAGMA threads=4")
    db.execute("PRAGMA memory_limit='3GB'")

    token_cache = TokenMarketCache()

    # Step 1: S3 snapshot
    if not args.skip_s3:
        s3_inserted = await stream_s3_snapshot(db, token_cache, dry_run=args.dry_run)
        log.info(f"S3 step done: {s3_inserted:,} rows")
    else:
        log.info("Skipping S3 step")

    # Step 2: GraphQL catch-up
    if not args.skip_graphql:
        gql_inserted = await graphql_catchup(
            db, token_cache,
            start_ts=args.graphql_start_ts,
            dry_run=args.dry_run,
        )
        log.info(f"GraphQL step done: {gql_inserted:,} rows")
    else:
        log.info("Skipping GraphQL step")

    if not args.dry_run:
        # Export to parquet
        log.info(f"Exporting to {OUTPUT_PARQUET}...")
        count = db.execute("SELECT COUNT(*) FROM maker_flow").fetchone()[0]
        log.info(f"Total rows in maker_flow: {count:,}")

        db.execute(f"""
            COPY (
                SELECT
                    id, transaction_hash, timestamp, maker, taker,
                    maker_direction, taker_direction,
                    maker_asset_id, taker_asset_id,
                    maker_amount, taker_amount, price,
                    usdc_amount, token_amount,
                    condition_id, market_id, is_platform
                FROM maker_flow
                ORDER BY timestamp
            ) TO '{OUTPUT_PARQUET}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """)
        parquet_size = OUTPUT_PARQUET.stat().st_size
        log.info(f"Parquet written: {parquet_size / 1e9:.2f} GB → {OUTPUT_PARQUET}")

        # Stats
        stats = db.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(DISTINCT maker) as unique_makers,
                COUNT(DISTINCT taker) as unique_takers,
                COUNT(DISTINCT market_id) as markets,
                MIN(timestamp) as min_ts,
                MAX(timestamp) as max_ts,
                SUM(CASE WHEN is_platform THEN 1 ELSE 0 END) as platform_events,
                SUM(CASE WHEN NOT is_platform THEN 1 ELSE 0 END) as real_events
            FROM maker_flow
        """).fetchone()
        log.info(
            f"Stats: total={stats[0]:,} makers={stats[1]:,} takers={stats[2]:,} "
            f"markets={stats[3]:,} "
            f"date_range={time.strftime('%Y-%m-%d', time.gmtime(stats[4]))}"
            f"..{time.strftime('%Y-%m-%d', time.gmtime(stats[5]))} "
            f"platform={stats[6]:,} real={stats[7]:,}"
        )

    db.close()
    log.info("Backfill complete.")


if __name__ == "__main__":
    asyncio.run(main())
