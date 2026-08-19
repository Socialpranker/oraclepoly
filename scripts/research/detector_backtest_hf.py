"""Detector Backtest на HF parquet — rule-based симуляция D1/D4/D5/D6.

Для каждого детектора применяем его реальные фильтры к историческим NO-сделкам
и считаем ROI maker/taker, разбивку по зонам, категориям, годам.

D1 — аппроксимация: whale_tx_count >= 1 (реальный D1 реагирует на live events,
     в parquet только агрегат)

Запуск: cd ~/oracle && python scripts/research/detector_backtest_hf.py 2>&1
"""

import sys
import time

import duckdb

DATA = "/mnt/data/polymarket_dataset"
PARQUET = f"{DATA}/full_hf_enriched.parquet"

TAKER_FEE = 0.02
MIN_N = 30

# ── Детекторные параметры (из исходников) ──────────────────────────────────
D1_ENTRY_FLOOR   = 0.10
D1_ENTRY_CEILING = 0.85
D1_WHALE_MIN     = 1       # proxy: хотя бы 1 whale tx в рынке

D4_ENTRY_FLOOR   = 0.10
D4_ENTRY_CEILING = 0.85
D4_VOL_MAX       = 5_000   # volume < $5K
D4_CATS          = ("'Culture'", "'Politics'", "'Geopolitics'", "'Weather'", "'Sports'")
# D4: zone-aware — Culture/Weather требуют ≥0.50, Politics/Geo/Sports требуют ≥0.70
D4_CATS_MID      = ("'Culture'", "'Weather'")    # entry ≥ 0.50
D4_CATS_HIGH     = ("'Politics'", "'Geopolitics'", "'Sports'")  # entry ≥ 0.70

D5_ENTRY_FLOOR   = 0.70
D5_ENTRY_CEILING = 0.95
D5_HORIZON_MIN   = 3.0
D5_EXCL_CATS     = ("'Other'", "'Minute Markets'")

D6_ENTRY_FLOOR   = 0.70
D6_ENTRY_CEILING = 0.95
D6_HORIZON_MIN   = 3.0
D6_MDR_MIN       = 0.20    # maker_direction_ratio >= 0.20
D6_EXCL_CATS     = ("'Other'", "'Minute Markets'")


