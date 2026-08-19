#!/usr/bin/env python3
"""Bootstrap live_trade_buckets и live_wallet_flow_per_market одноразовым fill.

Запускается ВРУЧНУЮ один раз перед включением cron'ов live writer'ов.
Просто вызывает run_live_trade_bucket_writer + run_live_wallet_flow_writer
БЕЗ предыдущей истории — writer'ы сами определят что bootstrap path
(MAX(hour_bucket) IS NULL → 30-day lookback).

Время: 30-60 минут для ~50-100 active markets, поскольку pagination до 30 дней истории.

Запуск на VPS:
    cd ~/oracle && docker exec oraclepoly-backend python scripts/research/bootstrap_live_buckets.py

См. docs/superpowers/plans/bright-hopping-meteor.md (Day 3).
"""

from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


async def main() -> int:
    log.info("Bootstrap live_trade_buckets...")
    from app.jobs.live_trade_bucket_writer import run_live_trade_bucket_writer

    result_buckets = await run_live_trade_bucket_writer()
    log.info("trade_buckets done: %s", result_buckets)

    log.info("Bootstrap live_wallet_flow_per_market...")
    from app.jobs.live_wallet_flow_writer import run_live_wallet_flow_writer

    result_wflow = await run_live_wallet_flow_writer()
    log.info("wallet_flow done: %s", result_wflow)

    if result_buckets["buckets_upserted"] == 0:
        log.warning("0 buckets upserted — check Polymarket API connectivity / active markets list")
        return 1
    if result_wflow["buckets_upserted"] == 0:
        log.warning("0 wallet_flow buckets upserted — check wallet_smart_money table populated")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
