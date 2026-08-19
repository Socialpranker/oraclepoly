"""Live trade bucket writer — каждые 15 min обновляет live_trade_buckets.

Для каждого active market (end_date > now + 1h):
  1. Найти MAX(hour_bucket) уже сохранённых buckets для этого market
  2. Fetch trades from Polymarket data-api начиная с этого момента (или 24h назад
     если ничего нет — bootstrap path)
  3. Aggregate в hourly buckets
  4. UPSERT в live_trade_buckets (ON CONFLICT update — buckets с late-arriving trades
     дополняются)
  5. Cleanup: DELETE WHERE hour_bucket < now() - INTERVAL '31 days'

Schema mirrors canonical trade_buckets.parquet exactly:
  condition_id, hour_bucket, n_trades, total_volume_usd, vwap_yes,
  min_yes_price, max_yes_price, buy_volume, sell_volume.

Reuses _fetch_market_trades pattern из d13_scan.py.
Concurrency limit 5 (не убить data-api rate limit).

См. docs/superpowers/plans/bright-hopping-meteor.md (Day 3).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.services.database import execute, executemany, fetch
from app.services.polymarket_trades_pager import fetch_trades_since

logger = structlog.get_logger(__name__)

# Concurrency 2 (was 5) — Polymarket data-api имеет per-endpoint rate limit на
# cache-MISS /trades?market=<id> запросы. При concurrency=5 быстро попадаем в 429.
# С concurrency=2 + http_retry (3 attempts, backoff 2→4→8s) теряем <5% markets.
TRADES_FETCH_CONCURRENCY = 2

# Default lookback при bootstrap (если для market нет ни одного bucket).
# 30 дней покрывает самую длинную feature window 720h. Для cron loop hourly можно
# тонкое окно — fetch заберёт всё что после MAX(hour_bucket).
BOOTSTRAP_LOOKBACK_SEC: int = 30 * 24 * 3600  # 30 дней
RETENTION_DAYS: int = 31  # cleanup older

# Cron tick lookback для incremental — берём с запасом 6h на late-arriving trades
INCREMENTAL_OVERLAP_SEC: int = 6 * 3600


# Min 24h volume для inclusion в live ML pipeline. Используем volume_24h
# т.к. lifetime_volume_usd нет в Postgres markets table (только в canonical).
# Сокращает 26K active markets до ~1900 (vol_24h>=$5K).
MIN_VOLUME_24H_USD: float = 5000.0

_ACTIVE_MARKETS_SQL = """
SELECT m.condition_id, m.end_date
  FROM markets m
 WHERE m.is_active = true
   AND m.resolved = false
   AND m.condition_id IS NOT NULL
   AND m.end_date IS NOT NULL
   AND m.end_date > now() + INTERVAL '1 hour'
   AND COALESCE(m.volume_24h, 0) >= $1
"""

_LAST_BUCKET_SQL = """
SELECT condition_id, MAX(hour_bucket) AS last_bucket
  FROM live_trade_buckets
 WHERE condition_id = ANY($1::text[])
 GROUP BY condition_id
