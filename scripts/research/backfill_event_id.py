"""Backfill event_id в detector_signals из HF parquet.

Матчинг: detector_signals.market_id (hex condition_id) → markets.parquet condition_id → event_id.

Покрытие (Phase 0): 197,812 / 198,145 уникальных market_ids (99.8%).

Запуск на VPS:
    cd ~/oracle
    python3 scripts/research/backfill_event_id.py --dry-run   # preview
    python3 scripts/research/backfill_event_id.py             # apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import asyncpg
import pyarrow.parquet as pq

HF_MARKETS_PATH = "/mnt/data/polymarket_dataset/markets.parquet"
BATCH_SIZE = 1000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def build_condition_to_event_id() -> dict[str, str]:
    """Читает markets.parquet, возвращает {condition_id_lower: event_id_str}."""
    log.info("Loading %s ...", HF_MARKETS_PATH)
    table = pq.read_table(HF_MARKETS_PATH, columns=["condition_id", "event_id"])
    df = table.to_pandas()
    df = df.dropna(subset=["condition_id", "event_id"])
    mapping = dict(zip(df["condition_id"].str.lower(), df["event_id"].astype(str)))
    log.info("Built mapping: %d entries", len(mapping))
    return mapping


async def run(db_url: str, dry_run: bool) -> None:
    conn = await asyncpg.connect(db_url)
    try:
        mapping = build_condition_to_event_id()

        # Загружаем только сигналы без event_id
        rows = await conn.fetch(
            "SELECT id, market_id FROM detector_signals WHERE event_id IS NULL ORDER BY id"
        )
        log.info("Signals without event_id: %d", len(rows))

        matched = 0
        unmatched_ids: list[str] = []
        updates: list[tuple[str, str]] = []  # (event_id, signal_id)

        for row in rows:
            event_id = mapping.get(row["market_id"].lower())
            if event_id:
                updates.append((event_id, str(row["id"])))
                matched += 1
            else:
                unmatched_ids.append(str(row["id"]))

        log.info("Matched: %d  |  Unmatched: %d", matched, len(unmatched_ids))

        if dry_run:
            log.info("DRY RUN — no changes written. Sample updates:")
            for ev, sid in updates[:5]:
                log.info("  signal %s → event_id=%s", sid, ev)
            return

        # Батчевый UPDATE
        total_updated = 0
        for i in range(0, len(updates), BATCH_SIZE):
            batch = updates[i : i + BATCH_SIZE]
            await conn.executemany(
                "UPDATE detector_signals SET event_id = $1 WHERE id = $2::uuid",
                batch,
            )
            total_updated += len(batch)
            log.info("Updated %d / %d ...", total_updated, matched)

        log.info("Done. Total updated: %d", total_updated)

        # Итоговая статистика
        stats = await conn.fetchrow(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(event_id) AS with_event_id,
              ROUND(100.0 * COUNT(event_id) / COUNT(*), 1) AS pct_filled
            FROM detector_signals
            WHERE resolved = true AND outcome IN ('WIN', 'LOSS')
            """
        )
        log.info(
            "Post-backfill stats (resolved): total=%s  with_event_id=%s  pct=%.1f%%",
            stats["total"],
            stats["with_event_id"],
            float(stats["pct_filled"]),
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL", ""))
    args = parser.parse_args()

    if not args.db_url:
        raise SystemExit("DATABASE_URL env var or --db-url required")

    asyncio.run(run(args.db_url, args.dry_run))
