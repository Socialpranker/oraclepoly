"""Live wallet flow writer — каждые 15 min обновляет live_wallet_flow_per_market.

Per (market, hour_bucket) aggregates:
  n_unique_takers, n_unique_makers, whale_taker_volume (smart_money_taker_share weighted),
  whale_taker_count, retail_taker_avg_size, whale_buy_pressure, taker_count_total.

Fetch стратегия:
  - Полностью независимый fetch from Polymarket data-api (не зависит от
    live_trade_buckets — даёт изоляцию от его сбоев).
  - Окно 30 дней (mirror retention live_trade_buckets).
  - Incremental: с overlap 6h после MAX(hour_bucket) для late-arriving trades.

Smart money set: snapshot из wallet_smart_money table (refreshed weekly через
refresh_wallet_smart_money). is_smart_money=true → классифицируется как whale.

Schema mirrors canonical wallet_flow_per_market.parquet exactly.

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

TRADES_FETCH_CONCURRENCY = 2  # Was 5; см. live_trade_bucket_writer комментарий

BOOTSTRAP_LOOKBACK_SEC: int = 30 * 24 * 3600
RETENTION_DAYS: int = 31
INCREMENTAL_OVERLAP_SEC: int = 6 * 3600


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
  FROM live_wallet_flow_per_market
 WHERE condition_id = ANY($1::text[])
 GROUP BY condition_id
"""

_SMART_WALLETS_SQL = """
SELECT wallet
  FROM wallet_smart_money
 WHERE is_smart_money = true
"""

_UPSERT_SQL = """
INSERT INTO live_wallet_flow_per_market (
    condition_id, hour_bucket, n_unique_takers, n_unique_makers,
    whale_taker_volume, whale_taker_count, retail_taker_avg_size,
    whale_buy_pressure, taker_count_total, captured_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
ON CONFLICT (condition_id, hour_bucket)
DO UPDATE SET
    n_unique_takers       = EXCLUDED.n_unique_takers,
    n_unique_makers       = EXCLUDED.n_unique_makers,
    whale_taker_volume    = EXCLUDED.whale_taker_volume,
    whale_taker_count     = EXCLUDED.whale_taker_count,
    retail_taker_avg_size = EXCLUDED.retail_taker_avg_size,
    whale_buy_pressure    = EXCLUDED.whale_buy_pressure,
    taker_count_total     = EXCLUDED.taker_count_total,
    captured_at           = EXCLUDED.captured_at
"""

