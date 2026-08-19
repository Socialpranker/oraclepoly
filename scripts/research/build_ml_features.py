#!/usr/bin/env python3
"""Build ml_features.parquet — 5-й canonical файл (LEAK-FREE per-anchor design).

V2 (2026-05-02): Расширение с 35 → 55 колонок через Phases A+B+C.

КАЖДЫЙ ROW = (condition_id, anchor_hours) — точка принятия решения "за N часов до резолва".
Все features являются `as_of anchor_ts = end_date - anchor_hours`.
Никаких post-anchor данных (vwap_lifetime, price_max_lifetime, vol_last_*) — это leakage.

Anchor points: 720h, 168h, 72h, 24h, 4h до резолва.

V3 (2026-05-05): SQL вынесен в `app/ml/feature_kernel.py` (single source of truth
для canonical batch + live incremental builders). Этот скрипт стал тонкой обёрткой —
для retraining/audit на canonical parquet sources.

Использование (walk-forward):
    df = pd.read_parquet('/mnt/data/canonical/ml_features.parquet')
    train = df[df['anchor_hours'] == 24]
    train_tr = train[train['_resolve_dt'] < '2026-01-01']
    train_te = train[train['_resolve_dt'] >= '2026-01-01']

Источники (только canonical):
  - markets_clean.parquet   (target + metadata)
  - price_snapshots.parquet (yes_price на 4h/8h/.../720h до резолва)
  - trade_buckets.parquet   (hourly aggregates → as_of-N-hours features)
  - wallet_flow_per_market.parquet (pre-computed per-(market,hour) wallet flow)

Запуск на VPS:
    cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/build_ml_features.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Allow running как standalone script: добавляем repo root в sys.path для импорта app.ml
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import duckdb  # noqa: E402

from app.ml.feature_kernel import CANONICAL_ANCHORS, build_features_sql  # noqa: E402

OUT_PATH = "/mnt/data/canonical/ml_features.parquet"
MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"
BUCKETS = "/mnt/data/canonical/trade_buckets.parquet"
WFLOW = "/mnt/data/canonical/wallet_flow_per_market.parquet"


def build_for_anchor(con: duckdb.DuckDBPyConnection, anchor_h: int) -> None:
    """Build features for one anchor point. Output to temp parquet."""
    print(f"  anchor={anchor_h}h ...", end=" ", flush=True)
    t0 = time.time()

    out = f"/mnt/data/canonical/_anchor_{anchor_h}.parquet"
    sql = build_features_sql(
        markets_src=MARKETS,
        snaps_src=SNAPS,
        buckets_src=BUCKETS,
        wflow_src=WFLOW,
        anchor_h=anchor_h,
        market_filter="WHERE m.yes_won IS NOT NULL",
        is_duckdb=True,
    )
    con.execute(f"COPY ({sql}) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)")

    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0]
    print(f"{n:,} rows ({time.time()-t0:.1f}s)", flush=True)


def main():
    print("Build ml_features.parquet V2 (35 → 55 cols)...", flush=True)
    print(f"Anchors: {CANONICAL_ANCHORS} hours to resolve", flush=True)
    print("Phase A: wallet-level (8) | Phase B: microstructure (7) | Phase C: event-level (5)", flush=True)
    t_total = time.time()

    # Validate sources
    for f, label in [
        (MARKETS, "markets_clean"),
        (SNAPS, "price_snapshots"),
        (BUCKETS, "trade_buckets"),
        (WFLOW, "wallet_flow_per_market"),
    ]:
        if not Path(f).exists():
            print(f"ERROR: {label} not found at {f}", flush=True)
            sys.exit(1)

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute("SET threads=4")
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")
    con.execute("SET preserve_insertion_order=false")

    for h in CANONICAL_ANCHORS:
        build_for_anchor(con, h)

    print("\n  Concatenating anchors...", flush=True)
    chunk_glob = "/mnt/data/canonical/_anchor_*.parquet"
    con.execute(f"""
        COPY (SELECT * FROM read_parquet('{chunk_glob}'))
        TO '{OUT_PATH}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    for h in CANONICAL_ANCHORS:
        Path(f"/mnt/data/canonical/_anchor_{h}.parquet").unlink()

    n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{OUT_PATH}')").fetchone()[0]
    n_cols = len(con.execute(f"SELECT * FROM read_parquet('{OUT_PATH}') LIMIT 0").description)
    size_mb = Path(OUT_PATH).stat().st_size / 1024 / 1024
    print(f"\n  → {n:,} rows × {n_cols} cols ({size_mb:.1f} MB) | total {time.time()-t_total:.1f}s", flush=True)

    print("\n=== Rows per anchor ===")
    r = con.execute(f"""
        SELECT anchor_hours, COUNT(*) AS n,
               ROUND(AVG(yes_won::INT) * 100, 1) AS yes_won_pct
        FROM read_parquet('{OUT_PATH}')
        GROUP BY 1 ORDER BY 1
    """).df()
    print(r.to_string(index=False))


if __name__ == "__main__":
    sys.exit(main() or 0)