def hdr(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print("=" * 72, flush=True)


def subhdr(title: str) -> None:
    print(f"\n  ── {title} ──")


def fmt(rows: list, cols: list, indent: int = 2) -> None:
    if not rows:
        print(" " * indent + "(нет данных / n < MIN_N)")
        return
    pad = " " * indent
    col_w = [max(len(str(c)), max(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    header = pad + "  ".join(str(c).ljust(col_w[i]) for i, c in enumerate(cols))
    print(header)
    print(pad + "-" * (sum(col_w) + 2 * len(cols)))
    for row in rows:
        print(pad + "  ".join(str(v).ljust(col_w[i]) for i, v in enumerate(row)))


def roi_expr(win_col: str = "trade_win", entry_col: str = "entry_no") -> str:
    """SQL выражение для roi_maker и roi_taker per-trade."""
    return f"""
        CASE WHEN {win_col}=1 THEN (1.0/NULLIF({entry_col},0)-1.0)*100 ELSE -100.0 END AS roi_maker,
        CASE WHEN {win_col}=1 THEN (1.0/NULLIF({entry_col},0)-1.0)*100 - {TAKER_FEE*100} ELSE -100.0 END AS roi_taker
    """


HORIZON_BAND_EXPR = """
    CASE
        WHEN horizon_days <  3  THEN '0-3d'
        WHEN horizon_days <  7  THEN '3-7d'
        WHEN horizon_days < 14  THEN '7-14d'
        WHEN horizon_days < 30  THEN '14-30d'
        ELSE '30d+'
    END
"""

PRICE_ZONE_EXPR = """
    CASE
        WHEN entry_no < 0.10 THEN '<10¢'
        WHEN entry_no < 0.30 THEN '10-30¢'
        WHEN entry_no < 0.50 THEN '30-50¢'
        WHEN entry_no < 0.70 THEN '50-70¢'
        WHEN entry_no < 0.85 THEN '70-85¢'
        WHEN entry_no < 0.95 THEN '85-95¢'
        ELSE '95¢+'
    END
"""

YEAR_EXPR = "EXTRACT(year FROM created_at)::int"

AGG_COLS = ["n", "wr%", "roi_maker%", "roi_taker%"]

AGG_SQL = f"""
    COUNT(*) AS n,
    ROUND(AVG(trade_win)*100, 1) AS wr_pct,
    ROUND(AVG(roi_maker), 1) AS roi_maker_pct,
    ROUND(AVG(roi_taker), 1) AS roi_taker_pct
"""


def section(con: duckdb.DuckDBPyConnection, table: str, label: str) -> None:
    """Печатает полный срез: overall + по зонам + по горизонту + по категориям + по годам."""

    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"\n  Сделок в выборке: {n:,}")

    # Overall
    subhdr("Overall")
    rows = con.execute(f"SELECT {AGG_SQL} FROM {table}").fetchall()
    fmt(rows, AGG_COLS)

    # По ценовой зоне
    subhdr("По ценовой зоне (entry_no)")
    rows = con.execute(f"""
        SELECT {PRICE_ZONE_EXPR} AS zone, {AGG_SQL}
        FROM {table}
        GROUP BY 1 HAVING COUNT(*) >= {MIN_N}
        ORDER BY MIN(entry_no)
    """).fetchall()
    fmt(rows, ["zone"] + AGG_COLS)

    # По горизонту
    subhdr("По горизонту резолва")
    rows = con.execute(f"""
        SELECT {HORIZON_BAND_EXPR} AS hband, {AGG_SQL}
        FROM {table}
        GROUP BY 1 HAVING COUNT(*) >= {MIN_N}
        ORDER BY CASE {HORIZON_BAND_EXPR}
            WHEN '0-3d' THEN 1 WHEN '3-7d' THEN 2
            WHEN '7-14d' THEN 3 WHEN '14-30d' THEN 4 ELSE 5 END
    """).fetchall()
    fmt(rows, ["horizon"] + AGG_COLS)

    # По категории
    subhdr("По категории")
    rows = con.execute(f"""
        SELECT category, {AGG_SQL}
        FROM {table}
        GROUP BY 1 HAVING COUNT(*) >= {MIN_N}
        ORDER BY AVG(roi_maker) DESC
    """).fetchall()
    fmt(rows, ["category"] + AGG_COLS)

    # По году
    subhdr("По году (drift)")
    rows = con.execute(f"""
        SELECT {YEAR_EXPR} AS yr, {AGG_SQL}
        FROM {table}
        WHERE {YEAR_EXPR} >= 2022
        GROUP BY 1 HAVING COUNT(*) >= {MIN_N}
        ORDER BY 1
    """).fetchall()
    fmt(rows, ["year"] + AGG_COLS)

    # Тепловая карта zone × horizon
    subhdr("Тепловая карта: ценовая зона × горизонт (roi_maker%)")
    rows = con.execute(f"""
        SELECT
            {PRICE_ZONE_EXPR} AS zone,
            {HORIZON_BAND_EXPR} AS hband,
            COUNT(*) AS n,
            ROUND(AVG(roi_maker), 1) AS roi_maker_pct,
            ROUND(AVG(roi_taker), 1) AS roi_taker_pct
        FROM {table}
        GROUP BY 1, 2 HAVING COUNT(*) >= {MIN_N}
        ORDER BY MIN(entry_no),
            CASE {HORIZON_BAND_EXPR}
                WHEN '0-3d' THEN 1 WHEN '3-7d' THEN 2
                WHEN '7-14d' THEN 3 WHEN '14-30d' THEN 4 ELSE 5 END
    """).fetchall()
    fmt(rows, ["zone", "horizon", "n", "roi_maker%", "roi_taker%"])


def main() -> None:
    t0 = time.time()
    print(f"Loading {PARQUET} ...", flush=True)

    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    con.execute("SET threads=4")
    con.execute("SET temp_directory='/tmp/duckdb_det'")

    # ── Базовая таблица: все NO-сделки ──────────────────────────────────────
    con.execute(f"""
        CREATE OR REPLACE TABLE raw AS
        SELECT
            market_id,
            category,
            1 - entry_price          AS entry_no,
            (win = 0)::int           AS trade_win,
            horizon_days,
            created_at,
            volume,
            whale_tx_count,
            maker_direction_ratio,
            CASE WHEN (win=0)=true
                 THEN (1.0/NULLIF(1-entry_price,0)-1.0)*100
                 ELSE -100.0 END     AS roi_maker,
            CASE WHEN (win=0)=true
                 THEN (1.0/NULLIF(1-entry_price,0)-1.0)*100 - {TAKER_FEE*100}
                 ELSE -100.0 END     AS roi_taker
        FROM '{PARQUET}'
        WHERE
            win IS NOT NULL
            AND entry_price IS NOT NULL
            AND entry_price > 0 AND entry_price < 1
            AND side = 'NO'
            AND horizon_days IS NOT NULL
            AND created_at IS NOT NULL
    """)
    n_raw = con.execute("SELECT COUNT(*) FROM raw").fetchone()[0]
    print(f"Raw NO-trades: {n_raw:,}")

    # ── BASELINE ────────────────────────────────────────────────────────────
    hdr("BASELINE — все NO-сделки без фильтров")
    con.execute("CREATE OR REPLACE TABLE t_baseline AS SELECT * FROM raw")
    section(con, "t_baseline", "baseline")

    # ── D1-proxy ─────────────────────────────────────────────────────────────
    hdr(f"D1-proxy — FadeWhale (whale_tx_count>={D1_WHALE_MIN}, entry {D1_ENTRY_FLOOR}-{D1_ENTRY_CEILING}¢)")
    print("  ⚠️  Аппроксимация: реальный D1 реагирует на live-events, здесь whale_tx_count≥1")
    con.execute(f"""
        CREATE OR REPLACE TABLE t_d1 AS
        SELECT * FROM raw
        WHERE whale_tx_count >= {D1_WHALE_MIN}
          AND entry_no >= {D1_ENTRY_FLOOR}
          AND entry_no <  {D1_ENTRY_CEILING}
    """)
    section(con, "t_d1", "d1")

    # ── D4 ───────────────────────────────────────────────────────────────────
    hdr(f"D4 — SmallMarketFade (vol<${D4_VOL_MAX:,}, entry {D4_ENTRY_FLOOR}-{D4_ENTRY_CEILING}¢, cat whitelist)")
    d4_cats_sql  = ", ".join(D4_CATS)
    d4_mid_sql   = ", ".join(D4_CATS_MID)
    d4_high_sql  = ", ".join(D4_CATS_HIGH)
    con.execute(f"""
        CREATE OR REPLACE TABLE t_d4 AS
        SELECT * FROM raw
        WHERE category IN ({d4_cats_sql})
          AND volume < {D4_VOL_MAX}
          AND entry_no >= {D4_ENTRY_FLOOR}
          AND entry_no <  {D4_ENTRY_CEILING}
          AND (
              (category IN ({d4_mid_sql})  AND entry_no >= 0.50)
           OR (category IN ({d4_high_sql}) AND entry_no >= 0.70)
          )
    """)
    section(con, "t_d4", "d4")

    # ── D5 ───────────────────────────────────────────────────────────────────
    hdr(f"D5 — LongHorizonExtremeFade (entry {D5_ENTRY_FLOOR}-{D5_ENTRY_CEILING}¢, horizon≥{D5_HORIZON_MIN}d)")
    d5_excl_sql = ", ".join(D5_EXCL_CATS)
    con.execute(f"""
        CREATE OR REPLACE TABLE t_d5 AS
        SELECT * FROM raw
        WHERE entry_no >= {D5_ENTRY_FLOOR}
          AND entry_no <  {D5_ENTRY_CEILING}
          AND horizon_days >= {D5_HORIZON_MIN}
          AND category NOT IN ({d5_excl_sql})
    """)
    section(con, "t_d5", "d5")

    # ── D6 ───────────────────────────────────────────────────────────────────
    hdr(f"D6 — RetailBullishOvershoot (D5 + maker_direction_ratio≥{D6_MDR_MIN})")
    d6_excl_sql = ", ".join(D6_EXCL_CATS)
    con.execute(f"""
        CREATE OR REPLACE TABLE t_d6 AS
        SELECT * FROM raw
        WHERE entry_no >= {D6_ENTRY_FLOOR}
          AND entry_no <  {D6_ENTRY_CEILING}
          AND horizon_days >= {D6_HORIZON_MIN}
          AND category NOT IN ({d6_excl_sql})
          AND maker_direction_ratio >= {D6_MDR_MIN}
    """)
    section(con, "t_d6", "d6")

    # ── D5 ∩ D6 ──────────────────────────────────────────────────────────────
    hdr("D5∩D6 — пересечение (оба сигнала одновременно)")
    con.execute("""
        CREATE OR REPLACE TABLE t_d5_d6 AS
        SELECT * FROM t_d5
        WHERE market_id IN (SELECT market_id FROM t_d6)
          AND maker_direction_ratio >= 0.20
    """)
    section(con, "t_d5_d6", "d5_d6")

    # ── THRESHOLD SWEEP D5/D6 ────────────────────────────────────────────────
    hdr("THRESHOLD SWEEP — D5: ENTRY_CEILING × MIN_HORIZON (roi_maker%)")
    print()
    ceilings  = [0.80, 0.85, 0.90, 0.92, 0.95, 0.97, 0.99]
    horizons  = [1, 3, 5, 7, 14]

    print("  roi_maker% (n в скобках)  | " + "  ".join(f"ceil={c}" for c in ceilings))
    print("  " + "-" * (28 + len(ceilings) * 14))
    for h in horizons:
        row_parts = [f"  h>={h}d".ljust(28)]
        for c in ceilings:
            res = con.execute(f"""
                SELECT COUNT(*), ROUND(AVG(roi_maker),1)
                FROM raw
                WHERE entry_no >= {D5_ENTRY_FLOOR}
                  AND entry_no <  {c}
                  AND horizon_days >= {h}
                  AND category NOT IN ({d5_excl_sql})
            """).fetchone()
            n_val, roi_val = res
            if n_val >= MIN_N and roi_val is not None:
                row_parts.append(f"{roi_val:+.1f}({n_val:,})".ljust(14))
            else:
                row_parts.append("—".ljust(14))
        print("".join(row_parts))

    # ── MDR SWEEP D6 ─────────────────────────────────────────────────────────
    hdr("MDR SWEEP — D6: maker_direction_ratio порог (roi_maker%)")
    mdr_thresholds = [0.05, 0.10, 0.20, 0.40, 0.60, 0.80]
    print()
    print(f"  {'mdr_min':<10}  {'n':>8}  {'wr%':>6}  {'roi_maker%':>10}  {'roi_taker%':>10}")
    print("  " + "-" * 50)
    for mdr in mdr_thresholds:
        res = con.execute(f"""
            SELECT COUNT(*), ROUND(AVG(trade_win)*100,1), ROUND(AVG(roi_maker),1), ROUND(AVG(roi_taker),1)
            FROM raw
            WHERE entry_no >= {D6_ENTRY_FLOOR}
              AND entry_no <  {D6_ENTRY_CEILING}
              AND horizon_days >= {D6_HORIZON_MIN}
              AND category NOT IN ({d6_excl_sql})
              AND maker_direction_ratio >= {mdr}
        """).fetchone()
        n_val, wr, rm, rt = res
        if n_val >= MIN_N:
            print(f"  {f'>={mdr}':<10}  {n_val:>8,}  {wr:>6.1f}  {rm:>+10.1f}  {rt:>+10.1f}")
        else:
            print(f"  {f'>={mdr}':<10}  {'—':>8}")

    # ── CAPACITY vs ROI ───────────────────────────────────────────────────────
    hdr("CAPACITY vs ROI TRADEOFF — D5 по ценовым зонам (сколько теряем объём)")
    print()
    zones = [
        ("70-85¢",  0.70, 0.85),
        ("85-92¢",  0.85, 0.92),
        ("85-95¢",  0.85, 0.95),
        ("70-95¢",  0.70, 0.95),
        ("70-92¢",  0.70, 0.92),
    ]
    print(f"  {'zone':<12}  {'n':>8}  {'n% всех D5':>10}  {'roi_maker%':>10}  {'roi_taker%':>10}")
    print("  " + "-" * 56)
    n_d5 = con.execute("SELECT COUNT(*) FROM t_d5").fetchone()[0]
    for zname, zlo, zhi in zones:
        res = con.execute(f"""
            SELECT COUNT(*), ROUND(AVG(roi_maker),1), ROUND(AVG(roi_taker),1)
            FROM t_d5
            WHERE entry_no >= {zlo} AND entry_no < {zhi}
        """).fetchone()
        n_val, rm, rt = res
        pct = round(n_val / n_d5 * 100, 1) if n_d5 > 0 else 0
        print(f"  {zname:<12}  {n_val:>8,}  {pct:>10.1f}%  {rm:>+10.1f}  {rt:>+10.1f}")

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    hdr("SUMMARY — сравнение детекторов")
    print()
    detectors = [
        ("Baseline",  "t_baseline"),
        ("D1-proxy",  "t_d1"),
        ("D4",        "t_d4"),
        ("D5",        "t_d5"),
        ("D6",        "t_d6"),
        ("D5∩D6",     "t_d5_d6"),
    ]
    print(f"  {'detector':<12}  {'n':>8}  {'wr%':>6}  {'roi_maker%':>10}  {'roi_taker%':>10}  {'vs baseline':>12}")
    print("  " + "-" * 68)
    base_roi = None
    for name, tbl in detectors:
        res = con.execute(f"""
            SELECT COUNT(*), ROUND(AVG(trade_win)*100,1),
                   ROUND(AVG(roi_maker),1), ROUND(AVG(roi_taker),1)
            FROM {tbl}
        """).fetchone()
        n_val, wr, rm, rt = res
        if base_roi is None:
            base_roi = rm
            delta = "—"
        else:
            delta = f"{rm - base_roi:+.1f}pp"
        print(f"  {name:<12}  {n_val:>8,}  {wr:>6.1f}  {rm:>+10.1f}  {rt:>+10.1f}  {delta:>12}")

    elapsed = round(time.time() - t0, 1)
    print(f"\nDone in {elapsed}s")
    con.close()


if __name__ == "__main__":
    main()
