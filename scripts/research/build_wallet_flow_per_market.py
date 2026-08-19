#!/usr/bin/env python3
"""Build wallet_flow_per_market.parquet — pre-computed wallet flow per (market, hour_bucket).

V2 (memory-optimized): chunked by quarter, потом concat — как в build_canonical_hf.py.

Зачем: aggregating quant.parquet (568M rows) inline в build_ml_features = OOM.
Pre-compute один раз per quarter, потом JOIN.

Output structure (one row per market_hour):
  condition_id              VARCHAR
  hour_bucket               TIMESTAMP
  n_unique_takers           BIGINT
  n_unique_makers           BIGINT
  taker_count_total         BIGINT
  taker_volume_total        DOUBLE
  whale_taker_volume        DOUBLE  -- whales (top 5%) only
  whale_taker_count         BIGINT
  retail_taker_avg_size     DOUBLE
  whale_buy_pressure        DOUBLE  -- net flow whales (BUY - SELL)

Размер: ~13M rows × 10 cols ≈ 200MB.

Запуск:
    cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/build_wallet_flow_per_market.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb

QUANT_PATH = "/mnt/data/hf_data/quant.parquet"
WALLETS_PATH = "/mnt/data/canonical/wallet_registry.parquet"
MARKETS_PATH = "/mnt/data/canonical/markets_clean.parquet"
OUT_PATH = "/mnt/data/canonical/wallet_flow_per_market.parquet"

PERIOD_START_TS = 1704067200  # 2024-01-01


def main() -> None:
    for f, label in [(QUANT_PATH, "quant"), (WALLETS_PATH, "wallet_registry"),
                      (MARKETS_PATH, "markets_clean")]:
        if not Path(f).exists():
            print(f"ERROR: {label} not found at {f}")
            sys.exit(1)

    print(f"Build wallet_flow_per_market.parquet → {OUT_PATH}", flush=True)

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=1")
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("PRAGMA max_temp_directory_size='30GiB'")

    # Build chunks per quarter (как в build_canonical_hf.py для buckets)
    quarters = []
    for year in [2024]:
        for q in [1, 2, 3, 4]:
            sm = (q - 1) * 3 + 1
            em = sm + 3
            ey = year if em <= 12 else year + 1
            em_n = em if em <= 12 else em - 12
            quarters.append((f"{year}Q{q}",
                             int(datetime(year, sm, 1).timestamp()),
                             int(datetime(ey, em_n, 1).timestamp())))
    for q in [1, 2, 3]:
        sm = (q - 1) * 3 + 1
        em = sm + 3
        quarters.append((f"2025Q{q}",
                         int(datetime(2025, sm, 1).timestamp()),
                         int(datetime(2025, em, 1).timestamp())))
    # 2025Q4 + 2026Q1: monthly chunks (huge volume)
    for year, month in [(2025, 10), (2025, 11), (2025, 12),
                        (2026, 1), (2026, 2), (2026, 3)]:
        nm = month + 1
        ny = year if nm <= 12 else year + 1
        nm_n = nm if nm <= 12 else nm - 12
        quarters.append((f"{year}M{month:02d}",
                         int(datetime(year, month, 1).timestamp()),
                         int(datetime(ny, nm_n, 1).timestamp())))

    chunk_dir = Path("/mnt/data/canonical/_wflow_chunks")
    chunk_dir.mkdir(exist_ok=True)
    chunk_paths = []

    for label, start_ts, end_ts in quarters:
        cp = chunk_dir / f"wflow_{label}.parquet"
        chunk_paths.append(cp)
        if cp.exists() and cp.stat().st_size > 1000:
            n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{cp}')").fetchone()[0]
            print(f"  {label} exists ({n:,} rows), skip", flush=True)
            continue
        if cp.exists():
            cp.unlink()  # zero-byte from prev OOM

        print(f"  {label} ({datetime.fromtimestamp(start_ts).date()} → {datetime.fromtimestamp(end_ts).date()})...", flush=True)
        ct = time.time()
        con.execute(f"""
            COPY (
                WITH eligible AS (
                    SELECT DISTINCT condition_id FROM read_parquet('{MARKETS_PATH}')
                ),
                joined AS (
                    SELECT
                        q.condition_id,
                        DATE_TRUNC('hour', TO_TIMESTAMP(q.timestamp)) AS hour_bucket,
                        q.taker,
                        q.maker,
                        q.usd_amount,
                        q.side,
                        COALESCE(w.whale_class, 'unknown') AS whale_class
                    FROM read_parquet('{QUANT_PATH}') q
                    JOIN eligible em ON em.condition_id = q.condition_id
                    LEFT JOIN read_parquet('{WALLETS_PATH}') w ON w.wallet = q.taker
                    WHERE q.timestamp >= {start_ts}
                      AND q.timestamp <  {end_ts}
                )
                SELECT
                    condition_id,
                    hour_bucket,
                    COUNT(DISTINCT taker)                                       AS n_unique_takers,
                    COUNT(DISTINCT maker)                                       AS n_unique_makers,
                    COUNT(*)                                                    AS taker_count_total,
                    SUM(usd_amount)                                             AS taker_volume_total,
                    SUM(CASE WHEN whale_class IN ('whale_top1pct','whale_top5pct') THEN usd_amount ELSE 0 END) AS whale_taker_volume,
                    COUNT(DISTINCT CASE WHEN whale_class IN ('whale_top1pct','whale_top5pct') THEN taker END) AS whale_taker_count,
                    AVG(CASE WHEN whale_class = 'retail' THEN usd_amount END)   AS retail_taker_avg_size,
                    SUM(CASE WHEN whale_class IN ('whale_top1pct','whale_top5pct') AND side = 'BUY' THEN usd_amount
                             WHEN whale_class IN ('whale_top1pct','whale_top5pct') AND side = 'SELL' THEN -usd_amount
                             ELSE 0 END)                                        AS whale_buy_pressure
                FROM joined
                GROUP BY condition_id, hour_bucket
            ) TO '{cp}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{cp}')").fetchone()[0]
        print(f"    → {n:,} rows ({time.time()-ct:.1f}s)", flush=True)

    # Concat all chunks → final
    print(f"\n  Concatenating {len(chunk_paths)} chunks...", flush=True)
    chunk_glob = str(chunk_dir / "wflow_*.parquet")
    con.execute(f"""
        COPY (SELECT * FROM read_parquet('{chunk_glob}'))
        TO '{OUT_PATH}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    for cp in chunk_paths:
        cp.unlink(missing_ok=True)
    chunk_dir.rmdir()

    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT_PATH}')").fetchone()[0]
    n_markets = con.execute(f"SELECT COUNT(DISTINCT condition_id) FROM read_parquet('{OUT_PATH}')").fetchone()[0]
    size_mb = Path(OUT_PATH).stat().st_size / 1024 / 1024
    print(f"\n  → {n:,} rows ({n_markets:,} unique markets) | {size_mb:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
