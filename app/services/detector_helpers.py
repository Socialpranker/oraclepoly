"""Shared helpers для cron scan jobs детекторов.

Раньше каждый detector scan job (`d5_scan.py`, `d7b_scan.py`, `d10b_scan.py`,
`d13_scan.py`, `d14_scan.py`, `d_esports_favourite_fade_scan.py`) определял
свои локальные `_bucket_tx_hash`, `_rank_signal`, плюс D13/D_esports —
свои `_fetch_market_trades`. Логика бит-в-бит идентична с микро-вариациями
параметров (bucket size, id namespace), которые теперь явные аргументы.

⚠ ВНИМАНИЕ: формулу `bucket_tx_hash` НЕЛЬЗЯ менять — она задаёт
idempotency ключ в БД. Любое изменение → дубликаты signals при collision.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from typing import Any

import httpx
import structlog

from app.core.data_api_limiter import acquire_data_api_slot
from app.core.retry import http_retry
from app.detectors.contract import Signal
from app.services.database import fetch

logger = structlog.get_logger()


# ─── Per-event dedup: open events query ──────────────────────────────────────


_OPEN_EVENTS_SQL = """
SELECT DISTINCT event_id
  FROM detector_signals
 WHERE detector = $1
   AND resolved = false
   AND event_id IS NOT NULL
"""


async def fetch_open_event_ids(detector_name: str) -> set[str]:
    """Return set of event_ids с unresolved signals для given detector.

    Используется в per-event dedup loop scan jobs: skip events где уже есть
    открытый сигнал, иначе outcomes коррелированы и мы получаем 3-25× inflation
    позиций (memory event_idempotency_research_2026-04-27).
    """
    rows = await fetch(_OPEN_EVENTS_SQL, detector_name)
    return {r["event_id"] for r in rows}


# ─── Polymarket data-api trades fetch (D13, D_esports) ───────────────────────

POLYMARKET_TRADES_URL = "https://data-api.polymarket.com/trades"


def bucket_tx_hash(
    *,
    bucket_id: str,
    ts: datetime,
    bucket_hours: int = 4,
    namespace: str = "",
) -> str:
    """Synthetic tx_hash для idempotency на N-часовой bucket.

    Args:
        bucket_id: market_id или event_id — что используется как dedup unit.
        ts: timestamp генерации (округлим до начала текущего bucket).
        bucket_hours: размер bucket'а (4 для большинства, 6 для D5).
        namespace: optional суффикс ("d13"/"d14"/"d_esports_fav_fade")
                   когда несколько детекторов могут эмитить на тот же
                   market в одном bucket — namespace разделяет их.

    ⚠ Формула должна оставаться backward-compatible с existing detector_signals
    rows. Schema: `{bucket_id}|{namespace}|{bucket_NhYYYYMMDDHH}` если namespace,
    иначе `{bucket_id}|{bucket_NhYYYYMMDDHH}`.
    """
    bucket = ts.strftime("%Y%m%d%H")
    bucket_aligned = f"{bucket[:8]}{int(bucket[8:]) // bucket_hours * bucket_hours:02d}"
    if namespace:
        raw = f"{bucket_id}|{namespace}|{bucket_aligned}"
    else:
        raw = f"{bucket_id}|{bucket_aligned}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:62]


def rank_signal(signal: Signal) -> float:
    """Top-1 selection ranking: confidence × (1/current_price) = expected payout edge.

    Используется когда per-event несколько markets кандидатов — выбираем один
    с максимальным confidence-weighted payout.
    """
    if signal.current_price <= 0:
        return 0.0
    return float(signal.confidence) * (1.0 / float(signal.current_price))


@http_retry
async def _fetch_trades_once(
    client: httpx.AsyncClient,
    condition_id: str,
    limit: int,
    timeout: float,
) -> list[dict[str, Any]]:
    """Single fetch с retry на 429/5xx (3 attempts, exp backoff 2→4→8s).

    Изолируем network call под декоратор чтобы все детекторы получали
    consistent rate-limit retry behavior.
    """
    await acquire_data_api_slot()
    resp = await client.get(
        POLYMARKET_TRADES_URL,
        params={"market": condition_id, "limit": limit},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json() or []


async def fetch_market_trades(
    client: httpx.AsyncClient,
    condition_id: str,
    *,
    cutoff_ts: int | None = None,
    limit: int = 500,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Fetch trades для одного market из Polymarket data-api.

    Network errors → [] (caller продолжит без trades).
    Retry-aware: 429 и 5xx auto-retry с backoff (см. _fetch_trades_once).
    """
    try:
        trades = await _fetch_trades_once(client, condition_id, limit, timeout)
    except Exception as exc:
        logger.warning("polymarket_trades_fetch_failed", condition_id=condition_id, error=str(exc))
        return []
    if cutoff_ts is None:
        return trades
    return [t for t in trades if int(t.get("timestamp", 0)) >= cutoff_ts]


async def fetch_trades_batch(
    condition_ids: list[str],
    *,
    cutoff_ts: int | None = None,
    limit: int = 500,
    concurrency: int = 2,  # 2026-05-13: 5→2 чтобы reduce 429 rate-limit hits
    timeout: float = 10.0,
) -> dict[str, list[dict[str, Any]]]:
    """Parallel fetch trades для списка markets с semaphore-bounded concurrency.

    Returns: {condition_id: [trades]}.
    """
    if not condition_ids:
        return {}

    sem = asyncio.Semaphore(concurrency)

    async def _fetch_with_sem(client: httpx.AsyncClient, cid: str):
        async with sem:
            return cid, await fetch_market_trades(client, cid, cutoff_ts=cutoff_ts, limit=limit, timeout=timeout)

    async with httpx.AsyncClient() as client:
        tasks = [_fetch_with_sem(client, cid) for cid in condition_ids]
        results = await asyncio.gather(*tasks)

    return dict(results)
