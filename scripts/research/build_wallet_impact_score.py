#!/usr/bin/env python3
"""Build wallet_impact_score.parquet — per-wallet price impact measurement.

Алхимия: для каждого wallet (taker) считаем сколько он двигает цену
за каждый $1000 пушнутого USDC volume. High-impact wallets = informed traders
которые predicate price moves раньше остальных.

Methodology:
  Per (wallet, condition_id, minute):
    volume = sum(usd_amount)
    price_change = last_price - first_price (within minute)
    impact_per_1k_usd = price_change / (volume / 1000)

  Aggregate per wallet over all (market, minute):
    median_impact_per_1k = median |price_change / (volume/1000)|
    n_samples = count of (market, minute) buckets touched
    total_volume_taken = sum of all volumes
    realized_pnl = (pnl from wallet_performance JOIN)

Filter:
  - n_samples >= 20 (require meaningful sample)
  - total_volume_taken >= $10K (skip tiny wallets)

Output:
  /mnt/data/canonical/wallet_impact_score.parquet
  Columns: wallet, n_samples, total_volume_taken, median_impact_per_1k,
           p75_impact_per_1k, p90_impact_per_1k, realized_pnl, n_markets,
           is_top_decile_impact

Usage downstream:
  D25 Smart Money Lead detector: top 100 wallets by impact_per_1k →
  monitor their realtime entries → emit signal в direction в течение 30 мин.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb

USERS_PATH = "/mnt/data/hf_data2/alllong/users.parquet"
PERFORMANCE_PATH = "/mnt/data/canonical/wallet_performance.parquet"
OUT_PATH = Path("/mnt/data/canonical/wallet_impact_score.parquet")

MIN_SAMPLES = 20
MIN_VOLUME_USD = 10_000


def main() -> int:
    print("=" * 70, flush=True)
    print("Build wallet_impact_score.parquet — алхимическая трансформация", flush=True)
    print("=" * 70, flush=True)

    if not Path(USERS_PATH).exists():
        print(f"❌ users.parquet not found: {USERS_PATH}", flush=True)
        return 1

    con = duckdb.connect()
    con.execute("SET memory_limit='10GB'")
    con.execute("SET threads=4")
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("PRAGMA max_temp_directory_size='200GiB'")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n→ Source: {USERS_PATH} (340M user trade events)", flush=True)
    print(f"→ Output: {OUT_PATH}", flush=True)
    print(f"→ Filter: n_samples >= {MIN_SAMPLES}, total_volume >= ${MIN_VOLUME_USD:,}", flush=True)

    print("\n[1/2] Compute per-wallet impact metrics...", flush=True)
    t0 = time.time()

    # Stage 1: per (wallet, market, minute) bucket — compute impact
    # Note: используем role='taker' only (push'еры price). Maker absorbs, не push.
    sql = f"""
    COPY (
      WITH wallet_minute_bucket AS (
        SELECT
          user AS wallet,
          condition_id,
          DATE_TRUNC('minute', TO_TIMESTAMP(timestamp)) AS minute_bucket,
          SUM(usd_amount) AS volume_pushed,
          MAX(price) - MIN(price) AS price_range,
          (LAST(price ORDER BY timestamp) - FIRST(price ORDER BY timestamp)) AS price_drift,
          COUNT(*) AS n_trades_in_minute
        FROM read_parquet('{USERS_PATH}')
        WHERE role = 'taker'
          AND usd_amount > 0
          AND price BETWEEN 0.01 AND 0.99
        GROUP BY user, condition_id, DATE_TRUNC('minute', TO_TIMESTAMP(timestamp))
        HAVING SUM(usd_amount) >= 100  -- $100+ pushes only (filter noise)
      ),
      -- Per-wallet aggregates
      wallet_agg AS (
        SELECT
          wallet,
          COUNT(*) AS n_samples,
          SUM(volume_pushed) AS total_volume_taken,
          COUNT(DISTINCT condition_id) AS n_markets,
          -- impact normalized per $1000: price_drift / (volume/1000)
          -- Используем ABS(price_drift) — direction agnostic, важна сила движения
          MEDIAN(ABS(price_drift) / (volume_pushed / 1000.0)) AS median_impact_per_1k,
          QUANTILE_CONT(ABS(price_drift) / (volume_pushed / 1000.0), 0.75) AS p75_impact_per_1k,
          QUANTILE_CONT(ABS(price_drift) / (volume_pushed / 1000.0), 0.90) AS p90_impact_per_1k,
          -- Sign-aware: средний directional impact (положит = wallet двигает up)
          AVG(price_drift / (volume_pushed / 1000.0)) AS mean_directional_impact_per_1k
        FROM wallet_minute_bucket
        GROUP BY wallet
      )
      SELECT
        wa.wallet,
        wa.n_samples,
        wa.n_markets,
        ROUND(wa.total_volume_taken::numeric, 2) AS total_volume_taken,
        ROUND(wa.median_impact_per_1k::numeric, 6) AS median_impact_per_1k,
        ROUND(wa.p75_impact_per_1k::numeric, 6) AS p75_impact_per_1k,
        ROUND(wa.p90_impact_per_1k::numeric, 6) AS p90_impact_per_1k,
        ROUND(wa.mean_directional_impact_per_1k::numeric, 6) AS mean_directional_impact_per_1k,
        -- JOIN performance для context (realized PnL/ROI)
        COALESCE(wp.roi_pct, 0.0) AS roi_pct_lifetime,
        COALESCE(wp.realized_pnl_usd, 0.0) AS realized_pnl_usd,
        COALESCE(wp.is_smart_money, FALSE) AS is_smart_money_perf
      FROM wallet_agg wa
      LEFT JOIN read_parquet('{PERFORMANCE_PATH}') wp ON wp.wallet = wa.wallet
      WHERE wa.n_samples >= {MIN_SAMPLES}
        AND wa.total_volume_taken >= {MIN_VOLUME_USD}
    ) TO '{OUT_PATH}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """

    con.execute(sql)
    elapsed = time.time() - t0
    print(f"  done in {elapsed:.1f}s", flush=True)

    # Stage 2: compute top-decile flag and report stats
    print("\n[2/2] Compute top-decile threshold + sanity check...", flush=True)
    threshold = con.execute(f"""
        SELECT QUANTILE_CONT(median_impact_per_1k, 0.9)
        FROM read_parquet('{OUT_PATH}')
    """).fetchone()[0]
    print(f"  Top decile median_impact_per_1k threshold: {threshold:.6f}", flush=True)

    n_total = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT_PATH}')").fetchone()[0]
    print(f"  Total qualified wallets: {n_total:,}", flush=True)

    # Re-write with is_top_decile_impact flag
    con.execute(f"""
        COPY (
          SELECT *,
                 (median_impact_per_1k >= {threshold}) AS is_top_decile_impact
          FROM read_parquet('{OUT_PATH}')
        ) TO '{OUT_PATH}.tmp' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    OUT_PATH.unlink()
    Path(f"{OUT_PATH}.tmp").rename(OUT_PATH)

    # Final stats
    print("\n=== TOP 20 HIGH-IMPACT WALLETS ===", flush=True)
    rows = con.execute(f"""
        SELECT wallet, n_samples, ROUND(total_volume_taken::numeric, 0) AS vol_usd,
               ROUND(median_impact_per_1k::numeric, 5) AS med_imp,
               ROUND(p90_impact_per_1k::numeric, 5) AS p90_imp,
               ROUND(roi_pct_lifetime::numeric, 1) AS roi_pct,
               is_smart_money_perf
        FROM read_parquet('{OUT_PATH}')
        WHERE is_top_decile_impact = true
        ORDER BY median_impact_per_1k DESC
        LIMIT 20
    """).fetchall()
    print(f"{'wallet':40s} {'samples':>8s} {'volume':>12s} {'med_imp':>10s} {'p90':>10s} {'roi%':>8s} {'smart'}", flush=True)
    for r in rows:
        wallet, samples, vol, med, p90, roi, smart = r
        print(f"{wallet:40s} {samples:>8d} ${float(vol):>10,.0f} {float(med):>10.5f} {float(p90):>10.5f} {float(roi):>7.1f}% {smart}", flush=True)

    # Distribution check
    print("\n=== IMPACT DISTRIBUTION ===", flush=True)
    pcts = con.execute(f"""
        SELECT
          ROUND(QUANTILE_CONT(median_impact_per_1k, 0.50)::numeric, 5) AS p50,
          ROUND(QUANTILE_CONT(median_impact_per_1k, 0.75)::numeric, 5) AS p75,
          ROUND(QUANTILE_CONT(median_impact_per_1k, 0.90)::numeric, 5) AS p90,
          ROUND(QUANTILE_CONT(median_impact_per_1k, 0.95)::numeric, 5) AS p95,
          ROUND(QUANTILE_CONT(median_impact_per_1k, 0.99)::numeric, 5) AS p99,
          ROUND(MAX(median_impact_per_1k)::numeric, 5) AS max
        FROM read_parquet('{OUT_PATH}')
    """).fetchone()
    print(f"  median (p50):  {pcts[0]}", flush=True)
    print(f"  p75:           {pcts[1]}", flush=True)
    print(f"  p90 (top-decile threshold): {pcts[2]}", flush=True)
    print(f"  p95:           {pcts[3]}", flush=True)
    print(f"  p99:           {pcts[4]}", flush=True)
    print(f"  max:           {pcts[5]}", flush=True)

    # Correlation impact vs roi
    print("\n=== IMPACT vs ROI CORRELATION ===", flush=True)
    corr = con.execute(f"""
        SELECT
          ROUND(CORR(median_impact_per_1k, roi_pct_lifetime)::numeric, 3) AS corr_imp_roi,
          ROUND(AVG(roi_pct_lifetime) FILTER (WHERE is_top_decile_impact)::numeric, 1) AS top_decile_avg_roi,
          ROUND(AVG(roi_pct_lifetime) FILTER (WHERE NOT is_top_decile_impact)::numeric, 1) AS non_top_avg_roi
        FROM read_parquet('{OUT_PATH}')
    """).fetchone()
    print(f"  CORR(impact, ROI): {corr[0]}", flush=True)
    print(f"  Top decile avg ROI:     {corr[1]}%", flush=True)
    print(f"  Non-top avg ROI:        {corr[2]}%", flush=True)
    print()

    size_mb = OUT_PATH.stat().st_size / 1024**2
    print(f"✅ Output: {OUT_PATH} ({size_mb:.1f} MB)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
