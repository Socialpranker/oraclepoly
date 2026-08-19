#!/usr/bin/env python3
"""
Export oracle_snapshots from PostgreSQL → parquet for ablation.

Запуск на VPS:
    python3 scripts/export_snapshots.py

Артефакт: /mnt/data/maker_flow/oracle_snapshots.parquet
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("export_snapshots")

OUTPUT = Path("/mnt/data/maker_flow/oracle_snapshots.parquet")

# SQL для экспорта: resolved snapshots с category из markets
EXPORT_SQL = """
COPY (
    SELECT
        os.condition_id,
        os.market_id::text,
        os.oracle_score,
        EXTRACT(EPOCH FROM os.created_at)::bigint AS created_at,
        os.outcome,
        COALESCE(m.category, 'Unknown') AS category
    FROM oracle_snapshots os
    LEFT JOIN markets m ON m.condition_id = os.condition_id
    WHERE os.outcome IS NOT NULL
      AND os.oracle_score IS NOT NULL
      AND os.oracle_score > 0
    ORDER BY os.created_at
) TO STDOUT WITH (FORMAT CSV, HEADER true)
"""


def main():
    out = OUTPUT
    out.parent.mkdir(parents=True, exist_ok=True)

    csv_tmp = out.with_suffix(".csv")

    log.info("Exporting oracle_snapshots from PostgreSQL...")
    cmd = [
        "docker", "exec", "-i", "oraclepoly-postgres",
        "psql", "-U", "oraclepoly", "-d", "oraclepoly",
        "-c", EXPORT_SQL,
    ]

    with open(csv_tmp, "w") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        log.error(f"psql failed: {result.stderr}")
        raise SystemExit(1)

    size = csv_tmp.stat().st_size
    log.info(f"CSV exported: {csv_tmp} ({size / 1e6:.1f} MB)")

    # CSV → parquet через DuckDB
    log.info("Converting CSV → parquet...")
    import duckdb
    db = duckdb.connect(":memory:")
    db.execute(f"""
        COPY (
            SELECT * FROM read_csv_auto('{csv_tmp}', sample_size=10000)
        ) TO '{out}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    n = db.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0]
    parquet_size = out.stat().st_size
    log.info(f"Parquet written: {out} ({parquet_size / 1e6:.1f} MB, {n:,} rows)")

    # Stats
    cats = db.execute(f"""
        SELECT category, COUNT(*) as n, AVG(oracle_score) as avg_score
        FROM read_parquet('{out}')
        GROUP BY category ORDER BY n DESC
    """).fetchall()
    log.info("Category breakdown:")
    for row in cats:
        log.info(f"  {row[0]:15s}: {row[1]:7,} rows  avg_score={row[2]:.3f}")

    db.close()
    csv_tmp.unlink(missing_ok=True)
    log.info(f"Done: {out}")


if __name__ == "__main__":
    main()