"""

_UPSERT_BUCKET_SQL = """
INSERT INTO live_trade_buckets (
    condition_id, hour_bucket, n_trades, total_volume_usd,
    vwap_yes, min_yes_price, max_yes_price, buy_volume, sell_volume, captured_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
ON CONFLICT (condition_id, hour_bucket)
DO UPDATE SET
    n_trades         = EXCLUDED.n_trades,
    total_volume_usd = EXCLUDED.total_volume_usd,
    vwap_yes         = EXCLUDED.vwap_yes,
    min_yes_price    = EXCLUDED.min_yes_price,
    max_yes_price    = EXCLUDED.max_yes_price,
    buy_volume       = EXCLUDED.buy_volume,
    sell_volume      = EXCLUDED.sell_volume,
    captured_at      = EXCLUDED.captured_at
"""

_CLEANUP_SQL = """
DELETE FROM live_trade_buckets WHERE hour_bucket < now() - make_interval(days => $1)
"""


async def _safe_fetch_trades_since(
    client: httpx.AsyncClient,
    condition_id: str,
    since_ts: int,
) -> list[dict[str, Any]]:
    """Wrap shared pager — fail-silent чтобы один битый market не валил весь tick."""
    try:
        return await fetch_trades_since(client, condition_id, since_ts)
    except Exception as exc:
        logger.warning(
            "live_trade_bucket_fetch_failed",
            condition_id=condition_id,
            error=str(exc)[:200],
        )
        return []


def _aggregate_to_buckets(
    trades: list[dict[str, Any]],
) -> dict[datetime, dict[str, Any]]:
    """Aggregate trades into hourly buckets.

    Returns: {hour_bucket: {n_trades, total_volume_usd, vwap_numerator, ...}}
    where hour_bucket is datetime truncated to hour (UTC).
    """
    buckets: dict[datetime, dict[str, Any]] = {}

    for t in trades:
        try:
            ts = int(t.get("timestamp", 0))
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0 or price <= 0 or price >= 1:
            continue

        side = (t.get("side") or "").upper()
        volume = size * price

        # Truncate timestamp to hour
        hour = datetime.fromtimestamp(ts, tz=UTC).replace(minute=0, second=0, microsecond=0)

        b = buckets.get(hour)
        if b is None:
            b = {
                "n_trades": 0,
                "total_volume_usd": 0.0,
                "vwap_numerator": 0.0,  # SUM(size * price * price) — для weighted avg
                "min_yes_price": price,
                "max_yes_price": price,
                "buy_volume": 0.0,
                "sell_volume": 0.0,
            }
            buckets[hour] = b

        b["n_trades"] += 1
        b["total_volume_usd"] += volume
        b["vwap_numerator"] += volume * price  # numerator для VWAP = sum(price * vol) / sum(vol)
        b["min_yes_price"] = min(b["min_yes_price"], price)
        b["max_yes_price"] = max(b["max_yes_price"], price)
        if side == "BUY":
            b["buy_volume"] += volume
        elif side == "SELL":
            b["sell_volume"] += volume

    # Финализируем: VWAP = numerator / total_volume_usd
    for h, b in buckets.items():
        if b["total_volume_usd"] > 0:
            b["vwap_yes"] = round(b["vwap_numerator"] / b["total_volume_usd"], 6)
        else:
            b["vwap_yes"] = None
        b["min_yes_price"] = round(b["min_yes_price"], 6)
        b["max_yes_price"] = round(b["max_yes_price"], 6)
        del b["vwap_numerator"]

    return buckets


async def _process_one_market(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    condition_id: str,
    since_ts: int,
) -> tuple[str, list[tuple]]:
    """Fetch + aggregate one market. Returns (cid, list of UPSERT tuples)."""
    async with sem:
        trades = await _safe_fetch_trades_since(client, condition_id, since_ts)

    if not trades:
        return condition_id, []

    buckets = _aggregate_to_buckets(trades)
    rows = [
        (
            condition_id,
            hour,
            b["n_trades"],
            b["total_volume_usd"],
            b["vwap_yes"],
            b["min_yes_price"],
            b["max_yes_price"],
            b["buy_volume"],
            b["sell_volume"],
        )
        for hour, b in buckets.items()
    ]
    return condition_id, rows


async def run_live_trade_bucket_writer() -> dict[str, Any]:
    """Один тик: incremental fetch trades + UPSERT buckets для всех active markets."""
    rows = await fetch(_ACTIVE_MARKETS_SQL, MIN_VOLUME_24H_USD)
    if not rows:
        logger.info("live_trade_bucket_no_active_markets")
        return {"markets": 0, "trades_fetched": 0, "buckets_upserted": 0, "deleted_old": 0}

    condition_ids = [r["condition_id"] for r in rows]

    # Find last bucket per market
    last_rows = await fetch(_LAST_BUCKET_SQL, condition_ids)
    last_map: dict[str, datetime] = {r["condition_id"]: r["last_bucket"] for r in last_rows}

    now_unix = int(time.time())

    sem = asyncio.Semaphore(TRADES_FETCH_CONCURRENCY)

    async with httpx.AsyncClient() as client:
        tasks = []
        for r in rows:
            cid = r["condition_id"]
            last = last_map.get(cid)
            if last is None:
                # Bootstrap: 30 дней назад
                since = now_unix - BOOTSTRAP_LOOKBACK_SEC
            else:
                # Incremental: с overlap 6h на late-arriving trades
                since = int(last.timestamp()) - INCREMENTAL_OVERLAP_SEC
            tasks.append(_process_one_market(client, sem, cid, since))

        results = await asyncio.gather(*tasks)

    n_trades_total = 0
    upsert_rows: list[tuple] = []
    for _cid, market_rows in results:
        n_trades_total += sum(r[2] for r in market_rows)  # n_trades is index 2
        upsert_rows.extend(market_rows)

    n_upserted = 0
    if upsert_rows:
        try:
            await executemany(_UPSERT_BUCKET_SQL, upsert_rows)
            n_upserted = len(upsert_rows)
        except Exception as exc:
            logger.error("live_trade_bucket_upsert_failed", error=str(exc)[:200], n=len(upsert_rows))

    # Cleanup old buckets
    n_deleted = 0
    try:
        result = await execute(_CLEANUP_SQL, RETENTION_DAYS)
        # asyncpg execute возвращает строку 'DELETE <n>'
        if isinstance(result, str) and result.startswith("DELETE "):
            n_deleted = int(result.split()[1])
    except Exception as exc:
        logger.warning("live_trade_bucket_cleanup_failed", error=str(exc)[:200])

    logger.info(
        "live_trade_bucket_done",
        markets=len(rows),
        trades_fetched=n_trades_total,
        buckets_upserted=n_upserted,
        deleted_old=n_deleted,
    )
    return {
        "markets": len(rows),
        "trades_fetched": n_trades_total,
        "buckets_upserted": n_upserted,
        "deleted_old": n_deleted,
    }
