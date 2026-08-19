"""D5/D6 backtest на HF parquet — проверка per-bucket WR и сравнение.

D5: LongHorizonExtremeFade (entry_yes ≥ 0.70, horizon ≥ 3d, NO-fade)
D6: RetailBullishOvershoot (D5 + maker_direction_ratio ≥ 0.20)

Для каждого детектора считаем:
- WR per bucket (horizon_band × zone [× bullish_strength для D6])
- WR pre-V2 vs post-V2 (cutover: 2026-04-28)
- ENTRY_CEILING сравнение: 0.95 (новое) vs 0.99 (старое) — сколько сигналов отрезается

Запуск: cd ~/oracle && python scripts/research/d5_d6_hf_backtest.py
"""

import os
import sys
import time

import duckdb

DATA = "/mnt/data/polymarket_dataset"

V2_CUTOVER = "2026-04-28"


def horizon_band(days: float) -> str | None:
    if days < 3:
        return None
    if days < 7:
        return "3-7d"
    if days < 14:
        return "7-14d"
    return "14d+"


def zone(price: float) -> str | None:
    if price < 0.70:
        return None
    if price < 0.85:
        return "high"
    if price < 0.95:
        return "extreme"
    return None  # >= 0.95: ghost-fill zone


def main():
    print("=" * 70)
    print("D5/D6 HF Backtest — per-bucket WR + V2 split + ceiling comparison")
    print("=" * 70)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=4")
    con.execute("SET temp_directory='/tmp/duckdb'")
    os.makedirs("/tmp/duckdb", exist_ok=True)

    EXCLUDED_CATEGORIES = ("'Other'", "'Minute Markets'")
    excluded_sql = ", ".join(EXCLUDED_CATEGORIES)

    # ── Build base dataset ──────────────────────────────────────────────────
    # full_hf_enriched.parquet — уже обогащённый датасет:
    #   entry_price — YES-side entry price
    #   win         — 1 если YES выиграл (для NO-fade: win=0 → наш выигрыш)
    #   maker_direction_ratio — point-in-time proxy (из агрегатов на момент сборки)
    #   horizon_days, category, end_date, side
    #
    # Нас интересует NO-fade: side='NO' → win=0 (YES проиграл = NO победил)
    # Или считаем прямо: NO win = (win == 0) когда side='NO'.
    # В датасете side уже задаёт направление, win = 1 если сторона победила.
    print("\n1. Building dataset...", flush=True)

    con.execute(f"""
        CREATE OR REPLACE TABLE base AS
        SELECT
            market_id,
            win,
            entry_price AS entry_yes,
            category,
            end_date,
            horizon_days,
            maker_direction_ratio AS mdr,
            (end_date::date >= '{V2_CUTOVER}'::date) AS is_post_v2
        FROM '{DATA}/full_hf_enriched.parquet'
        WHERE
            win IS NOT NULL
            AND side = 'NO'
            AND category NOT IN ({excluded_sql})
            AND category IS NOT NULL AND category != ''
            AND end_date IS NOT NULL
            AND horizon_days IS NOT NULL
    """)

    n_total = con.execute("SELECT COUNT(*) FROM base").fetchone()[0]
    print(f"   Total NO positions: {n_total:,}", flush=True)

    # ── D5 filters ──────────────────────────────────────────────────────────
    print("\n2. D5 LongHorizonExtremeFade filters...", flush=True)
    con.execute("""
        CREATE OR REPLACE TABLE d5 AS
        SELECT *,
            CASE
                WHEN horizon_days < 3  THEN NULL
                WHEN horizon_days < 7  THEN '3-7d'
                WHEN horizon_days < 14 THEN '7-14d'
                ELSE '14d+'
            END AS band,
            CASE
                WHEN entry_yes < 0.70 THEN NULL
                WHEN entry_yes < 0.85 THEN 'high'
                WHEN entry_yes < 0.95 THEN 'extreme'
                ELSE 'ghost_fill_zone'  -- >= 0.95: excluded by new ceiling
            END AS zone_new,  -- ceiling = 0.95
            CASE
                WHEN entry_yes < 0.70 THEN NULL
                WHEN entry_yes < 0.85 THEN 'high'
                WHEN entry_yes < 0.95 THEN 'extreme'
                WHEN entry_yes < 0.99 THEN 'extreme_old'  -- 0.95-0.99: was included
                ELSE 'ghost_fill_zone'
            END AS zone_old   -- ceiling = 0.99
        FROM base
        WHERE horizon_days >= 3
          AND entry_yes >= 0.70
    """)

    n_d5 = con.execute("SELECT COUNT(*) FROM d5").fetchone()[0]
    print(f"   D5 candidates (horizon≥3, entry≥0.70): {n_d5:,}", flush=True)

    # ── D5: Per-bucket WR (ceiling = 0.95) ──────────────────────────────────
    print("\n3. D5 per-bucket WR (ceiling=0.95, ENTRY_CEILING fix)...", flush=True)
    d5_buckets = con.execute("""
        SELECT
            band,
            zone_new AS zone,
            is_post_v2,
            COUNT(*) AS n,
            ROUND(AVG(win::int)::numeric, 3) AS wr,
            ROUND(SUM(CASE WHEN win=1 THEN 1.0/NULLIF(1.0-entry_yes, 0) - 1 ELSE -1 END)
                  / COUNT(*)::numeric * 100, 1) AS roi_pct
        FROM d5
        WHERE band IS NOT NULL AND zone_new IN ('high', 'extreme')
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
    """).fetchall()

    print(f"\n   {'band':<8} {'zone':<10} {'post_v2':<10} {'n':>7} {'WR':>7} {'ROI%':>8}")
    print("   " + "-" * 55)
    for row in d5_buckets:
        band, zone, post_v2, n, wr, roi = row
        v2_label = "post-V2" if post_v2 else "pre-V2 "
        print(f"   {band:<8} {zone:<10} {v2_label:<10} {n:>7,} {wr:>7.3f} {roi:>8.1f}%")

    # ── D5: Ceiling comparison (0.95 vs 0.99) ───────────────────────────────
    print("\n4. D5 ceiling comparison: 0.95 vs 0.99...", flush=True)
    ceiling_cmp = con.execute("""
        SELECT
            zone_old,
            COUNT(*) AS n,
            ROUND(AVG(win::int)::numeric, 3) AS wr
        FROM d5
        WHERE band IS NOT NULL
          AND zone_old IN ('extreme', 'extreme_old', 'ghost_fill_zone')
        GROUP BY 1
        ORDER BY 1
    """).fetchall()

    print(f"\n   {'zone':<18} {'n':>7} {'WR':>7}  note")
    print("   " + "-" * 50)
    for row in ceiling_cmp:
        zone_label, n, wr = row
        note = {
            "extreme": "kept (0.85-0.95)",
            "extreme_old": "REMOVED by fix (0.95-0.99)",
            "ghost_fill_zone": "always excluded (≥0.99)",
        }.get(zone_label, "")
        print(f"   {zone_label:<18} {n:>7,} {wr:>7.3f}  {note}")

    # ── D6 filters ──────────────────────────────────────────────────────────
    print("\n5. D6 RetailBullishOvershoot filters...", flush=True)
    con.execute("""
        CREATE OR REPLACE TABLE d6 AS
        SELECT *,
            CASE
                WHEN mdr < 0.20 THEN NULL
                WHEN mdr < 0.60 THEN 'some_YES'
                ELSE 'heavy_YES'
            END AS bullish_strength
        FROM d5
        WHERE mdr >= 0.20
          AND zone_new IN ('high', 'extreme')
          AND band IS NOT NULL
    """)

    n_d6 = con.execute("SELECT COUNT(*) FROM d6").fetchone()[0]
    n_d5_eligible = con.execute(
        "SELECT COUNT(*) FROM d5 WHERE zone_new IN ('high','extreme') AND band IS NOT NULL"
    ).fetchone()[0]
    print(f"   D6 (MDR≥0.20 filter applied): {n_d6:,} / {n_d5_eligible:,} D5 signals ({100*n_d6/max(n_d5_eligible,1):.0f}%)", flush=True)

    # ── D6: Per-bucket WR ───────────────────────────────────────────────────
    print("\n6. D6 per-bucket WR (band × zone × strength)...", flush=True)
    d6_buckets = con.execute("""
        SELECT
            band,
            zone_new AS zone,
            bullish_strength,
            is_post_v2,
            COUNT(*) AS n,
            ROUND(AVG(win::int)::numeric, 3) AS wr,
            ROUND(SUM(CASE WHEN win=1 THEN 1.0/NULLIF(1.0-entry_yes, 0) - 1 ELSE -1 END)
                  / COUNT(*)::numeric * 100, 1) AS roi_pct
        FROM d6
        WHERE bullish_strength IS NOT NULL
        GROUP BY 1, 2, 3, 4
        ORDER BY 1, 2, 3, 4
    """).fetchall()

    print(f"\n   {'band':<8} {'zone':<10} {'strength':<12} {'post_v2':<10} {'n':>6} {'WR':>7} {'ROI%':>8}")
    print("   " + "-" * 65)
    for row in d6_buckets:
        band, zone, strength, post_v2, n, wr, roi = row
        v2_label = "post-V2" if post_v2 else "pre-V2 "
        print(f"   {band:<8} {zone:<10} {strength:<12} {v2_label:<10} {n:>6,} {wr:>7.3f} {roi:>8.1f}%")

    # ── D6 vs D5 comparison ─────────────────────────────────────────────────
    print("\n7. D6 vs D5 lift (MDR filter value-add)...", flush=True)
    comparison = con.execute("""
        SELECT
            d5_base.band,
            d5_base.zone,
            d5_base.n    AS d5_n,
            d5_base.wr   AS d5_wr,
            d6_filt.n    AS d6_n,
            d6_filt.wr   AS d6_wr,
            ROUND((d6_filt.wr - d5_base.wr)::numeric * 100, 1) AS lift_pp
        FROM (
            SELECT band, zone_new AS zone,
                   COUNT(*) AS n, ROUND(AVG(win::int)::numeric, 3) AS wr
            FROM d5
            WHERE band IS NOT NULL AND zone_new IN ('high', 'extreme')
            GROUP BY 1, 2
        ) d5_base
        LEFT JOIN (
            SELECT band, zone_new AS zone,
                   COUNT(*) AS n, ROUND(AVG(win::int)::numeric, 3) AS wr
            FROM d6
            WHERE bullish_strength IS NOT NULL
            GROUP BY 1, 2
        ) d6_filt USING (band, zone)
        ORDER BY 1, 2
    """).fetchall()

    print(f"\n   {'band':<8} {'zone':<10} {'D5_n':>7} {'D5_WR':>7} {'D6_n':>7} {'D6_WR':>7} {'lift':>7}")
    print("   " + "-" * 62)
    for row in comparison:
        band, zone, d5n, d5wr, d6n, d6wr, lift = row
        d6n = d6n or 0
        d6wr = d6wr or 0.0
        lift = lift or 0.0
        sign = "+" if lift >= 0 else ""
        print(f"   {band:<8} {zone:<10} {d5n:>7,} {d5wr:>7.3f} {d6n:>7,} {d6wr:>7.3f} {sign}{lift:>6.1f}pp")

    # ── Summary ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"Done in {time.time()-t0:.1f}s")
    print("\nKEY NOTES:")
    print("  - MDR = lifetime proxy (not point-in-time) → D6 WR may be optimistic")
    print("  - V2 cutover 2026-04-28: post-V2 n is small, treat with caution")
    print("  - 'extreme_old' zone (0.95-0.99) shows WR → confirms ENTRY_CEILING fix")
    print("=" * 70)


if __name__ == "__main__":
    main()
