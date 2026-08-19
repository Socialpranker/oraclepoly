#!/usr/bin/env python3
"""Build canonical HF dataset для всех future detector research.

Источники (SII-WANGZJ/Polymarket_data):
  - markets.parquet (~1M markets metadata, ~165 MB)
  - quant.parquet   (766M trades 2022-11 → 2026-05, ~28 GB)

Адаптация 2026-05-11: убрана зависимость от market_enriched.parquet
(не доступен в публичных HF dumps). yes_won derived из outcome_prices.
category derived из event_title keywords.

Output (/mnt/data/canonical/):
  1. markets_clean.parquet — один row per market, resolved + vol>=$1K, period 2024+
  2. price_snapshots.parquet — yes_price на hours_to_resolve = {4,8,12,24,48,72,168,720}h
  3. trade_buckets.parquet — hourly aggregates всех trades (VPIN/OFI ready)

Период: 2024-01-01 → 2026-05-05 (свежие данные post-V2 cutover).

Запуск на VPS:
    cd ~/oracle && .venv-research/bin/python scripts/research/build_canonical_hf.py
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import duckdb

# ── Paths ────────────────────────────────────────────────────────────────────

QUANT_PATH   = "/mnt/data/hf_data/quant.parquet"
MARKETS_PATH = "/mnt/data/hf_data/markets.parquet"

OUT_DIR = Path("/mnt/data/canonical")
OUT_MARKETS   = OUT_DIR / "markets_clean.parquet"
OUT_SNAPSHOTS = OUT_DIR / "price_snapshots.parquet"
OUT_BUCKETS   = OUT_DIR / "trade_buckets.parquet"

# ── Filters ──────────────────────────────────────────────────────────────────

PERIOD_START = "2024-01-01"
PERIOD_END = "2026-05-05"  # quant.parquet max ts
PERIOD_START_TS = int(datetime.fromisoformat(PERIOD_START).timestamp())
MIN_VOLUME_USD = 1000

# Snapshot hours_to_resolve points
SNAPSHOT_HOURS = [4, 8, 12, 24, 48, 72, 168, 720]  # 4h ... 30d


# Category derivation from event_title + question keywords.
# Order matters — first match wins. Mirrors enriched dataset taxonomy.
CATEGORY_RULES_SQL = """
CASE
    -- Crypto (highest priority — many BTC/ETH/SOL minute markets)
    WHEN regexp_matches(lower(coalesce(event_title,'') || ' ' || coalesce(question,'')),
        '\\b(bitcoin|btc|ethereum|eth\\b|solana|sol\\b|xrp|dogecoin|crypto|altcoin|memecoin|binance|coinbase)\\b')
        THEN 'Crypto'
    -- Minute Markets (BTC/ETH up-or-down 5-min — but Crypto wins above for clean split)
    WHEN regexp_matches(lower(coalesce(question,'')), '\\b(up or down|minute|5min|hourly close)\\b')
        THEN 'Minute Markets'
    -- Sports
    WHEN regexp_matches(lower(coalesce(event_title,'') || ' ' || coalesce(question,'')),
        '\\b(nba|nfl|nhl|mlb|ufc|mma|boxing|soccer|football|basketball|baseball|hockey|cricket|tennis|golf|f1|formula|tour de france|world cup|playoff|super bowl|finals|championship|game [0-9]|set [0-9]|match|fight|knockout|champion|league)\\b')
        THEN 'Sports'
    -- Politics
    WHEN regexp_matches(lower(coalesce(event_title,'') || ' ' || coalesce(question,'')),
        '\\b(election|trump|biden|harris|vance|musk|senate|congress|president|governor|primary|caucus|nominee|democrat|republican|gop|supreme court|impeach|cabinet|secretary of|department of|federal reserve|powell|fed chair)\\b')
        THEN 'Politics'
    -- Geopolitics
    WHEN regexp_matches(lower(coalesce(event_title,'') || ' ' || coalesce(question,'')),
        '\\b(ukraine|russia|putin|china|taiwan|israel|gaza|iran|nato|war\\b|invasion|ceasefire|treaty|sanction|nuclear|hostage|hamas|nuke|missile|invade)\\b')
        THEN 'Geopolitics'
    -- Tech & AI
    WHEN regexp_matches(lower(coalesce(event_title,'') || ' ' || coalesce(question,'')),
        '\\b(openai|chatgpt|gpt-?[0-9]|claude|anthropic|google|gemini|meta\\b|llm|ai\\b|artificial intelligence|tesla|spacex|nvidia|apple|microsoft|amazon|tech)\\b')
        THEN 'Tech & AI'
    -- Weather
    WHEN regexp_matches(lower(coalesce(event_title,'') || ' ' || coalesce(question,'')),
        '\\b(hurricane|tornado|snow|rain|temperature|weather|storm|wildfire|earthquake|tsunami|flood|climate)\\b')
        THEN 'Weather'
    -- Finance / Markets
    WHEN regexp_matches(lower(coalesce(event_title,'') || ' ' || coalesce(question,'')),
        '\\b(s&p|dow|nasdaq|spy|qqq|stock|ipo|earnings|recession|gdp|cpi|inflation|interest rate|treasury|bond|yield)\\b')
        THEN 'Finance'
    -- Culture
    WHEN regexp_matches(lower(coalesce(event_title,'') || ' ' || coalesce(question,'')),
        '\\b(oscar|grammy|emmy|academy award|netflix|movie|film|album|song|tour|concert|celebrity|kardashian|swift|drake|beyonce|tiktok|youtube|streaming)\\b')
        THEN 'Culture'
    ELSE 'Other'
