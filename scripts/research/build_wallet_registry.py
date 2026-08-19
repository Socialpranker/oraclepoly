#!/usr/bin/env python3
"""Build wallet_registry.parquet — per-wallet lifetime stats для whale/smart_money classification.

V2 (memory-optimized): убраны тяжёлые window functions и MEDIAN.
Используем 2-pass: (1) compute aggregates, (2) compute cutoffs, (3) label classes.

Источник: /mnt/data/hf_data/quant.parquet (36GB, 568M trades).

Output (/mnt/data/canonical/wallet_registry.parquet):
  Один row per wallet (~2.4M wallets).

Schema (упрощённая):
  wallet                  VARCHAR
  lifetime_volume_usd     DOUBLE   — taker volume
  n_trades_taker          BIGINT
  n_trades_maker          BIGINT
  buy_volume_usd          DOUBLE
  sell_volume_usd         DOUBLE
  buy_imbalance           DOUBLE
  first_trade_ts          BIGINT
  last_trade_ts           BIGINT
  avg_trade_size_usd      DOUBLE
  max_trade_size_usd      DOUBLE
  whale_class             VARCHAR  — 'whale_top1pct' | 'whale_top5pct' | 'mid' | 'retail'

Запуск:
    cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/build_wallet_registry.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb

QUANT_PATH = "/mnt/data/hf_data/quant.parquet"
OUT_PATH = "/mnt/data/canonical/wallet_registry.parquet"
TMP_TAKER = "/mnt/data/canonical/_wallet_taker_tmp.parquet"
TMP_MAKER = "/mnt/data/canonical/_wallet_maker_tmp.parquet"

PERIOD_START_TS = 1704067200  # 2024-01-01


def main() -> None:
    if not Path(QUANT_PATH).exists():
        print(f"ERROR: {QUANT_PATH} not found.")
        sys.exit(1)

    print(f"Build wallet_registry.parquet → {OUT_PATH}", flush=True)

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=1")
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("PRAGMA max_temp_directory_size='30GiB'")

    # ─── PASS 1: Aggregate per taker (без MEDIAN, без window) ───
    print("[1/3] Aggregate per taker...", flush=True)
    t0 = time.time()
    con.execute(f"""
        COPY (
            SELECT
                taker AS wallet,
                SUM(usd_amount) AS lifetime_volume_usd,
                COUNT(*) AS n_trades_taker,
                SUM(CASE WHEN side = 'BUY'  THEN usd_amount ELSE 0 END) AS buy_volume_usd,
                SUM(CASE WHEN side = 'SELL' THEN usd_amount ELSE 0 END) AS sell_volume_usd,
                MIN(timestamp) AS first_trade_ts,
                MAX(timestamp) AS last_trade_ts,
                AVG(usd_amount) AS avg_trade_size_usd,
                MAX(usd_amount) AS max_trade_size_usd
            FROM read_parquet('{QUANT_PATH}')
            WHERE timestamp >= {PERIOD_START_TS}
              AND taker IS NOT NULL
            GROUP BY taker
        ) TO '{TMP_TAKER}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    print(f"  taker agg done ({time.time()-t0:.1f}s)", flush=True)

    # ─── PASS 2: Aggregate per maker count only ───
    print("[2/3] Aggregate per maker count...", flush=True)
    t0 = time.time()
    con.execute(f"""
        COPY (
            SELECT maker AS wallet, COUNT(*) AS n_trades_maker
            FROM read_parquet('{QUANT_PATH}')
            WHERE timestamp >= {PERIOD_START_TS}
              AND maker IS NOT NULL
            GROUP BY maker
        ) TO '{TMP_MAKER}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    print(f"  maker agg done ({time.time()-t0:.1f}s)", flush=True)

    # ─── PASS 3: Compute cutoffs and label classes ───
    print("[3/3] Compute cutoffs and label whale classes...", flush=True)
    t0 = time.time()
    cutoffs = con.execute(f"""
        SELECT
            quantile_cont(lifetime_volume_usd, 0.99) AS cutoff_top1,
            quantile_cont(lifetime_volume_usd, 0.95) AS cutoff_top5,
            quantile_cont(lifetime_volume_usd, 0.50) AS cutoff_mid
        FROM read_parquet('{TMP_TAKER}')
    """).fetchone()
    cut_top1, cut_top5, cut_mid = cutoffs
    print(f"  cutoffs: top1=${cut_top1:.0f}, top5=${cut_top5:.0f}, mid=${cut_mid:.0f}", flush=True)

    # ─── PASS 4: Final join + label ───
    con.execute(f"""
        COPY (
            SELECT
                t.wallet,
                t.lifetime_volume_usd,
                t.n_trades_taker,
                COALESCE(mc.n_trades_maker, 0) AS n_trades_maker,
                t.buy_volume_usd,
                t.sell_volume_usd,
                (t.buy_volume_usd - t.sell_volume_usd) /
                    NULLIF(t.buy_volume_usd + t.sell_volume_usd, 0) AS buy_imbalance,
                t.first_trade_ts,
                t.last_trade_ts,
                t.avg_trade_size_usd,
                t.max_trade_size_usd,
                CASE
                    WHEN t.lifetime_volume_usd >= {cut_top1} THEN 'whale_top1pct'
                    WHEN t.lifetime_volume_usd >= {cut_top5} THEN 'whale_top5pct'
                    WHEN t.lifetime_volume_usd >= {cut_mid}  THEN 'mid'
                    ELSE 'retail'
                END AS whale_class
            FROM read_parquet('{TMP_TAKER}') t
            LEFT JOIN read_parquet('{TMP_MAKER}') mc ON mc.wallet = t.wallet
            WHERE t.lifetime_volume_usd > 0
        ) TO '{OUT_PATH}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    print(f"  final join done ({time.time()-t0:.1f}s)", flush=True)

    # cleanup tmp
    Path(TMP_TAKER).unlink(missing_ok=True)
    Path(TMP_MAKER).unlink(missing_ok=True)

    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT_PATH}')").fetchone()[0]
    size_mb = Path(OUT_PATH).stat().st_size / 1024 / 1024
    print(f"\n  → {n:,} wallets ({size_mb:.1f} MB)", flush=True)

    print("\n--- Class distribution ---")
    df = con.execute(f"""
        SELECT
            whale_class,
            COUNT(*) AS n_wallets,
            SUM(lifetime_volume_usd) AS total_vol,
            AVG(lifetime_volume_usd) AS avg_vol,
            MIN(lifetime_volume_usd) AS min_vol,
            MAX(lifetime_volume_usd) AS max_vol
        FROM read_parquet('{OUT_PATH}')
        GROUP BY whale_class
        ORDER BY MIN(lifetime_volume_usd) DESC
    """).fetchall()
    for cls, nw, tot, avg, mn, mx in df:
        print(f"  {cls:<18} n={nw:>9,}  total=${tot:>16,.0f}  avg=${avg:>10,.0f}  range=[${mn:.0f}..${mx:,.0f}]")


if __name__ == "__main__":
    main()
