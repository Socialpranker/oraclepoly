#!/usr/bin/env python3
"""Backfill detector_signals из исторических данных Polymarket.

Ускоряет L1 Beta calibration: вместо 14 дней shadow → дёргаем
полную историю /activity и/или oracle_snapshots → сразу N=1000+ training pairs.

Usage:
    # D1 (whale TX через Polymarket Data API /activity):
    python scripts/backfill_detector_signals.py d1 --start 2026-04-28 --end 2026-04-30
    python scripts/backfill_detector_signals.py d1 --days 30   # последние 30 дней

    # D3 (cross-mkt arb через oracle_snapshots):
    python scripts/backfill_detector_signals.py d3 --start 2026-04-28 --end 2026-04-30

    # Dry run (не пишет в БД, только статистика):
    python scripts/backfill_detector_signals.py d1 --days 7 --dry-run

D1 idempotency: UNIQUE по (detector, tx_hash) → повторный запуск безопасен.
D3 idempotency: UNIQUE по (detector, market_id, minute) → ок при повторе.

После backfill:
    python scripts/cron_runner.py detector-signals-resolve  # WIN/LOSS/EXPIRED
    python scripts/cron_runner.py ml-calibrator-fit         # L1 Beta cal
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backfill")

WINDOW_HOURS = 6  # API возвращает <=500 events / call → разрезаем на окна


def _parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _resolve_dates(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.days:
        end = datetime.now(UTC)
        start = end - timedelta(days=args.days)
        return start, end
    if not (args.start and args.end):
        raise SystemExit("--start + --end или --days обязательны")
    return _parse_date(args.start), _parse_date(args.end)


def _split_into_windows(start: datetime, end: datetime, hours: int) -> list[tuple[int, int]]:
    """Делит [start, end] на окна по N часов. Возвращает (unix_start, unix_end)."""
    windows: list[tuple[int, int]] = []
    cursor = start
    delta = timedelta(hours=hours)
    while cursor < end:
        nxt = min(cursor + delta, end)
        windows.append((int(cursor.timestamp()), int(nxt.timestamp())))
        cursor = nxt
    return windows


async def _ensure_market_id(condition_id: str, cache: dict[str, str | None]) -> str | None:
    if not condition_id:
        return None
    if condition_id in cache:
        return cache[condition_id]
    from app.services.database import fetchval

    market_id = await fetchval(
        "SELECT polymarket_id FROM markets WHERE condition_id = $1",
        condition_id,
    )
    cache[condition_id] = market_id
    return market_id


def _normalize_to_d1_payload(event: dict[str, Any], market_id: str) -> dict[str, Any]:
    """Конвертация /activity TRADE → формат, который ожидает d1_fade_whale.detect()."""
    return {
        "tx_hash": event.get("transactionHash") or "",
        "from_address": (event.get("proxyWallet") or "").lower(),
        "amount": str(event.get("usdcSize") or 0),
        "direction": (event.get("side") or "").lower(),
        "outcome": event.get("outcome") or "",
        "price": str(event.get("price") or 0),
        "market_id": market_id,
        "source": "backfill_activity",
    }


async def _backfill_d1(args: argparse.Namespace) -> dict[str, int]:
    from app.detectors import d1_fade_whale
    from app.repositories.detector_signal_repository import get_detector_signal_repository
    from app.services.polymarket_activity_service import get_polymarket_activity_service

    start, end = _resolve_dates(args)
    windows = _split_into_windows(start, end, WINDOW_HOURS)
    logger.info(
        "backfill_d1_started",
        extra={"start": start, "end": end, "windows": len(windows)},
    )
    activity = get_polymarket_activity_service()
    repo = get_detector_signal_repository()
    market_cache: dict[str, str | None] = {}

    fetched = candidates = signals = inserted = unmapped = 0

    for w_start, w_end in windows:
        events = await activity.iterate_window(
            start=w_start,
            end=w_end,
            activity_type="TRADE",
            page_size=500,
            rate_delay=0.2,
        )
        fetched += len(events)
        if not events:
            continue

        for event in events:
            try:
                if Decimal(str(event.get("usdcSize") or 0)) < Decimal("5000"):
                    continue
                if (event.get("side") or "").upper() != "BUY":
                    continue
            except (TypeError, ValueError):
                continue
            candidates += 1

            condition_id = event.get("conditionId") or ""
            market_id = await _ensure_market_id(condition_id, market_cache)
            if not market_id:
                unmapped += 1
                continue

            payload = _normalize_to_d1_payload(event, market_id)
            signal = d1_fade_whale.detect(payload)
            if signal is None:
                continue

            ts = int(event.get("timestamp") or 0)
            if ts:
                from dataclasses import replace
                signal = replace(
                    signal,
                    generated_at=datetime.fromtimestamp(ts, tz=UTC),
                )

            signals += 1
            if args.dry_run:
                continue
            try:
                if await repo.insert(signal):
                    inserted += 1
            except Exception as exc:
                logger.warning("backfill_d1_insert_failed: %s", exc)

        logger.info(
            "backfill_d1_window_done",
            extra={
                "w_start": w_start,
                "w_end": w_end,
                "fetched_so_far": fetched,
                "signals_so_far": signals,
            },
        )

    return {
        "windows": len(windows),
        "fetched": fetched,
        "candidates": candidates,
        "unmapped_market": unmapped,
        "signals_emitted": signals,
        "inserted": inserted,
    }


async def _backfill_d1_from_db(args: argparse.Namespace) -> dict[str, int]:
    """D1 backfill из локальной transactions table.

    Используется когда Polymarket /activity API недоступен (post-V2 cutover April 2026
    требует per-wallet user= parameter, глобальный TRADE feed не работает).

    Источник: наша WS ingestion пишет каждый whale TX (≥$5K) в `transactions`.
    Берём BUY-операции, конвертируем в формат d1_fade_whale.detect().
    """
    from app.detectors import d1_fade_whale
    from app.repositories.detector_signal_repository import get_detector_signal_repository
    from app.services.database import fetch

    start, end = _resolve_dates(args)
    logger.info("backfill_d1_from_db_started", extra={"start": start, "end": end})

    rows = await fetch(
        """
        SELECT t.tx_hash,
               t.from_address,
               t.amount,
               t.direction,
               t.outcome,
               t.price,
               t.block_timestamp,
               m.polymarket_id AS market_id
          FROM transactions t
          JOIN markets m ON m.id = t.market_id
         WHERE t.block_timestamp BETWEEN $1 AND $2
           AND t.amount >= 5000
           AND lower(t.direction) = 'buy'
           AND t.tx_hash IS NOT NULL
           AND t.tx_hash <> ''
        """,
        start,
        end,
    )

    repo = get_detector_signal_repository()
    candidates = signals = inserted = 0
    for r in rows:
        candidates += 1
        payload = {
            "tx_hash": r["tx_hash"],
            "from_address": (r["from_address"] or "").lower(),
            "amount": str(r["amount"]),
            "direction": (r["direction"] or "").lower(),
            "outcome": r["outcome"] or "",
            "price": str(r["price"] or 0),
            "market_id": r["market_id"],
            "source": "backfill_db",
        }
        signal = d1_fade_whale.detect(payload)
        if signal is None:
            continue
        ts = r.get("block_timestamp")
        if ts:
            from dataclasses import replace
            signal = replace(signal, generated_at=ts if ts.tzinfo else ts.replace(tzinfo=UTC))
        signals += 1
        if args.dry_run:
            continue
        try:
            if await repo.insert(signal):
                inserted += 1
        except Exception as exc:
            logger.warning("backfill_d1_db_insert_failed: %s", exc)

    return {
        "rows_loaded": len(rows),
        "candidates": candidates,
        "signals_emitted": signals,
        "inserted": inserted,
    }


async def _backfill_d3(args: argparse.Namespace) -> dict[str, int]:
    """D3 backfill через oracle_snapshots.

    Группируем snapshots по (event_polymarket_id, snapshot_at), считаем
    sum(YES) на батче, пропускаем через d3_cross_mkt_arb.detect().

    Используем только TRUE neg-risk events (e.neg_risk=true, neg_risk_other=false).
    Это требует чтобы neg-risk sync уже отработал.
    """
    from app.detectors import d3_cross_mkt_arb
    from app.detectors.d3_cross_mkt_arb import EventMarket
    from app.repositories.detector_signal_repository import get_detector_signal_repository
    from app.services.database import fetch

    start, end = _resolve_dates(args)
    logger.info("backfill_d3_started", extra={"start": start, "end": end})

    rows = await fetch(
        """
        SELECT e.polymarket_id      AS event_id,
               m.polymarket_id      AS market_id,
               m.clob_token_ids,
               os.market_price      AS current_price,
               date_trunc('minute', os.created_at) AS bucket
          FROM oracle_snapshots os
          JOIN markets m ON m.id = os.market_id
          JOIN events  e ON e.id = m.event_id
         WHERE e.neg_risk = true
           AND e.neg_risk_other = false
           AND e.n_yes_winners = 1
           AND os.created_at BETWEEN $1 AND $2
           AND os.market_price IS NOT NULL
        """,
        start,
        end,
    )

    by_bucket: dict[tuple[str, datetime], list[EventMarket]] = {}
    for r in rows:
        key = (r["event_id"], r["bucket"])
        tokens = r.get("clob_token_ids") or []
        token_id = str(tokens[0]) if tokens else None
        by_bucket.setdefault(key, []).append(
            EventMarket(
                market_id=r["market_id"],
                yes_price=float(r["current_price"]),
                token_id=token_id,
            )
        )

    repo = get_detector_signal_repository()
    signals = 0
    inserted = 0
    for (event_id, bucket_ts), markets in by_bucket.items():
        if len(markets) < 2:
            continue
        emitted = d3_cross_mkt_arb.detect(
            event_id=event_id,
            markets=markets,
            neg_risk=True,
            neg_risk_other=False,
            n_yes_winners=1,
        )
        for sig in emitted:
            from dataclasses import replace
            sig = replace(sig, generated_at=bucket_ts.replace(tzinfo=UTC) if bucket_ts.tzinfo is None else bucket_ts)
            signals += 1
            if args.dry_run:
                continue
            try:
                if await repo.insert(sig):
                    inserted += 1
            except Exception as exc:
                logger.warning("backfill_d3_insert_failed: %s", exc)

    return {
        "buckets": len(by_bucket),
        "rows_loaded": len(rows),
        "signals_emitted": signals,
        "inserted": inserted,
    }


async def _main(args: argparse.Namespace) -> int:
    from app.core.cache import close_redis, init_redis
    from app.core.config import get_settings
    from app.services.database import close_db, init_db

    settings = get_settings()
    await init_db()
    await init_redis(settings.redis_url)
    try:
        if args.detector == "d1":
            stats = await _backfill_d1(args)
        elif args.detector == "d1-db":
            stats = await _backfill_d1_from_db(args)
        elif args.detector == "d3":
            stats = await _backfill_d3(args)
        else:
            raise SystemExit(f"unknown detector: {args.detector}")

        logger.info("backfill_done", extra=stats)
        for k, v in stats.items():
            print(f"  {k}: {v}")
        return 0
    finally:
        await close_db()
        await close_redis()


def _parse_cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("detector", choices=["d1", "d1-db", "d3"])
    p.add_argument("--start", help="ISO date (YYYY-MM-DD)")
    p.add_argument("--end", help="ISO date (YYYY-MM-DD)")
    p.add_argument("--days", type=int, help="last N days")
    p.add_argument("--dry-run", action="store_true", help="не пишет в БД")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_cli()
    sys.exit(asyncio.run(_main(args)))