END
"""


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Build canonical HF dataset → {OUT_DIR}", flush=True)
    print(f"Period: {PERIOD_START} → {PERIOD_END}, min_volume ${MIN_VOLUME_USD}", flush=True)

    con = duckdb.connect()
    con.execute('SET memory_limit="5GB"')
    con.execute('SET threads=4')
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("PRAGMA max_temp_directory_size='50GiB'")

    # ── STAGE 1: markets_clean ──────────────────────────────────────────────
    t0 = time.time()
    if OUT_MARKETS.exists():
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT_MARKETS}')").fetchone()[0]
        print(f"\n[1/3] markets_clean.parquet exists, skip ({n:,} rows)", flush=True)
    else:
        print("\n[1/3] Building markets_clean.parquet...", flush=True)
        con.execute(f"""
            COPY (
                WITH parsed_outcome AS (
                    SELECT
                        m.condition_id,
                        m.event_id,
                        m.question,
                        m.event_title,
                        {CATEGORY_RULES_SQL}                 AS category,
                        CAST(m.neg_risk AS BOOLEAN)          AS neg_risk,
                        CAST(m.created_at AS TIMESTAMP)      AS created_at,
                        CAST(m.end_date AS TIMESTAMP)        AS end_date,
                        m.volume                             AS lifetime_volume_usd,
                        EXTRACT(EPOCH FROM (m.end_date - m.created_at)) / 86400.0 AS horizon_days,
                        -- yes_won: outcome_prices[0] = YES probability.
                        -- "['1', '0']" → YES won (1); "['0', '1']" → NO won (0).
                        -- Edge resolutions ("['0.9995','0.0005']") → treat as definitive ≥0.95.
                        CASE
                            WHEN m.outcome_prices IN ('[''1'', ''0'']', '[''1.0'', ''0.0'']') THEN 1
                            WHEN m.outcome_prices IN ('[''0'', ''1'']', '[''0.0'', ''1.0'']') THEN 0
                            WHEN m.outcome_prices LIKE '[%'
                                AND TRY_CAST(SPLIT_PART(REPLACE(REPLACE(REPLACE(m.outcome_prices,'[',''),']',''),'''',''), ',', 1) AS DOUBLE) >= 0.95 THEN 1
                            WHEN m.outcome_prices LIKE '[%'
                                AND TRY_CAST(SPLIT_PART(REPLACE(REPLACE(REPLACE(m.outcome_prices,'[',''),']',''),'''',''), ',', 2) AS DOUBLE) >= 0.95 THEN 0
                            ELSE NULL
                        END                                  AS yes_won
                    FROM read_parquet('{MARKETS_PATH}') m
                    WHERE m.closed = 1
                      AND m.volume >= {MIN_VOLUME_USD}
                      AND m.end_date >= TIMESTAMP '{PERIOD_START}'
                      AND m.end_date < TIMESTAMP '{PERIOD_END}'
                      AND m.condition_id IS NOT NULL
                      AND m.outcome_prices IS NOT NULL
                ),
                with_trade_count AS (
                    SELECT
                        p.*,
                        COALESCE(t.n_trades_total, 0) AS n_trades_total
                    FROM parsed_outcome p
                    LEFT JOIN (
                        SELECT condition_id, COUNT(*) AS n_trades_total
                        FROM read_parquet('{QUANT_PATH}')
                        WHERE timestamp >= {PERIOD_START_TS}
                        GROUP BY condition_id
                    ) t ON t.condition_id = p.condition_id
                )
                SELECT * FROM with_trade_count
                WHERE n_trades_total > 0
                  AND yes_won IS NOT NULL
            ) TO '{OUT_MARKETS}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT_MARKETS}')").fetchone()[0]
        n_yes = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT_MARKETS}') WHERE yes_won=1").fetchone()[0]
        print(f"  → {n:,} markets ({n_yes:,} YES won) | {time.time()-t0:.1f}s", flush=True)
        # Category breakdown for sanity
        print("  Category distribution:", flush=True)
        cats = con.execute(f"""
            SELECT category, COUNT(*) AS n, ROUND(AVG(yes_won)*100, 1) AS yes_pct
            FROM read_parquet('{OUT_MARKETS}')
            GROUP BY category ORDER BY n DESC
        """).fetchall()
        for cat, ncat, ypct in cats:
            print(f"    {cat:18s} n={ncat:>7,}  yes_won%={ypct}", flush=True)

    # ── STAGE 2: price_snapshots ────────────────────────────────────────────
    t0 = time.time()
    if OUT_SNAPSHOTS.exists():
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT_SNAPSHOTS}')").fetchone()[0]
        print(f"\n[2/3] price_snapshots.parquet exists, skip ({n:,} rows)", flush=True)
    else:
        print("\n[2/3] Building price_snapshots.parquet...", flush=True)
        snap_hours_sql = ", ".join(str(h) for h in SNAPSHOT_HOURS)
        con.execute(f"""
            COPY (
                WITH market_targets AS (
                    SELECT
                        m.condition_id,
                        m.end_date,
                        h AS hours_to_resolve,
                        EXTRACT(EPOCH FROM m.end_date)::BIGINT - h * 3600 AS cutoff_ts
                    FROM read_parquet('{OUT_MARKETS}') m
                    CROSS JOIN UNNEST([{snap_hours_sql}]) AS t(h)
                ),
                last_trades AS (
                    SELECT
                        mt.condition_id,
                        mt.hours_to_resolve,
                        q.price AS yes_price,
                        q.timestamp AS trade_ts,
                        ROW_NUMBER() OVER (
                            PARTITION BY mt.condition_id, mt.hours_to_resolve
                            ORDER BY q.timestamp DESC
                        ) AS rn
                    FROM market_targets mt
                    JOIN read_parquet('{QUANT_PATH}') q
                      ON q.condition_id = mt.condition_id
                     AND q.timestamp <= mt.cutoff_ts
                     AND q.timestamp >= {PERIOD_START_TS}
                )
                SELECT condition_id, hours_to_resolve, yes_price, trade_ts
                FROM last_trades
                WHERE rn = 1
            ) TO '{OUT_SNAPSHOTS}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT_SNAPSHOTS}')").fetchone()[0]
        print(f"  → {n:,} snapshots | {time.time()-t0:.1f}s", flush=True)

    # ── STAGE 3: trade_buckets (chunked by quarter/month to fit RAM) ─────────
    t0 = time.time()
    if OUT_BUCKETS.exists():
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT_BUCKETS}')").fetchone()[0]
        print(f"\n[3/3] trade_buckets.parquet exists, skip ({n:,} rows)", flush=True)
    else:
        print("\n[3/3] Building trade_buckets.parquet (hourly aggregates, chunked)...", flush=True)

        chunks = []
        # 2024: quarterly
        for q in [1, 2, 3, 4]:
            start_m = (q - 1) * 3 + 1
            end_m = start_m + 3
            end_y = 2024 if end_m <= 12 else 2025
            end_m_norm = end_m if end_m <= 12 else end_m - 12
            chunks.append((
                f"2024Q{q}",
                int(datetime(2024, start_m, 1).timestamp()),
                int(datetime(end_y, end_m_norm, 1).timestamp()),
            ))
        # 2025 Q1-Q3: quarterly
        for q in [1, 2, 3]:
            start_m = (q - 1) * 3 + 1
            end_m = start_m + 3
            chunks.append((
                f"2025Q{q}",
                int(datetime(2025, start_m, 1).timestamp()),
                int(datetime(2025, end_m, 1).timestamp()),
            ))
        # 2025 Q4 + 2026 Q1-Q2: monthly (highest volume period)
        for y, m in [(2025, 10), (2025, 11), (2025, 12),
                     (2026, 1), (2026, 2), (2026, 3), (2026, 4)]:
            nm = m + 1
            ny = y if nm <= 12 else y + 1
            nm_norm = nm if nm <= 12 else nm - 12
            chunks.append((
                f"{y}M{m:02d}",
                int(datetime(y, m, 1).timestamp()),
                int(datetime(ny, nm_norm, 1).timestamp()),
            ))

        chunk_dir = OUT_DIR / "_chunks"
        chunk_dir.mkdir(exist_ok=True)
        chunk_paths = []

        for label, start_ts, end_ts in chunks:
            cp = chunk_dir / f"buckets_{label}.parquet"
            chunk_paths.append(cp)
            if cp.exists() and cp.stat().st_size > 1000:
                n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{cp}')").fetchone()[0]
                print(f"  {label} exists ({n:,} buckets), skip", flush=True)
                continue
            if cp.exists():
                cp.unlink()
            print(f"  {label} ({datetime.fromtimestamp(start_ts).date()} → {datetime.fromtimestamp(end_ts).date()})...", flush=True)
            ct = time.time()
            con.execute(f"""
                COPY (
                    WITH eligible AS (
                        SELECT DISTINCT condition_id FROM read_parquet('{OUT_MARKETS}')
                    )
                    SELECT
                        q.condition_id,
                        DATE_TRUNC('hour', TO_TIMESTAMP(q.timestamp)) AS hour_bucket,
                        COUNT(*) AS n_trades,
                        SUM(q.usd_amount) AS total_volume_usd,
                        SUM(q.price * q.usd_amount) / NULLIF(SUM(q.usd_amount), 0) AS vwap_yes,
                        MIN(q.price) AS min_yes_price,
                        MAX(q.price) AS max_yes_price,
                        SUM(CASE WHEN q.side = 'BUY'  THEN q.usd_amount ELSE 0 END) AS buy_volume,
                        SUM(CASE WHEN q.side = 'SELL' THEN q.usd_amount ELSE 0 END) AS sell_volume,
                        COUNT(DISTINCT q.maker) AS n_distinct_makers,
                        COUNT(DISTINCT q.taker) AS n_distinct_takers
                    FROM read_parquet('{QUANT_PATH}') q
                    JOIN eligible m ON m.condition_id = q.condition_id
                    WHERE q.timestamp >= {start_ts} AND q.timestamp < {end_ts}
                    GROUP BY q.condition_id, DATE_TRUNC('hour', TO_TIMESTAMP(q.timestamp))
                ) TO '{cp}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """)
            n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{cp}')").fetchone()[0]
            print(f"    → {n:,} buckets ({time.time()-ct:.1f}s)", flush=True)

        print(f"  Concatenating {len(chunk_paths)} chunks...", flush=True)
        chunk_glob = str(chunk_dir / "buckets_*.parquet")
        con.execute(f"""
            COPY (SELECT * FROM read_parquet('{chunk_glob}'))
            TO '{OUT_BUCKETS}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)

        for cp in chunk_paths:
            cp.unlink()
        chunk_dir.rmdir()

        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT_BUCKETS}')").fetchone()[0]
        size_mb = OUT_BUCKETS.stat().st_size / 1024 / 1024
        print(f"  → {n:,} total hourly buckets ({size_mb:.1f} MB) | {time.time()-t0:.1f}s", flush=True)

    # ── Summary ──
    print("\n" + "=" * 60)
    print("CANONICAL DATASET BUILD COMPLETE")
    print("=" * 60)
    for path in [OUT_MARKETS, OUT_SNAPSHOTS, OUT_BUCKETS]:
        size_mb = path.stat().st_size / 1024 / 1024
        n_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{path}')").fetchone()[0]
        print(f"  {path.name}: {n_rows:>10,} rows, {size_mb:>7.1f} MB")
    print()


if __name__ == "__main__":
    main()
