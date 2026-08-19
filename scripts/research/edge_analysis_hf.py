"""Edge Analysis — полный срез по HF parquet.

Вопросы:
  1. Ценовой диапазон (бакеты 0-10¢, 10-20¢, ...) для NO и YES раздельно
  2. Категории
  3. Горизонт резолва (0-3d, 3-7d, 7-14d, 14-30d, 30d+)
  4. Ликвидность рынка (volume_usd бакеты)
  5. Топ комбо: диапазон × горизонт × категория
  6. Временной drift по годам

Метрики:
  roi_maker — ROI при maker (fee=0)
  roi_taker — ROI при taker (fee=2% от суммы ставки)
  win_rate
  n (число сделок)

Запуск: cd ~/oracle && python scripts/research/edge_analysis_hf.py
"""

import sys
import time

import duckdb

DATA = "/mnt/data/polymarket_dataset"
PARQUET = f"{DATA}/full_hf_enriched.parquet"

TAKER_FEE = 0.02  # 2% от суммы ставки

# Категории с малым объёмом исключаем
EXCLUDED_CATS = ("'Other'", "'Minute Markets'")
EXCL_SQL = ", ".join(EXCLUDED_CATS)

MIN_N = 30  # минимум сделок для отображения строки


def hdr(title: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {title}")
    print("=" * 70, flush=True)


def fmt(rows: list, cols: list) -> None:
    if not rows:
        print("  (нет данных)")
        return
    col_w = [max(len(str(c)), max(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    header = "  " + "  ".join(str(c).ljust(col_w[i]) for i, c in enumerate(cols))
    print(header)
    print("  " + "-" * (sum(col_w) + 2 * len(cols)))
    for row in rows:
        print("  " + "  ".join(str(v).ljust(col_w[i]) for i, v in enumerate(row)))


def main() -> None:
    t0 = time.time()
    print(f"Connecting to DuckDB and loading {PARQUET} ...")

    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    con.execute("SET threads=4")
    con.execute("SET temp_directory='/tmp/duckdb_edge'")

    # ── Базовая таблица ──────────────────────────────────────────────────
    # win=1 значит YES выиграл
    # NO-сделка: наш win = (win == 0), entry_no = 1 - entry_price
    # YES-сделка: наш win = (win == 1), entry = entry_price
    # roi_maker per trade: (payout / stake - 1) * 100
    #   YES: payout = 1/entry, stake = 1  →  roi = (1/entry_price - 1)*100
    #   NO:  payout = 1/(1-entry_price), stake = 1  →  roi = (1/(1-entry_price) - 1)*100
    #   taker fee: roi_taker = roi_maker - fee_pct * 100 / entry_price  (fee на stake)
    #   фактически: roi_taker = roi_maker - TAKER_FEE * 100

    con.execute(f"""
        CREATE OR REPLACE TABLE base AS
        SELECT
            market_id,
            side,
            CASE
                WHEN side = 'YES' THEN (win = 1)::int
                ELSE (win = 0)::int
            END AS trade_win,
            entry_price,
            CASE
                WHEN side = 'YES' THEN entry_price
                ELSE 1.0 - entry_price
            END AS entry_side,
            category,
            horizon_days,
            end_date,
            EXTRACT(year FROM end_date::date)::int AS year,
            CASE
                WHEN horizon_days <= 3  THEN '0-3d'
                WHEN horizon_days <= 7  THEN '3-7d'
                WHEN horizon_days <= 14 THEN '7-14d'
                WHEN horizon_days <= 30 THEN '14-30d'
                ELSE '30d+'
            END AS horizon_band,
            CASE
                WHEN side = 'YES' THEN
                    FLOOR(entry_price * 10) / 10
                ELSE
                    FLOOR((1.0 - entry_price) * 10) / 10
            END AS price_bucket_lo,
            -- volume_usd если есть
            TRY_CAST(volume AS DOUBLE) AS vol_usd
        FROM '{PARQUET}'
        WHERE
            win IS NOT NULL
            AND entry_price IS NOT NULL
            AND entry_price > 0 AND entry_price < 1
            AND side IN ('YES', 'NO')
            AND category NOT IN ({EXCL_SQL})
            AND category IS NOT NULL AND category != ''
            AND end_date IS NOT NULL
            AND horizon_days IS NOT NULL
    """)

    n_total = con.execute("SELECT COUNT(*) FROM base").fetchone()[0]
    n_yes = con.execute("SELECT COUNT(*) FROM base WHERE side='YES'").fetchone()[0]
    n_no = con.execute("SELECT COUNT(*) FROM base WHERE side='NO'").fetchone()[0]
    print(f"Loaded: {n_total:,} trades  (YES={n_yes:,}  NO={n_no:,})")

    # Добавляем roi_maker и roi_taker
    con.execute(f"""
        ALTER TABLE base ADD COLUMN roi_maker DOUBLE;
        ALTER TABLE base ADD COLUMN roi_taker DOUBLE;

        UPDATE base SET
            roi_maker = CASE
                WHEN trade_win = 1 THEN (1.0 / NULLIF(entry_side, 0) - 1.0) * 100
                ELSE -100.0
            END,
            roi_taker = CASE
                WHEN trade_win = 1 THEN (1.0 / NULLIF(entry_side, 0) - 1.0) * 100 - {TAKER_FEE * 100}
                ELSE -100.0
            END
    """)

    # ── 1. Ценовой диапазон ──────────────────────────────────────────────
    hdr("1. ЦЕНОВОЙ ДИАПАЗОН (entry price per share)")

    for side in ("NO", "YES"):
        print(f"\n  Side = {side}")
        rows = con.execute(f"""
            SELECT
                CONCAT(ROUND(price_bucket_lo * 100)::int, '-', ROUND((price_bucket_lo + 0.1) * 100)::int, '¢') AS range,
                COUNT(*) AS n,
                ROUND(AVG(trade_win) * 100, 1) AS wr_pct,
                ROUND(AVG(roi_maker), 1) AS roi_maker_pct,
                ROUND(AVG(roi_taker), 1) AS roi_taker_pct
            FROM base
            WHERE side = '{side}'
            GROUP BY price_bucket_lo
            HAVING COUNT(*) >= {MIN_N}
            ORDER BY price_bucket_lo
        """).fetchall()
        fmt(rows, ["range", "n", "wr%", "roi_maker%", "roi_taker%"])

    # ── 2. Категории ─────────────────────────────────────────────────────
    hdr("2. КАТЕГОРИИ")

    for side in ("NO", "YES"):
        print(f"\n  Side = {side}")
        rows = con.execute(f"""
            SELECT
                category,
                COUNT(*) AS n,
                ROUND(AVG(trade_win) * 100, 1) AS wr_pct,
                ROUND(AVG(roi_maker), 1) AS roi_maker_pct,
                ROUND(AVG(roi_taker), 1) AS roi_taker_pct
            FROM base
            WHERE side = '{side}'
            GROUP BY category
            HAVING COUNT(*) >= {MIN_N}
            ORDER BY roi_maker_pct DESC
        """).fetchall()
        fmt(rows, ["category", "n", "wr%", "roi_maker%", "roi_taker%"])

    # ── 3. Горизонт резолва ───────────────────────────────────────────────
    hdr("3. ГОРИЗОНТ РЕЗОЛВА")

    for side in ("NO", "YES"):
        print(f"\n  Side = {side}")
        rows = con.execute(f"""
            SELECT
                horizon_band,
                COUNT(*) AS n,
                ROUND(AVG(trade_win) * 100, 1) AS wr_pct,
                ROUND(AVG(roi_maker), 1) AS roi_maker_pct,
                ROUND(AVG(roi_taker), 1) AS roi_taker_pct
            FROM base
            WHERE side = '{side}'
            GROUP BY horizon_band
            HAVING COUNT(*) >= {MIN_N}
            ORDER BY
                CASE horizon_band
                    WHEN '0-3d'   THEN 1
                    WHEN '3-7d'   THEN 2
                    WHEN '7-14d'  THEN 3
                    WHEN '14-30d' THEN 4
                    ELSE 5
                END
        """).fetchall()
        fmt(rows, ["horizon", "n", "wr%", "roi_maker%", "roi_taker%"])

    # ── 4. Ликвидность (volume_usd) ───────────────────────────────────────
    hdr("4. ЛИКВИДНОСТЬ РЫНКА (volume_usd)")

    has_vol = con.execute("SELECT COUNT(*) FROM base WHERE vol_usd IS NOT NULL").fetchone()[0]
    if has_vol < MIN_N:
        print("  volume_usd недоступен в parquet — пропускаем")
    else:
        for side in ("NO", "YES"):
            print(f"\n  Side = {side}")
            rows = con.execute(f"""
                SELECT
                    CASE
                        WHEN vol_usd < 1000       THEN '<$1K'
                        WHEN vol_usd < 10000      THEN '$1K-$10K'
                        WHEN vol_usd < 100000     THEN '$10K-$100K'
                        WHEN vol_usd < 1000000    THEN '$100K-$1M'
                        ELSE '>$1M'
                    END AS vol_band,
                    COUNT(*) AS n,
                    ROUND(AVG(trade_win) * 100, 1) AS wr_pct,
                    ROUND(AVG(roi_maker), 1) AS roi_maker_pct,
                    ROUND(AVG(roi_taker), 1) AS roi_taker_pct
                FROM base
                WHERE side = '{side}' AND vol_usd IS NOT NULL
                GROUP BY 1
                HAVING COUNT(*) >= {MIN_N}
                ORDER BY
                    CASE
                        WHEN MIN(vol_usd) < 1000    THEN 1
                        WHEN MIN(vol_usd) < 10000   THEN 2
                        WHEN MIN(vol_usd) < 100000  THEN 3
                        WHEN MIN(vol_usd) < 1000000 THEN 4
                        ELSE 5
                    END
            """).fetchall()
            fmt(rows, ["vol_band", "n", "wr%", "roi_maker%", "roi_taker%"])

    # ── 5. Топ комбо: диапазон × горизонт × категория ─────────────────────
    hdr("5. ТОП КОМБО (price_range × horizon × category)")

    for side in ("NO", "YES"):
        print(f"\n  Side = {side} — топ 15 по roi_maker")
        rows = con.execute(f"""
            SELECT
                CONCAT(ROUND(price_bucket_lo * 100)::int, '-', ROUND((price_bucket_lo + 0.1) * 100)::int, '¢') AS range,
                horizon_band,
                category,
                COUNT(*) AS n,
                ROUND(AVG(trade_win) * 100, 1) AS wr_pct,
                ROUND(AVG(roi_maker), 1) AS roi_maker_pct,
                ROUND(AVG(roi_taker), 1) AS roi_taker_pct
            FROM base
            WHERE side = '{side}'
            GROUP BY price_bucket_lo, horizon_band, category
            HAVING COUNT(*) >= {MIN_N}
            ORDER BY roi_maker_pct DESC
            LIMIT 15
        """).fetchall()
        fmt(rows, ["range", "horizon", "category", "n", "wr%", "roi_maker%", "roi_taker%"])

        print(f"\n  Side = {side} — худшие 10 (куда НЕ ставить)")
        rows = con.execute(f"""
            SELECT
                CONCAT(ROUND(price_bucket_lo * 100)::int, '-', ROUND((price_bucket_lo + 0.1) * 100)::int, '¢') AS range,
                horizon_band,
                category,
                COUNT(*) AS n,
                ROUND(AVG(trade_win) * 100, 1) AS wr_pct,
                ROUND(AVG(roi_maker), 1) AS roi_maker_pct,
                ROUND(AVG(roi_taker), 1) AS roi_taker_pct
            FROM base
            WHERE side = '{side}'
            GROUP BY price_bucket_lo, horizon_band, category
            HAVING COUNT(*) >= {MIN_N}
            ORDER BY roi_maker_pct ASC
            LIMIT 10
        """).fetchall()
        fmt(rows, ["range", "horizon", "category", "n", "wr%", "roi_maker%", "roi_taker%"])

    # ── 6. Временной drift по годам ───────────────────────────────────────
    hdr("6. ВРЕМЕННОЙ DRIFT (по годам)")

    for side in ("NO", "YES"):
        print(f"\n  Side = {side}")
        rows = con.execute(f"""
            SELECT
                year,
                COUNT(*) AS n,
                ROUND(AVG(trade_win) * 100, 1) AS wr_pct,
                ROUND(AVG(roi_maker), 1) AS roi_maker_pct,
                ROUND(AVG(roi_taker), 1) AS roi_taker_pct
            FROM base
            WHERE side = '{side}' AND year >= 2021
            GROUP BY year
            HAVING COUNT(*) >= {MIN_N}
            ORDER BY year
        """).fetchall()
        fmt(rows, ["year", "n", "wr%", "roi_maker%", "roi_taker%"])

    # ── Бонус: тепловая карта price × horizon (NO) ───────────────────────
    hdr("БОНУС: price × horizon (NO) — roi_maker%")

    rows = con.execute(f"""
        SELECT
            CONCAT(ROUND(price_bucket_lo * 100)::int, '-', ROUND((price_bucket_lo + 0.1) * 100)::int, '¢') AS range,
            horizon_band,
            COUNT(*) AS n,
            ROUND(AVG(roi_maker), 1) AS roi_maker_pct,
            ROUND(AVG(roi_taker), 1) AS roi_taker_pct
        FROM base
        WHERE side = 'NO'
        GROUP BY price_bucket_lo, horizon_band
        HAVING COUNT(*) >= {MIN_N}
        ORDER BY price_bucket_lo, CASE horizon_band
            WHEN '0-3d' THEN 1 WHEN '3-7d' THEN 2
            WHEN '7-14d' THEN 3 WHEN '14-30d' THEN 4 ELSE 5
        END
    """).fetchall()
    fmt(rows, ["range", "horizon", "n", "roi_maker%", "roi_taker%"])

    elapsed = round(time.time() - t0, 1)
    print(f"\nDone in {elapsed}s")
    con.close()


if __name__ == "__main__":
    main()
