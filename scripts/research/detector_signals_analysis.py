"""Detector Signals Performance Analysis — Variant B.

Анализирует реальные сигналы из таблицы detector_signals (прод БД):
- realized_roi / realized_roi_net (уже посчитаны при resolve)
- confidence (L1 calibrated p)
- current_price (YES-entry), entry_no = 1 - current_price

Секции:
  1. Общая сводка по детекторам (hf_backfill vs live_v2)
  2. По ценовому диапазону (10¢-бакеты entry_no)
  3. По горизонту (horizon_sec → дни)
  4. Calibration check: avg confidence vs realized WR
  5. Топ-15 комбо: detector × price_bucket × horizon_band
  6. Pre/post V2 split (2026-04-28)

Запуск: cd ~/oracle && python scripts/research/detector_signals_analysis.py
"""

import csv
import io
import os
import subprocess
import sys
import time
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set", file=sys.stderr)
    sys.exit(1)

MIN_N = 30
V2_CUTOVER = "2026-04-28"

DETECTORS = [
    "D1_fade_whale",
    "D3_cross_mkt_arb",
    "D4_small_market_fade",
    "D5_long_horizon_extreme_fade",
    "D6_retail_bullish_overshoot",
]


def hdr(title: str) -> None:
    print(f"\n{'='*72}")
    print(f"  {title}")
    print("=" * 72, flush=True)


def fmt(rows: list, cols: list) -> None:
    if not rows:
        print("  (нет данных)")
        return
    col_w = [max(len(str(c)), max((len(str(r[i])) for r in rows), default=0)) for i, c in enumerate(cols)]
    header = "  " + "  ".join(str(c).ljust(col_w[i]) for i, c in enumerate(cols))
    print(header)
    print("  " + "-" * (sum(col_w) + 2 * len(cols)))
    for row in rows:
        print("  " + "  ".join(str(v).ljust(col_w[i]) for i, v in enumerate(row)))


def run_sql(sql: str) -> list[tuple]:
    """Execute SQL via docker exec psql, return list of tuples."""
    full_sql = f"COPY ({sql}) TO STDOUT WITH CSV HEADER"
    cmd = [
        "docker", "exec", "oraclepoly-postgres",
        "psql", "-U", "oraclepoly", "-d", "oraclepoly",
        "-c", full_sql,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"SQL error:\n{result.stderr}", file=sys.stderr)
        return []
    reader = csv.reader(io.StringIO(result.stdout))
    rows = list(reader)
    if not rows:
        return []
    # skip header row, return data rows as tuples
    return [tuple(r) for r in rows[1:]]


