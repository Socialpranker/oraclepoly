#!/usr/bin/env python3
"""Backfill detector_signals из HuggingFace dataset (full_hf_enriched.parquet).

Задача: за один проход сформировать train set для L1 Beta + L2 LightGBM
из готового бэктест-датасета 2022-2026, минуя shadow-mode сбор и resolve cron.

Что в HF (`/mnt/data/polymarket_dataset/full_hf_enriched.parquet`):
- 532K rows, каждая = одна попытка fade-whale на конкретный market
- Колонки: market_id, side='NO', entry_price, market_price (= what would have been after horizon),
  category, created_at, end_date, win (0/1), whale_tx_count, volume, vwap,
  taker_buy_ratio, maker_direction_ratio
- 162K rows с whale_tx_count >= 1 — наш target population
- WR 53% базовый, но в high-zone (0.7-0.9) WR 73%, в extreme (0.9+) WR 83%

Логика backfill:
1. Читаем parquet через DuckDB (efficient column-pruned scan)
2. Фильтруем:
   - whale_tx_count >= 1 (есть whale-trade)
   - 0.10 <= entry_price < 0.85 (соблюдаем D1 entry boundaries)
   - created_at >= --since (по умолчанию 2024-01-01)
   - category != 'Minute Markets' (WR 44%, отрицательный edge)
3. Для каждой строки конструируем Signal:
   - detector: 'D1_fade_whale'
   - side: 'NO' (HF — NO-fade only)
   - confidence: 0.55 + 0.30 × min(1, whale_tx_count / 5)  (proxy за amount/25K)
   - entry_price из HF, fair_price из zone edge
   - generated_at = HF.created_at
4. INSERT в detector_signals + сразу UPDATE outcome+realized_roi
   (минуем resolve cron, у нас уже есть истина в HF.win)
5. Idempotency: synthetic tx_hash = sha256(market_id + iso(created_at))

Usage:
    docker exec oraclepoly-backend python scripts/backfill_from_hf.py
    docker exec oraclepoly-backend python scripts/backfill_from_hf.py --since 2025-01-01
    docker exec oraclepoly-backend python scripts/backfill_from_hf.py --dry-run

Exit:
    0 — backfill done, signals inserted
    1 — error (parquet not found, DB unreachable)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_hf")

PARQUET_PATH = "/mnt/data/polymarket_dataset/full_hf_enriched.parquet"
DEFAULT_SINCE = "2024-01-01"
D1_DETECTOR_NAME = "D1_fade_whale"
D4_DETECTOR_NAME = "D4_small_market_fade"
D6_DETECTOR_NAME = "D6_retail_bullish_overshoot"

D1_HORIZON_SEC = 30 * 60
D4_HORIZON_SEC = 6 * 60 * 60
D6_HORIZON_SEC = 12 * 60 * 60

D1_ZONE_EDGE_PCT: dict[str, float] = {
    "longshot": 76.7,
    "low": 75.5,
    "mid": 80.4,
    "high": 103.8,
}

# D4: per-bucket WR + dynamic edge (re-uses constants из app.detectors.d4)
# Импортируется внутри _backfill чтобы избежать циклических зависимостей
# при загрузке без app context (тесты).


def _zone(price: float) -> str | None:
    if price < 0.10:
        return None
    if price < 0.30:
        return "longshot"
    if price < 0.50:
        return "low"
    if price < 0.70:
        return "mid"
    if price < 0.85:
        return "high"
    return None


def _confidence(whale_tx_count: int) -> float:
    """Proxy: чем больше whale-tx на маркете, тем выше confidence.

    Для in-process detector confidence = 0.55 + 0.30 × min(1, amount/25000).
    В HF amount нет, используем whale_tx_count как количественный
    индикатор whale-активности (1 → 0.61, 5+ → 0.85).
    """
    size_factor = min(1.0, whale_tx_count / 5.0)
    return round(0.55 + 0.30 * size_factor, 4)


def _fair_price_no(entry_price_no: float, edge_pct: float) -> float:
    delta = (edge_pct / 100.0) * entry_price_no
    return round(max(0.0, min(1.0, entry_price_no - delta)), 4)


def _realized_roi_pct(win: int, entry_price: float) -> float:
    """ROI в % на $1 paper-stake (как в detector_signals_resolve)."""
    if win == 1:
        if entry_price <= 0:
            return 0.0
        return round((1.0 - entry_price) / entry_price * 100.0, 3)
    return -100.0


def _synthetic_tx_hash(market_id: str, created_at: datetime) -> str:
    raw = f"{market_id}|{created_at.isoformat()}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:62]


async def _backfill(since: str, dry_run: bool, detector: str, batch_size: int = 1000) -> dict[str, int]:
    """Главная backfill процедура.

    Читает HF parquet через DuckDB, конструирует synthetic signals,
    INSERT'ит batch'ами с outcome+realized_roi уже заполненными.

    detector: 'd1' (whale fade, whale_tx_count >= 1) или 'd4' (small-market fade,
              whale_tx_count = 0, volume < 5000, category in whitelist).
    """
    import json

    import duckdb  # на VPS установлен через requirements.txt

    from app.detectors.d4_small_market_fade import BUCKET_WR as D4_BUCKET_WR
    from app.detectors.d6_retail_bullish_overshoot import BUCKET_WR as D6_BUCKET_WR
    from app.detectors.d6_retail_bullish_overshoot import _horizon_band as d6_horizon_band
    from app.detectors.d6_retail_bullish_overshoot import _strength as d6_strength
    from app.detectors.d6_retail_bullish_overshoot import _zone as d6_zone
    from app.services.database import execute

    if not Path(PARQUET_PATH).exists():
        raise SystemExit(f"parquet not found: {PARQUET_PATH}")

    if detector == "d1":
        whale_filter = "whale_tx_count >= 1"
        category_filter = "category IS NOT NULL AND category != 'Minute Markets'"
        bucket_filter = ""  # для D1 не нужно (любая зона)
    elif detector == "d4":
        whale_filter = "whale_tx_count = 0 AND volume < 5000"
        # D4 per-bucket whitelist (CATEGORY_ZONE_CONFIG из app.detectors.d4_small_market_fade):
        #  Culture: high+mid (entry >= 0.50)
        #  Politics, Geopolitics, Sports: high only (entry >= 0.70)
        #  Weather: high+mid (entry >= 0.50)
        category_filter = "true"  # bucket_filter заменяет category whitelist
        bucket_filter = """
          AND (
            (category IN ('Culture', 'Weather') AND entry_price >= 0.50)
            OR (category IN ('Politics', 'Geopolitics', 'Sports') AND entry_price >= 0.70)
          )
        """
    elif detector == "d5":
        whale_filter = "true"  # D5 не зависит от whale активности
        # Exclude noisy categories
        category_filter = "category NOT IN ('Other', 'Minute Markets')"
        # entry 0.70-0.99, horizon ≥ 3 days
        bucket_filter = """
          AND entry_price >= 0.70 AND entry_price < 0.99
          AND horizon_days >= 3.0
        """
    elif detector == "d6":
        whale_filter = "true"
        category_filter = "category NOT IN ('Other', 'Minute Markets')"
        # D6: entry 0.70-0.99, horizon ≥ 3 days, maker_direction_ratio >= 0.20
        bucket_filter = """
          AND entry_price >= 0.70 AND entry_price < 0.99
          AND horizon_days >= 3.0
          AND maker_direction_ratio IS NOT NULL
          AND maker_direction_ratio >= 0.20
        """
    else:
        raise SystemExit(f"unknown detector: {detector}")

    con = duckdb.connect()
    # D6 нужен entry_price до 0.99 (extreme zone), остальные детекторы — до 0.85
    entry_upper = "0.99" if detector == "d6" else "0.85"
    query = f"""
        SELECT
            market_id,
            entry_price,
            category,
            created_at,
            end_date,
            win,
            whale_tx_count,
            volume,
            horizon_days,
            maker_direction_ratio
        FROM read_parquet('{PARQUET_PATH}')
        WHERE {whale_filter}
          AND entry_price >= 0.10 AND entry_price < {entry_upper}
          AND created_at >= '{since}'
          AND {category_filter}
          AND win IN (0, 1)
          {bucket_filter}
        ORDER BY created_at ASC
    """
    rows = con.execute(query).fetchall()
    cols = [d[0] for d in con.execute(f"DESCRIBE {query}").fetchall()]
    logger.info("hf_loaded rows=%s columns=%s", len(rows), cols)

    if not rows:
        return {"loaded": 0, "inserted": 0, "skipped": 0}

    inserted = 0
    skipped_no_zone = 0
    skipped_dup = 0
    sql_insert = """
        INSERT INTO detector_signals (
            detector, market_id, event_id, side,
            confidence, expected_edge_pct, fair_price, current_price,
            horizon_sec, token_id, evidence, generated_at,
            resolved, outcome, realized_roi, resolved_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12,
                true, $13, $14, now())
        ON CONFLICT DO NOTHING
    """

    batch: list[tuple] = []
    for r in rows:
        rec = dict(zip(cols, r))
        entry = float(rec["entry_price"])
        # D6 использует свой _zone (high/extreme), остальные — стандартный
        if detector == "d6":
            zone = d6_zone(entry)
        else:
            zone = _zone(entry)
        if zone is None:
            skipped_no_zone += 1
            continue

        market_id = str(rec["market_id"])
        created_at = rec["created_at"]
        if hasattr(created_at, "tzinfo") and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)

        wtc = int(rec["whale_tx_count"] or 0)
        category = rec["category"]

        # detector-specific:
        if detector == "d1":
            detector_name = D1_DETECTOR_NAME
            confidence = _confidence(wtc)
            edge_pct = D1_ZONE_EDGE_PCT[zone]
            horizon_sec = D1_HORIZON_SEC
        elif detector == "d4":
            detector_name = D4_DETECTOR_NAME
            bucket_wr = D4_BUCKET_WR.get((category, zone))
            if bucket_wr is None:
                # Bucket not in whitelist (SQL filter должен это уже отсечь, но
                # safety net на случай data drift). Skip.
                skipped_no_zone += 1
                continue
            confidence = round(bucket_wr, 4)
            entry_price_no_d4 = round(1.0 - entry, 4)
            if entry_price_no_d4 <= 0:
                skipped_no_zone += 1
                continue
            payout = 1.0 / entry_price_no_d4
            edge_pct = round((bucket_wr * payout - 1) * 100, 2)
            horizon_sec = D4_HORIZON_SEC
        else:  # d6
            detector_name = D6_DETECTOR_NAME
            horizon_days = float(rec.get("horizon_days") or 0)
            mdr_raw = rec.get("maker_direction_ratio")
            if mdr_raw is None:
                skipped_no_zone += 1
                continue
            mdr = float(mdr_raw)
            band = d6_horizon_band(horizon_days)
            d6_z = d6_zone(entry)
            strength = d6_strength(mdr)
            if band is None or d6_z is None or strength is None:
                skipped_no_zone += 1
                continue
            bucket_wr = D6_BUCKET_WR.get((band, d6_z, strength))
            if bucket_wr is None:
                skipped_no_zone += 1
                continue
            confidence = round(bucket_wr, 4)
            entry_price_no_d6 = round(1.0 - entry, 4)
            if entry_price_no_d6 <= 0:
                skipped_no_zone += 1
                continue
            payout = 1.0 / entry_price_no_d6
            edge_pct = round((bucket_wr * payout - 1) * 100, 2)
            horizon_sec = D6_HORIZON_SEC

        # NO-side: entry_price_no = 1 - entry_price (HF.entry_price = YES-цена)
        entry_price_no = round(1.0 - entry, 4)
        fair = _fair_price_no(entry_price_no, edge_pct)

        win = int(rec["win"])
        outcome_label = "WIN" if win == 1 else "LOSS"
        roi = _realized_roi_pct(win, entry_price_no)

        evidence = {
            "source": "backfill_hf",
            "tx_hash": _synthetic_tx_hash(market_id, created_at),
            "zone": zone,
            "category": category,
            "bucket_key": f"{category}_{zone}",
            "volume_24h": float(rec["volume"] or 0),
            "horizon_days": float(rec["horizon_days"] or 0),
            "whale_tx_count": wtc,
            "hf_entry_price_yes": entry,
        }

        batch.append((
            detector_name,
            market_id,
            None,
            "NO",
            confidence,
            float(edge_pct),
            fair,
            entry_price_no,
            horizon_sec,
            None,
            json.dumps(evidence),
            created_at,
            outcome_label,
            roi,
        ))

        if len(batch) >= batch_size:
            if not dry_run:
                inserted += await _flush_batch(execute, sql_insert, batch)
            else:
                inserted += len(batch)
            batch.clear()
            if inserted % 5000 == 0:
                logger.info("backfill_progress inserted=%s", inserted)

    if batch and not dry_run:
        inserted += await _flush_batch(execute, sql_insert, batch)
    elif batch:
        inserted += len(batch)

    return {
        "loaded": len(rows),
        "inserted": inserted,
        "skipped_no_zone": skipped_no_zone,
        "skipped_dup": skipped_dup,
    }


async def _flush_batch(execute_fn, sql: str, batch: list[tuple]) -> int:
    """INSERT всех строк в batch. Возвращает количество вставленных (rows affected суммарно)."""
    n = 0
    for params in batch:
        result = await execute_fn(sql, *params)
        if isinstance(result, str) and result.startswith("INSERT 0 1"):
            n += 1
    return n


async def _main(args: argparse.Namespace) -> int:
    from app.core.cache import close_redis, init_redis
    from app.core.config import get_settings
    from app.services.database import close_db, init_db

    settings = get_settings()
    await init_db()
    await init_redis(settings.redis_url)
    try:
        stats = await _backfill(since=args.since, dry_run=args.dry_run, detector=args.detector)
        logger.info("backfill_done detector=%s %s", args.detector, stats)
        for k, v in stats.items():
            print(f"  {k}: {v}")
        return 0
    finally:
        await close_db()
        await close_redis()


def _parse_cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--detector", choices=["d1", "d4", "d5", "d6"], default="d1",
                   help="d1 — whale fade; d4 — small-market fade; d5 — long-horizon extreme fade; "
                        "d6 — retail bullish overshoot")
    p.add_argument("--since", default=DEFAULT_SINCE, help="ISO date (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true", help="не пишет в БД")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_cli()
    sys.exit(asyncio.run(_main(args)))
