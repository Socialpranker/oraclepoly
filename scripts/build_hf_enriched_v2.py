#!/usr/bin/env python3
"""Build full_hf_enriched_v2.parquet — adds oracle_history features.

Phase B.3.3 (2026-04-30): расширяет full_hf_enriched.parquet данными из
oracle_history.parquet — z_posterior, divergence из самых свежих snapshots.

Логика JOIN:
  Для каждого row в full_hf_enriched (NO-trade в момент created_at)
  → найти ближайший oracle_snapshot ДО created_at (latest snapshot_at <= trade_at)
  → взять его z_posterior + divergence + oracle_score
  → добавить в enriched

Output: /mnt/data/polymarket_dataset/full_hf_enriched_v2.parquet
  Все колонки full_hf_enriched + oracle_score, oracle_z_score, oracle_divergence

Usage:
  python3 scripts/build_hf_enriched_v2.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb

# Container paths (volumes via /app/research/output for rw)
ENRICHED = "/mnt/data/polymarket_dataset/full_hf_enriched.parquet"
ORACLE_HIST = "/app/research/output/oracle_history.parquet"
OUTPUT = "/app/research/output/full_hf_enriched_v2.parquet"


def main() -> int:
    if not Path(ENRICHED).exists():
        print(f"ERROR: {ENRICHED} not found", file=sys.stderr)
        return 1
    if not Path(ORACLE_HIST).exists():
        print(f"ERROR: {ORACLE_HIST} not found — run extract_oracle_history.py first", file=sys.stderr)
        return 1

    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'")
    con.execute("SET threads=4")
    con.execute("SET temp_directory='/tmp/duckdb'")

    print(f"Building {OUTPUT}...")
    t0 = time.time()

    # JOIN: per-market AVG z_posterior + AVG divergence (representative
    # за всё время жизни рынка). Это per-market features, как и enriched.
    #
    # Note: HF.created_at = market creation time, не trade time. oracle snapshots
    # пишутся в течение жизни рынка → нет matching по time. Используем
    # per-market aggregates.
    con.execute(
        f"""
        COPY (
            WITH oracle_per_market AS (
                SELECT
                    market_id,
                    AVG(oracle_score) AS oracle_score_avg,
                    AVG(z_posterior) AS oracle_z_score_avg,
                    AVG(divergence) AS oracle_divergence_avg,
                    MIN(z_posterior) AS oracle_z_score_min,
                    MAX(z_posterior) AS oracle_z_score_max,
                    COUNT(*) AS oracle_snapshot_count
                FROM read_parquet('{ORACLE_HIST}')
                GROUP BY market_id
            )
            SELECT
                e.*,
                od.oracle_score_avg,
                od.oracle_z_score_avg,
                od.oracle_divergence_avg,
                od.oracle_z_score_min,
                od.oracle_z_score_max,
                od.oracle_snapshot_count
            FROM read_parquet('{ENRICHED}') e
            LEFT JOIN oracle_per_market od ON e.market_id = od.market_id
        ) TO '{OUTPUT}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )

    elapsed = time.time() - t0
    n_total = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUTPUT}')").fetchone()[0]
    n_with_oracle = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{OUTPUT}') WHERE oracle_z_score_avg IS NOT NULL"
    ).fetchone()[0]
    pct = 100 * n_with_oracle / max(n_total, 1)

    print(f"\nDONE in {elapsed:.1f}s")
    print(f"  output: {OUTPUT}")
    print(f"  total rows: {n_total:,}")
    print(f"  with oracle features: {n_with_oracle:,} ({pct:.1f}%)")

    # Quick test: check D7 hypothesis on enriched_v2 (using AVG features)
    print("\n=== Quick check: D7 hypothesis (NO + avg_z<-0.5 + avg_div 8-15% + Sports/Crypto) ===")
    print(con.execute(
        f"""
        SELECT
          category,
          COUNT(*) as n,
          ROUND(AVG(CAST(win=1 AS DOUBLE))::numeric, 3) as wr,
          ROUND(AVG(oracle_z_score_avg)::numeric, 2) as avg_z,
          ROUND(AVG(oracle_divergence_avg)::numeric, 3) as avg_div
        FROM read_parquet('{OUTPUT}')
        WHERE side = 'NO'
          AND oracle_z_score_avg < -0.5
          AND oracle_divergence_avg BETWEEN 0.08 AND 0.15
          AND whale_tx_count = 0
          AND category IN ('Sports', 'Crypto', 'Politics')
          AND horizon_days >= 3 AND horizon_days < 14
        GROUP BY category
        HAVING COUNT(*) >= 30
        ORDER BY wr DESC
        """
    ).fetchdf().to_string())

    return 0


if __name__ == "__main__":
    sys.exit(main())