def main() -> None:
    t0 = time.time()
    print("Running detector_signals analysis via psql ...")

    # Basic stats
    stat = run_sql("SELECT COUNT(*), COUNT(*) FILTER (WHERE outcome IS NOT NULL) FROM detector_signals")
    if stat:
        print(f"detector_signals: {stat[0][0]} total, {stat[0][1]} resolved")

    # ── 1. Общая сводка ───────────────────────────────────────────────────
    hdr("1. ОБЩАЯ СВОДКА (detector × source)")

    rows = run_sql(f"""
        SELECT
            detector,
            source,
            COUNT(*) AS n,
            COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) AS n_resolved,
            ROUND(100.0 * COUNT(*) FILTER (WHERE outcome='WIN')
                  / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')), 0), 1) AS wr_pct,
            ROUND(AVG(realized_roi) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS roi_maker,
            ROUND(AVG(realized_roi_net) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS roi_net
        FROM detector_signals
        GROUP BY detector, source
        HAVING COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) >= {MIN_N}
        ORDER BY detector, source
    """)
    fmt(rows, ["detector", "source", "n_total", "n_resolved", "wr%", "roi_maker%", "roi_net%"])

    # ── 2. По ценовому диапазону (entry_no бакеты) ────────────────────────
    hdr("2. ПО ЦЕНОВОМУ ДИАПАЗОНУ (entry_no = 1 - current_price)")

    for det in DETECTORS:
        rows = run_sql(f"""
            SELECT
                CONCAT(
                    ROUND(FLOOR((1-current_price)*10)/10*100)::int, '-',
                    ROUND(FLOOR((1-current_price)*10)/10*100 + 10)::int, chr(162)
                ) AS range,
                COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) AS n,
                ROUND(100.0 * COUNT(*) FILTER (WHERE outcome='WIN')
                      / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')), 0), 1) AS wr_pct,
                ROUND(AVG(realized_roi) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS roi_maker,
                ROUND(AVG(realized_roi_net) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS roi_net,
                FLOOR((1-current_price)*10)/10 AS bucket_lo
            FROM detector_signals
            WHERE detector = '{det}'
            GROUP BY bucket_lo
            HAVING COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) >= {MIN_N}
            ORDER BY bucket_lo
        """)
        if not rows:
            continue
        # drop last col (bucket_lo, used only for ORDER BY)
        data = [r[:-1] for r in rows]
        print(f"\n  {det}")
        fmt(data, ["range", "n", "wr%", "roi_maker%", "roi_net%"])

    # ── 3. По горизонту ───────────────────────────────────────────────────
    hdr("3. ПО ГОРИЗОНТУ (horizon_sec / 86400)")

    for det in DETECTORS:
        rows = run_sql(f"""
            SELECT
                CASE
                    WHEN horizon_sec / 86400.0 <= 3   THEN '0-3d'
                    WHEN horizon_sec / 86400.0 <= 7   THEN '3-7d'
                    WHEN horizon_sec / 86400.0 <= 14  THEN '7-14d'
                    WHEN horizon_sec / 86400.0 <= 30  THEN '14-30d'
                    ELSE '30d+'
                END AS horizon_band,
                COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) AS n,
                ROUND(100.0 * COUNT(*) FILTER (WHERE outcome='WIN')
                      / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')), 0), 1) AS wr_pct,
                ROUND(AVG(realized_roi) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS roi_maker,
                ROUND(AVG(realized_roi_net) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS roi_net,
                CASE
                    WHEN horizon_sec / 86400.0 <= 3   THEN 1
                    WHEN horizon_sec / 86400.0 <= 7   THEN 2
                    WHEN horizon_sec / 86400.0 <= 14  THEN 3
                    WHEN horizon_sec / 86400.0 <= 30  THEN 4
                    ELSE 5
                END AS band_ord
            FROM detector_signals
            WHERE detector = '{det}'
            GROUP BY 1, band_ord
            HAVING COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) >= {MIN_N}
            ORDER BY band_ord
        """)
        if not rows:
            continue
        data = [r[:-1] for r in rows]  # drop band_ord
        print(f"\n  {det}")
        fmt(data, ["horizon", "n", "wr%", "roi_maker%", "roi_net%"])

    # ── 4. Calibration check ──────────────────────────────────────────────
    hdr("4. CALIBRATION CHECK (avg confidence vs realized WR)")
    print("  (если avg_conf% ≈ wr% → L1 калибровка хорошая)")

    rows = run_sql(f"""
        SELECT
            detector,
            ROUND(FLOOR(confidence * 10) / 10, 1) AS conf_bucket,
            COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) AS n,
            ROUND(100.0 * AVG(confidence) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS avg_conf_pct,
            ROUND(100.0 * COUNT(*) FILTER (WHERE outcome='WIN')
                  / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')), 0), 1) AS wr_pct
        FROM detector_signals
        WHERE confidence IS NOT NULL
        GROUP BY detector, conf_bucket
        HAVING COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) >= {MIN_N}
        ORDER BY detector, conf_bucket
    """)
    fmt(rows, ["detector", "conf_bucket", "n", "avg_conf%", "wr%"])

    # ── 5. Топ-15 комбо: detector × price_bucket × horizon ────────────────
    hdr("5. ТОП-15 КОМБО (detector × price_bucket × horizon)")

    rows = run_sql(f"""
        SELECT
            detector,
            CONCAT(
                ROUND(FLOOR((1-current_price)*10)/10*100)::int, '-',
                ROUND(FLOOR((1-current_price)*10)/10*100 + 10)::int, chr(162)
            ) AS range,
            CASE
                WHEN horizon_sec / 86400.0 <= 3   THEN '0-3d'
                WHEN horizon_sec / 86400.0 <= 7   THEN '3-7d'
                WHEN horizon_sec / 86400.0 <= 14  THEN '7-14d'
                WHEN horizon_sec / 86400.0 <= 30  THEN '14-30d'
                ELSE '30d+'
            END AS horizon_band,
            COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) AS n,
            ROUND(100.0 * COUNT(*) FILTER (WHERE outcome='WIN')
                  / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')), 0), 1) AS wr_pct,
            ROUND(AVG(realized_roi) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS roi_maker,
            ROUND(AVG(realized_roi_net) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS roi_net
        FROM detector_signals
        GROUP BY 1, FLOOR((1-current_price)*10)/10, 3
        HAVING COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) >= {MIN_N}
        ORDER BY ROUND(AVG(realized_roi) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) DESC
        LIMIT 15
    """)
    fmt(rows, ["detector", "range", "horizon", "n", "wr%", "roi_maker%", "roi_net%"])

    print("\n  Худшие 10:")
    rows = run_sql(f"""
        SELECT
            detector,
            CONCAT(
                ROUND(FLOOR((1-current_price)*10)/10*100)::int, '-',
                ROUND(FLOOR((1-current_price)*10)/10*100 + 10)::int, chr(162)
            ) AS range,
            CASE
                WHEN horizon_sec / 86400.0 <= 3   THEN '0-3d'
                WHEN horizon_sec / 86400.0 <= 7   THEN '3-7d'
                WHEN horizon_sec / 86400.0 <= 14  THEN '7-14d'
                WHEN horizon_sec / 86400.0 <= 30  THEN '14-30d'
                ELSE '30d+'
            END AS horizon_band,
            COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) AS n,
            ROUND(100.0 * COUNT(*) FILTER (WHERE outcome='WIN')
                  / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')), 0), 1) AS wr_pct,
            ROUND(AVG(realized_roi) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS roi_maker
        FROM detector_signals
        GROUP BY 1, FLOOR((1-current_price)*10)/10, 3
        HAVING COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) >= {MIN_N}
        ORDER BY ROUND(AVG(realized_roi) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) ASC
        LIMIT 10
    """)
    fmt(rows, ["detector", "range", "horizon", "n", "wr%", "roi_maker%"])

    # ── 6. Pre/post V2 split ───────────────────────────────────────────────
    hdr(f"6. PRE/POST V2 SPLIT (cutover {V2_CUTOVER})")

    rows = run_sql(f"""
        SELECT
            detector,
            CASE WHEN generated_at >= '{V2_CUTOVER}' THEN 'post-V2' ELSE 'pre-V2' END AS era,
            COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) AS n,
            ROUND(100.0 * COUNT(*) FILTER (WHERE outcome='WIN')
                  / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')), 0), 1) AS wr_pct,
            ROUND(AVG(realized_roi) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS roi_maker,
            ROUND(AVG(realized_roi_net) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS roi_net
        FROM detector_signals
        GROUP BY 1, 2
        HAVING COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) >= {MIN_N}
        ORDER BY 1, 2 DESC
    """)
    fmt(rows, ["detector", "era", "n", "wr%", "roi_maker%", "roi_net%"])

    # ── Бонус: D5/D6 тепловая карта price × horizon ───────────────────────
    hdr("БОНУС: D5/D6 тепловая карта (price_bucket × horizon)")

    for det in ("D5_long_horizon_extreme_fade", "D6_retail_bullish_overshoot"):
        rows = run_sql(f"""
            SELECT
                CONCAT(
                    ROUND(FLOOR((1-current_price)*10)/10*100)::int, '-',
                    ROUND(FLOOR((1-current_price)*10)/10*100 + 10)::int, chr(162)
                ) AS range,
                CASE
                    WHEN horizon_sec / 86400.0 <= 3   THEN '0-3d'
                    WHEN horizon_sec / 86400.0 <= 7   THEN '3-7d'
                    WHEN horizon_sec / 86400.0 <= 14  THEN '7-14d'
                    WHEN horizon_sec / 86400.0 <= 30  THEN '14-30d'
                    ELSE '30d+'
                END AS horizon_band,
                COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) AS n,
                ROUND(100.0 * COUNT(*) FILTER (WHERE outcome='WIN')
                      / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')), 0), 1) AS wr_pct,
                ROUND(AVG(realized_roi) FILTER (WHERE outcome IN ('WIN','LOSS')), 1) AS roi_maker,
                FLOOR((1-current_price)*10)/10 AS bucket_lo,
                CASE
                    WHEN horizon_sec / 86400.0 <= 3   THEN 1
                    WHEN horizon_sec / 86400.0 <= 7   THEN 2
                    WHEN horizon_sec / 86400.0 <= 14  THEN 3
                    WHEN horizon_sec / 86400.0 <= 30  THEN 4
                    ELSE 5
                END AS band_ord
            FROM detector_signals
            WHERE detector = '{det}'
            GROUP BY bucket_lo, 2, band_ord
            HAVING COUNT(*) FILTER (WHERE outcome IN ('WIN','LOSS')) >= {MIN_N}
            ORDER BY bucket_lo, band_ord
        """)
        if not rows:
            continue
        data = [r[:-2] for r in rows]  # drop bucket_lo, band_ord
        print(f"\n  {det}")
        fmt(data, ["range", "horizon", "n", "wr%", "roi_maker%"])

    print(f"\nDone in {round(time.time()-t0, 1)}s")


if __name__ == "__main__":
    main()
