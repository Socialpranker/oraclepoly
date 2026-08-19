"""Аудит всех фильтров pipeline на 6.8M historical positions.

Для каждого фильтра:
- Distribution (сколько позиций в каждом бине)
- NO side win rate с/без фильтра
- Volume × WR tradeoff
- Interaction с другими фильтрами

Запуск: cd ~/oracle && python scripts/research/filter_audit.py
"""

import os
import sys
import time

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

DATA = "/mnt/data/polymarket_dataset"
OUT = "/mnt/data/polymarket_dataset/analysis"
os.makedirs(OUT, exist_ok=True)


def main():
    print("=" * 70)
    print("FILTER AUDIT — 6.8M positions, all pipeline dimensions")
    print("=" * 70)
    t_start = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'")
    con.execute("SET threads=3")
    con.execute("SET temp_directory='/tmp/duckdb'")
    os.makedirs("/tmp/duckdb", exist_ok=True)

    # ── 1. Build base dataset ──
    print("\n1/6 Building dataset...", flush=True)
    con.execute(f"""
        CREATE OR REPLACE TABLE pos AS
        SELECT
            p.is_win,
            p.avg_price,
            CASE WHEN p.nonusdc_side = 'token1' THEN 'YES' ELSE 'NO' END as side,
            p.category,
            a.vwap as market_price,
            a.price_stddev,
            coalesce(a.whale_volume / nullif(a.total_volume_usd, 0), 0) as whale_fraction,
            a.whale_trade_count,
            coalesce((a.taker_buy_volume - a.taker_sell_volume)
                / nullif(a.taker_buy_volume + a.taker_sell_volume, 0), 0) as taker_imbalance,
            a.total_volume_usd as market_volume,
            a.trade_count as market_trades,
            a.big_trade_count,
            m.end_date,
            m.created_at as market_created,
            extract(epoch FROM m.end_date::timestamp - m.created_at::timestamp) / 86400.0 as days_to_resolve,
            -- Effective entry price
            CASE WHEN p.nonusdc_side = 'token1' THEN p.avg_price ELSE 1.0 - p.avg_price END as effective_entry
        FROM (
            SELECT * FROM '{DATA}/maker_pnl.parquet'
            WHERE is_resolved = true AND is_win IS NOT NULL AND avg_price BETWEEN 0.01 AND 0.99
            UNION ALL
            SELECT * FROM '{DATA}/taker_pnl.parquet'
            WHERE is_resolved = true AND is_win IS NOT NULL AND avg_price BETWEEN 0.01 AND 0.99
        ) p
        JOIN '{DATA}/market_aggregates.parquet' a ON p.condition_id = a.condition_id
        JOIN '{DATA}/market_enriched.parquet' m ON p.condition_id = m.condition_id
        WHERE a.total_volume_usd >= 100
          AND a.vwap BETWEEN 0.02 AND 0.98
          AND m.end_date IS NOT NULL
    """)

    total = con.execute("SELECT count(*) FROM pos").fetchone()[0]
    print(f"   {total:,} total positions")

    # Test period only
    con.execute("CREATE OR REPLACE TABLE test_pos AS SELECT * FROM pos WHERE end_date >= '2025-01-01' AND end_date < '2025-07-01'")
    test_n = con.execute("SELECT count(*) FROM test_pos").fetchone()[0]
    print(f"   {test_n:,} test period (2025-H1)")

    # ── 2. Dimension distributions ──
    print("\n2/6 DIMENSION DISTRIBUTIONS (test period, ALL positions)")
    print("=" * 70)

    # Side
    print("\n  SIDE:")
    r = con.execute("SELECT side, count(*) as n, round(100.0*avg(CASE WHEN is_win THEN 1 ELSE 0 END),1) as wr FROM test_pos GROUP BY side").fetchall()
    for row in r:
        print(f"    {row[0]}: {row[1]:>9,} positions, overall WR={row[2]}%")

    # Category
    print("\n  CATEGORY:")
    r = con.execute("""
        SELECT category, count(*) as n,
               round(100.0*count(*) FILTER (WHERE side='NO' AND is_win)/nullif(count(*) FILTER (WHERE side='NO'),0),1) as no_wr
        FROM test_pos GROUP BY category ORDER BY n DESC
    """).fetchall()
    for row in r:
        print(f"    {row[0]:<20} {row[1]:>9,}   NO WR={row[2]}%")

    # Whale
    print("\n  WHALE PRESENCE (whale_fraction thresholds):")
    for thresh in [0.0, 0.01, 0.05, 0.10, 0.20]:
        r = con.execute(f"""
            SELECT count(*) as n,
                   round(100.0*count(*) FILTER (WHERE side='NO' AND is_win)/nullif(count(*) FILTER (WHERE side='NO'),0),1) as no_wr
            FROM test_pos WHERE whale_fraction > {thresh}
        """).fetchone()
        print(f"    whale_frac > {thresh:.2f}: {r[0]:>9,}   NO WR={r[1]}%")

    r_no = con.execute("SELECT count(*) as n, round(100.0*count(*) FILTER (WHERE side='NO' AND is_win)/nullif(count(*) FILTER (WHERE side='NO'),0),1) FROM test_pos WHERE whale_fraction <= 0.01").fetchone()
    print(f"    whale_frac ≤ 0.01: {r_no[0]:>9,}   NO WR={r_no[1]}%")

    # Whale trade count
    print("\n  WHALE_TRADE_COUNT (from market_aggregates):")
    for val in [0, 1, 5, 10, 50]:
        r = con.execute(f"""
            SELECT count(*) as n,
                   round(100.0*count(*) FILTER (WHERE side='NO' AND is_win)/nullif(count(*) FILTER (WHERE side='NO'),0),1)
            FROM test_pos WHERE whale_trade_count > {val}
        """).fetchone()
        print(f"    whale_trades > {val}: {r[0]:>9,}   NO WR={r[1]}%")

    # Taker imbalance (micro proxy)
    print("\n  TAKER IMBALANCE (micro proxy):")
    for lo, hi, label in [(-1.01, -0.3, "sell_heavy"), (-0.3, -0.05, "sell_mild"),
                           (-0.05, 0.05, "balanced"), (0.05, 0.3, "buy_mild"), (0.3, 1.01, "buy_heavy")]:
        r = con.execute(f"""
            SELECT count(*) as n,
                   round(100.0*count(*) FILTER (WHERE side='NO' AND is_win)/nullif(count(*) FILTER (WHERE side='NO'),0),1)
            FROM test_pos WHERE taker_imbalance >= {lo} AND taker_imbalance < {hi}
        """).fetchone()
        print(f"    {label:<12}: {r[0]:>9,}   NO WR={r[1]}%")

    # Price stddev (volatility)
    print("\n  PRICE STDDEV (volatility):")
    for lo, hi, label in [(0, 0.05, "very_low"), (0.05, 0.10, "low"), (0.10, 0.20, "mid"),
                           (0.20, 0.35, "high"), (0.35, 1.01, "extreme")]:
        r = con.execute(f"""
            SELECT count(*) as n,
                   round(100.0*count(*) FILTER (WHERE side='NO' AND is_win)/nullif(count(*) FILTER (WHERE side='NO'),0),1)
            FROM test_pos WHERE price_stddev >= {lo} AND price_stddev < {hi}
        """).fetchone()
        nv = r[0] if r[0] else 0
        wr = r[1] if r[1] else 0
        print(f"    {label:<12}: {nv:>9,}   NO WR={wr}%")

    # Days to resolve (horizon)
    print("\n  DAYS TO RESOLVE (horizon):")
    for lo, hi, label in [(0, 3, "0-3d"), (3, 7, "3-7d"), (7, 14, "7-14d"),
                           (14, 30, "14-30d"), (30, 90, "30-90d"), (90, 9999, "90d+")]:
        r = con.execute(f"""
            SELECT count(*) as n,
                   round(100.0*count(*) FILTER (WHERE side='NO' AND is_win)/nullif(count(*) FILTER (WHERE side='NO'),0),1)
            FROM test_pos WHERE days_to_resolve >= {lo} AND days_to_resolve < {hi}
        """).fetchone()
        nv = r[0] if r[0] else 0
        wr = r[1] if r[1] else 0
        print(f"    {label:<12}: {nv:>9,}   NO WR={wr}%")

    # Effective entry price (floor filter)
    print("\n  EFFECTIVE ENTRY PRICE (NO side only):")
    for lo, hi, label in [(0, 0.05, "0-5¢"), (0.05, 0.10, "5-10¢"), (0.10, 0.15, "10-15¢"),
                           (0.15, 0.25, "15-25¢"), (0.25, 0.40, "25-40¢"), (0.40, 1.0, "40¢+")]:
        r = con.execute(f"""
            SELECT count(*) as n,
                   round(100.0*count(*) FILTER (WHERE is_win)/nullif(count(*),0),1) as wr
            FROM test_pos WHERE side = 'NO' AND effective_entry >= {lo} AND effective_entry < {hi}
        """).fetchone()
        nv = r[0] if r[0] else 0
        wr = r[1] if r[1] else 0
        print(f"    {label:<12}: {nv:>9,}   WR={wr}%")

    # Market volume
    print("\n  MARKET VOLUME:")
    for lo, hi, label in [(100, 1000, "$100-1K"), (1000, 10000, "$1K-10K"), (10000, 100000, "$10K-100K"),
                           (100000, 1000000, "$100K-1M"), (1000000, 1e9, "$1M+")]:
        r = con.execute(f"""
            SELECT count(*) as n,
                   round(100.0*count(*) FILTER (WHERE side='NO' AND is_win)/nullif(count(*) FILTER (WHERE side='NO'),0),1)
            FROM test_pos WHERE market_volume >= {lo} AND market_volume < {hi}
        """).fetchone()
        nv = r[0] if r[0] else 0
        wr = r[1] if r[1] else 0
        print(f"    {label:<12}: {nv:>9,}   NO WR={wr}%")

    # ── 3. Category mapping: historical vs production ──
    print("\n\n3/6 CATEGORY MAPPING")
    print("=" * 70)
    print("\n  Historical categories:")
    r = con.execute("SELECT category, count(*) FROM test_pos GROUP BY category ORDER BY count(*) DESC").fetchall()
    hist_cats = set()
    for row in r:
        hist_cats.add(row[0])
        print(f"    {row[0]}: {row[1]:,}")

    print("\n  Production categories (from oracle_snapshots, known):")
    prod_cats = {"Sports", "Crypto", "Politics", "Tech & AI", "Culture", "World & Economy"}
    for c in sorted(prod_cats):
        in_hist = "✅" if c in hist_cats else "❌ NOT IN HISTORICAL"
        print(f"    {c}: {in_hist}")

    print("\n  Historical-only (not in production):")
    for c in sorted(hist_cats - prod_cats):
        n = con.execute(f"SELECT count(*) FROM test_pos WHERE category = '{c}'").fetchone()[0]
        print(f"    {c}: {n:,} ← mapping needed or skip")

    # ── 4. Best NO combos ──
    print("\n\n4/6 BEST NO-SIDE COMBINATIONS (test period)")
    print("=" * 70)

    r = con.execute("""
        SELECT
            category,
            CASE
                WHEN market_price >= 0.80 THEN '80-100%'
                WHEN market_price >= 0.60 THEN '60-80%'
                WHEN market_price >= 0.40 THEN '40-60%'
                ELSE '<40%'
            END as price_zone,
            CASE WHEN whale_trade_count > 0 THEN 'whale' ELSE 'no_whale' END as whale,
            CASE
                WHEN taker_imbalance < -0.05 THEN 'sell'
                WHEN taker_imbalance > 0.05 THEN 'buy'
                ELSE 'balanced'
            END as taker,
            count(*) as trades,
            round(100.0 * avg(CASE WHEN is_win THEN 1 ELSE 0 END), 1) as wr,
            round(avg(1.0/GREATEST(effective_entry, 0.01) - 1) * avg(CASE WHEN is_win THEN 1 ELSE 0 END)
                  - (1 - avg(CASE WHEN is_win THEN 1 ELSE 0 END)), 4) as ev_per_dollar
        FROM test_pos
        WHERE side = 'NO'
        GROUP BY 1, 2, 3, 4
        HAVING count(*) >= 50
        ORDER BY ev_per_dollar DESC
        LIMIT 30
    """).fetchall()

    print(f"\n  {'Category':<15} {'Price':>8} {'Whale':>9} {'Taker':>8} {'Trades':>7} {'WR':>5} {'EV/$':>7}")
    print(f"  {'-'*15} {'-'*8} {'-'*9} {'-'*8} {'-'*7} {'-'*5} {'-'*7}")
    for row in r:
        print(f"  {row[0]:<15} {row[1]:>8} {row[2]:>9} {row[3]:>8} {row[4]:>7,} {row[5]:>4.0f}% {row[6]:>7.4f}")

    # ── 5. Worst combos (what to avoid) ──
    print("\n\n5/6 WORST NO-SIDE COMBINATIONS (avoid)")
    print("=" * 70)

    r = con.execute("""
        SELECT
            category,
            CASE
                WHEN market_price >= 0.80 THEN '80-100%'
                WHEN market_price >= 0.60 THEN '60-80%'
                WHEN market_price >= 0.40 THEN '40-60%'
                ELSE '<40%'
            END as price_zone,
            CASE WHEN whale_trade_count > 0 THEN 'whale' ELSE 'no_whale' END as whale,
            CASE
                WHEN taker_imbalance < -0.05 THEN 'sell'
                WHEN taker_imbalance > 0.05 THEN 'buy'
                ELSE 'balanced'
            END as taker,
            count(*) as trades,
            round(100.0 * avg(CASE WHEN is_win THEN 1 ELSE 0 END), 1) as wr,
            round(avg(1.0/GREATEST(effective_entry, 0.01) - 1) * avg(CASE WHEN is_win THEN 1 ELSE 0 END)
                  - (1 - avg(CASE WHEN is_win THEN 1 ELSE 0 END)), 4) as ev_per_dollar
        FROM test_pos
        WHERE side = 'NO'
        GROUP BY 1, 2, 3, 4
        HAVING count(*) >= 50
        ORDER BY ev_per_dollar ASC
        LIMIT 15
    """).fetchall()

    print(f"\n  {'Category':<15} {'Price':>8} {'Whale':>9} {'Taker':>8} {'Trades':>7} {'WR':>5} {'EV/$':>7}")
    print(f"  {'-'*15} {'-'*8} {'-'*9} {'-'*8} {'-'*7} {'-'*5} {'-'*7}")
    for row in r:
        print(f"  {row[0]:<15} {row[1]:>8} {row[2]:>9} {row[3]:>8} {row[4]:>7,} {row[5]:>4.0f}% {row[6]:>7.4f}")

    # ── 6. Filter interaction matrix ──
    print("\n\n6/6 FILTER INTERACTION (NO side, WR impact)")
    print("=" * 70)

    # whale × taker × price_zone
    print("\n  whale × taker (NO side, all categories):")
    r = con.execute("""
        SELECT
            CASE WHEN whale_trade_count > 0 THEN 'whale' ELSE 'no_whale' END as whale,
            CASE
                WHEN taker_imbalance < -0.05 THEN 'sell'
                WHEN taker_imbalance > 0.05 THEN 'buy'
                ELSE 'balanced'
            END as taker,
            count(*) as trades,
            round(100.0 * avg(CASE WHEN is_win THEN 1 ELSE 0 END), 1) as wr
        FROM test_pos WHERE side = 'NO'
        GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchall()
    for row in r:
        print(f"    {row[0]:>9} × {row[1]:>8}: {row[2]:>8,} trades, WR={row[3]}%")

    # price_zone × volatility
    print("\n  price_zone × volatility (NO side):")
    r = con.execute("""
        SELECT
            CASE
                WHEN market_price >= 0.80 THEN '80-100%'
                WHEN market_price >= 0.60 THEN '60-80%'
                ELSE '<60%'
            END as price,
            CASE WHEN price_stddev >= 0.20 THEN 'high_vol' ELSE 'low_vol' END as vol,
            count(*) as trades,
            round(100.0 * avg(CASE WHEN is_win THEN 1 ELSE 0 END), 1) as wr
        FROM test_pos WHERE side = 'NO'
        GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchall()
    for row in r:
        print(f"    {row[0]:>8} × {row[1]:>8}: {row[2]:>8,} trades, WR={row[3]}%")

    # Save
    print(f"\nTotal time: {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()