_CLEANUP_SQL = """
DELETE FROM live_wallet_flow_per_market WHERE hour_bucket < now() - make_interval(days => $1)
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
            "live_wallet_flow_fetch_failed",
            condition_id=condition_id,
            error=str(exc)[:200],
        )
        return []


def _aggregate_wallet_flow(
    trades: list[dict[str, Any]],
    smart_wallets: set[str],
) -> dict[datetime, dict[str, Any]]:
    """Per-bucket aggregates: takers, makers, whale split, buy pressure.

    Polymarket API trade format:
      proxyWallet — adres TAKER (тот кто инициировал — market order)
      maker — адрес MAKER (тот кто выставил лимит) — может отсутствовать
      side — BUY/SELL направление taker'а

    Returns: {hour: {...aggregates...}}
    """
    # Per-bucket: track unique takers/makers + per-wallet vol
    bucket_data: dict[datetime, dict[str, Any]] = {}

    for t in trades:
        try:
            ts = int(t.get("timestamp", 0))
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0 or price <= 0 or price >= 1:
            continue
        taker = (t.get("proxyWallet") or "").lower()
        maker = (t.get("maker") or "").lower()
        side = (t.get("side") or "").upper()
        if not taker:
            continue

        volume = size * price
        hour = datetime.fromtimestamp(ts, tz=UTC).replace(minute=0, second=0, microsecond=0)

        b = bucket_data.get(hour)
        if b is None:
            b = {
                "takers": set(),
                "makers": set(),
                "taker_count_total": 0,
                "whale_takers": set(),
                "whale_taker_volume": 0.0,
                "whale_buy_volume": 0.0,
                "whale_sell_volume": 0.0,
                "retail_taker_volumes": [],  # для avg_size
            }
            bucket_data[hour] = b

        b["takers"].add(taker)
        if maker:
            b["makers"].add(maker)
        b["taker_count_total"] += 1

        is_whale = taker in smart_wallets
        if is_whale:
            b["whale_takers"].add(taker)
            b["whale_taker_volume"] += volume
            if side == "BUY":
                b["whale_buy_volume"] += volume
            elif side == "SELL":
                b["whale_sell_volume"] += volume
        else:
            b["retail_taker_volumes"].append(volume)

    # Финализируем
    out: dict[datetime, dict[str, Any]] = {}
    for hour, b in bucket_data.items():
        whale_total = b["whale_buy_volume"] + b["whale_sell_volume"]
        whale_buy_pressure = (b["whale_buy_volume"] - b["whale_sell_volume"]) / whale_total if whale_total > 0 else 0.0
        retail_avg = (
            sum(b["retail_taker_volumes"]) / len(b["retail_taker_volumes"]) if b["retail_taker_volumes"] else None
        )
        out[hour] = {
            "n_unique_takers": len(b["takers"]),
            "n_unique_makers": len(b["makers"]),
            "whale_taker_volume": round(b["whale_taker_volume"], 4),
            "whale_taker_count": len(b["whale_takers"]),
            "retail_taker_avg_size": round(retail_avg, 4) if retail_avg is not None else None,
            "whale_buy_pressure": round(whale_buy_pressure, 4),
            "taker_count_total": b["taker_count_total"],
        }
    return out


async def _process_one_market(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    condition_id: str,
    since_ts: int,
    smart_wallets: set[str],
) -> tuple[str, list[tuple]]:
    async with sem:
        trades = await _safe_fetch_trades_since(client, condition_id, since_ts)
    if not trades:
        return condition_id, []
    flow = _aggregate_wallet_flow(trades, smart_wallets)
    rows = [
        (
            condition_id,
            hour,
            f["n_unique_takers"],
            f["n_unique_makers"],
            f["whale_taker_volume"],
            f["whale_taker_count"],
            f["retail_taker_avg_size"],
            f["whale_buy_pressure"],
            f["taker_count_total"],
        )
        for hour, f in flow.items()
    ]
    return condition_id, rows


async def run_live_wallet_flow_writer() -> dict[str, Any]:
    """Один тик: fetch trades + classify takers + UPSERT wallet_flow per market."""
    rows = await fetch(_ACTIVE_MARKETS_SQL, MIN_VOLUME_24H_USD)
    if not rows:
        logger.info("live_wallet_flow_no_active_markets")
        return {
            "markets": 0,
            "trades_fetched": 0,
            "buckets_upserted": 0,
            "smart_wallets": 0,
            "deleted_old": 0,
        }

    # Snapshot smart wallets set
    smart_rows = await fetch(_SMART_WALLETS_SQL)
    smart_wallets: set[str] = {(r["wallet"] or "").lower() for r in smart_rows if r["wallet"]}

    condition_ids = [r["condition_id"] for r in rows]
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
                since = now_unix - BOOTSTRAP_LOOKBACK_SEC
            else:
                since = int(last.timestamp()) - INCREMENTAL_OVERLAP_SEC
            tasks.append(_process_one_market(client, sem, cid, since, smart_wallets))

        results = await asyncio.gather(*tasks)

    upsert_rows: list[tuple] = []
    n_trades_total = 0
    for _cid, market_rows in results:
        # n_trades приблизительно = sum(taker_count_total) — из позиции 8
        n_trades_total += sum(r[8] for r in market_rows)
        upsert_rows.extend(market_rows)

    n_upserted = 0
    if upsert_rows:
        try:
            await executemany(_UPSERT_SQL, upsert_rows)
            n_upserted = len(upsert_rows)
        except Exception as exc:
            logger.error("live_wallet_flow_upsert_failed", error=str(exc)[:200], n=len(upsert_rows))

    n_deleted = 0
    try:
        result = await execute(_CLEANUP_SQL, RETENTION_DAYS)
        if isinstance(result, str) and result.startswith("DELETE "):
            n_deleted = int(result.split()[1])
    except Exception as exc:
        logger.warning("live_wallet_flow_cleanup_failed", error=str(exc)[:200])

    logger.info(
        "live_wallet_flow_done",
        markets=len(rows),
        trades_fetched=n_trades_total,
        buckets_upserted=n_upserted,
        smart_wallets=len(smart_wallets),
        deleted_old=n_deleted,
    )
    return {
        "markets": len(rows),
        "trades_fetched": n_trades_total,
        "buckets_upserted": n_upserted,
        "smart_wallets": len(smart_wallets),
        "deleted_old": n_deleted,
    }
