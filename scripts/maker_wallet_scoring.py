#!/usr/bin/env python3
"""
Maker Wallet Scoring — Phase 2 of maker-flow research pipeline.

Читает maker_flow.parquet → агрегирует статистику по maker-адресам →
скорит в тиры (whale / smart / retail / platform).

Запуск на VPS:
    python3 scripts/maker_wallet_scoring.py [--input /mnt/data/maker_flow/maker_flow.parquet]

Требования: duckdb
Артефакт: /mnt/data/maker_flow/maker_wallet_scores.parquet
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import duckdb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("maker_wallet_scoring")

INPUT_PARQUET = Path("/mnt/data/maker_flow/maker_flow.parquet")
OUTPUT_SCORES = Path("/mnt/data/maker_flow/maker_wallet_scores.parquet")

# Тир-пороги (калибровать по данным)
WHALE_MIN_USDC = 100_000 * 1e6        # >100K USDC total volume (raw units)
SMART_MIN_MARKETS = 5                  # торгует в ≥5 разных markets
SMART_MIN_WIN_RATE = 0.52              # win rate ≥52% (если resolve данные есть)
SMART_MIN_USDC = 10_000 * 1e6         # >10K USDC
RETAIL_MAX_USDC = 1_000 * 1e6         # <1K USDC

# Период "recency" — последние 30 дней считаются активными
RECENCY_DAYS = 30


def compute_scores(db: duckdb.DuckDBPyConnection, input_path: Path) -> int:
    """Агрегировать maker stats и записать scores parquet. Returns row count."""

    log.info(f"Reading: {input_path}")

    # Базовая статистика по каждому maker
    log.info("Computing maker aggregates...")
    db.execute(f"""
        CREATE OR REPLACE TABLE maker_stats AS
        SELECT
            maker,
            COUNT(*)                                        AS trade_count,
            COUNT(DISTINCT market_id)                       AS market_count,
            COUNT(DISTINCT CASE WHEN market_id != '' THEN market_id END) AS known_market_count,
            SUM(usdc_amount)                                AS total_usdc_raw,
            AVG(usdc_amount)                                AS avg_usdc_raw,
            MIN(timestamp)                                  AS first_trade_ts,
            MAX(timestamp)                                  AS last_trade_ts,
            -- направление: доля BUY сделок от maker
            AVG(CASE WHEN maker_direction = 'BUY' THEN 1.0 ELSE 0.0 END)  AS buy_ratio,
            -- активность по дням
            COUNT(DISTINCT DATE_TRUNC('day', TO_TIMESTAMP(timestamp))) AS active_days,
            -- медианный размер сделки
            MEDIAN(usdc_amount)                             AS median_usdc_raw,
            -- число маркетов с обеими сторонами (market hedging indicator)
            COUNT(DISTINCT CASE WHEN maker_direction = 'BUY' THEN market_id END)  AS buy_market_count,
            COUNT(DISTINCT CASE WHEN maker_direction = 'SELL' THEN market_id END) AS sell_market_count,
            -- концентрация: доля объёма в топ-1 маркете
            MAX(market_usdc) / NULLIF(SUM(usdc_amount), 0) AS top_market_concentration
        FROM read_parquet('{input_path}')
        CROSS JOIN LATERAL (
            SELECT SUM(usdc_amount) AS market_usdc
            FROM read_parquet('{input_path}') sub
            WHERE sub.maker = read_parquet('{input_path}').maker
            GROUP BY sub.market_id
            ORDER BY market_usdc DESC
            LIMIT 1
        )
        WHERE is_platform = false AND maker != ''
        GROUP BY maker
        HAVING trade_count >= 3
    """)

    # Упрощённый вариант без LATERAL (совместимость с DuckDB 1.x)
    # Пересчитываем top_market_concentration отдельно
    log.info("Computing top-market concentration...")
    db.execute(f"""
        CREATE OR REPLACE TABLE maker_top_market AS
        SELECT maker, MAX(vol) / SUM(vol) AS top_concentration
        FROM (
            SELECT maker, market_id, SUM(usdc_amount) AS vol
            FROM read_parquet('{input_path}')
            WHERE is_platform = false AND maker != '' AND market_id != ''
            GROUP BY maker, market_id
        )
        GROUP BY maker
    """)

    log.info("Merging stats...")
    db.execute(f"""
        CREATE OR REPLACE TABLE maker_stats AS
        SELECT
            maker,
            COUNT(*)                                         AS trade_count,
            COUNT(DISTINCT market_id)                        AS market_count,
            SUM(usdc_amount)                                 AS total_usdc_raw,
            AVG(usdc_amount)                                 AS avg_usdc_raw,
            MEDIAN(usdc_amount)                              AS median_usdc_raw,
            MIN(timestamp)                                   AS first_trade_ts,
            MAX(timestamp)                                   AS last_trade_ts,
            AVG(CASE WHEN maker_direction = 'BUY' THEN 1.0 ELSE 0.0 END) AS buy_ratio,
            COUNT(DISTINCT DATE_TRUNC('day', TO_TIMESTAMP(timestamp))) AS active_days,
            COUNT(DISTINCT CASE WHEN maker_direction = 'BUY' THEN market_id END)  AS buy_markets,
            COUNT(DISTINCT CASE WHEN maker_direction = 'SELL' THEN market_id END) AS sell_markets
        FROM read_parquet('{input_path}')
        WHERE is_platform = false AND maker != ''
        GROUP BY maker
        HAVING COUNT(*) >= 3
    """)

    # Join с concentration
    db.execute("""
        CREATE OR REPLACE TABLE maker_stats AS
        SELECT s.*, COALESCE(t.top_concentration, 1.0) AS top_market_concentration
        FROM maker_stats s
        LEFT JOIN maker_top_market t USING (maker)
    """)

    # Recency: последний трейд в пределах RECENCY_DAYS
    now_ts = int(time.time())
    recency_cutoff = now_ts - RECENCY_DAYS * 86400
    db.execute(f"""
        ALTER TABLE maker_stats ADD COLUMN IF NOT EXISTS is_recent BOOLEAN;
        UPDATE maker_stats SET is_recent = (last_trade_ts >= {recency_cutoff});
    """)

    # Тир-классификация
    log.info("Computing tiers...")
    db.execute(f"""
        CREATE OR REPLACE TABLE maker_scores AS
        SELECT
            maker,
            trade_count,
            market_count,
            total_usdc_raw,
            avg_usdc_raw,
            median_usdc_raw,
            first_trade_ts,
            last_trade_ts,
            buy_ratio,
            active_days,
            buy_markets,
            sell_markets,
            top_market_concentration,
            is_recent,
            -- Тир: whale > smart > retail (по убыванию приоритета)
            CASE
                WHEN total_usdc_raw >= {WHALE_MIN_USDC}
                    THEN 'whale'
                WHEN total_usdc_raw >= {SMART_MIN_USDC}
                    AND market_count >= {SMART_MIN_MARKETS}
                    AND top_market_concentration < 0.80
                    THEN 'smart'
                WHEN total_usdc_raw < {RETAIL_MAX_USDC}
                    THEN 'retail'
                ELSE 'mid'
            END AS tier,
            -- Скор [0..1]: нормализованный log-объём
            -- (будет откалиброван позже по resolved outcomes)
            LN(1 + total_usdc_raw) / 35.0 AS volume_score,
            -- Диверсификация [0..1]: больше маркетов → выше
            LEAST(1.0, market_count / 50.0) AS diversity_score,
            -- Частота: trades per active day
            trade_count::DOUBLE / NULLIF(active_days, 0) AS trade_frequency,
            -- Bidirectionality: насколько равномерно BUY/SELL
            1.0 - ABS(buy_ratio - 0.5) * 2.0 AS bidirectionality
        FROM maker_stats
    """)

    count = db.execute("SELECT COUNT(*) FROM maker_scores").fetchone()[0]
    log.info(f"Maker scores computed: {count:,} wallets")

    # Статистика по тирам
    tier_stats = db.execute("""
        SELECT tier, COUNT(*) as n,
               AVG(total_usdc_raw / 1e6) as avg_usdc,
               AVG(market_count) as avg_markets,
               AVG(trade_count) as avg_trades
        FROM maker_scores
        GROUP BY tier
        ORDER BY avg_usdc DESC
    """).fetchall()
    log.info("Tier breakdown:")
    for row in tier_stats:
        log.info(f"  {row[0]:8s}: n={row[1]:8,}  avg_usdc=${row[2]:,.0f}  "
                 f"avg_markets={row[3]:.1f}  avg_trades={row[4]:.1f}")

    # Export parquet
    log.info(f"Writing {OUTPUT_SCORES}...")
    db.execute(f"""
        COPY (
            SELECT * FROM maker_scores ORDER BY total_usdc_raw DESC
        ) TO '{OUTPUT_SCORES}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    size_mb = OUTPUT_SCORES.stat().st_size / 1e6
    log.info(f"Scores written: {OUTPUT_SCORES} ({size_mb:.1f} MB)")
    return count


def main():
    parser = argparse.ArgumentParser(description="Maker wallet scoring — Phase 2")
    parser.add_argument(
        "--input", type=Path, default=INPUT_PARQUET,
        help=f"Input maker_flow.parquet (default: {INPUT_PARQUET})"
    )
    parser.add_argument(
        "--output", type=Path, default=OUTPUT_SCORES,
        help=f"Output scores parquet (default: {OUTPUT_SCORES})"
    )
    args = parser.parse_args()

    global OUTPUT_SCORES
    OUTPUT_SCORES = args.output

    if not args.input.exists():
        log.error(f"Input not found: {args.input}")
        raise SystemExit(1)

    log.info("Phase 2: Maker Wallet Scoring")
    log.info(f"Input:  {args.input} ({args.input.stat().st_size / 1e9:.2f} GB)")
    log.info(f"Output: {OUTPUT_SCORES}")

    db = duckdb.connect(":memory:")
    db.execute("PRAGMA threads=4")
    db.execute("PRAGMA memory_limit='4GB'")

    n = compute_scores(db, args.input)
    db.close()

    log.info(f"Phase 2 done: {n:,} maker wallets scored → {OUTPUT_SCORES}")


if __name__ == "__main__":
    main()
