#!/usr/bin/env python3
"""Backtest D26: Fade-Whale detector hypothesis.

Hypothesis: когда wallet из FADE_WHALE cohort (10,115 wallets с high impact
+ low historic ROI) делает большой push, price reverses в течение 1-4h.

Methodology:
  1. JOIN users.parquet с fade_whale_set
  2. Для каждого push >= MIN_PUSH_USD:
     entry_price = price at push moment
     exit_price_Xh = avg price [entry+5min, entry+Xh]
     reverse_return = -(exit - entry) / entry   (negative because we fade)
  3. Aggregate: WR (price reversed), avg ROI per trade
  4. Сравнить с DOUBLE_ELITE (control): должен показать opposite signal

Если D26 показывает positive net ROI после fees+slippage → строим production.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb

USERS_PATH = "/mnt/data/hf_data2/alllong/users.parquet"
COHORTS_PATH = "/mnt/data/canonical/wallet_alchemy_classes.parquet"
MARKETS_PATH = "/mnt/data/canonical/markets_clean.parquet"

# Push threshold — wallet pushes ≥ this in a minute
MIN_PUSH_USD = 1000
MIN_PRICE_DRIFT_PP = 0.005  # 0.5pp move in the minute

# Hold horizon — check reversion at these points
HOLD_HOURS = [1, 2, 4, 12, 24]

# Slippage/fees (conservative)
ROUND_TRIP_FEE_PP = 0.5  # ~0.25 each leg dynamic
SLIPPAGE_PP = 1.5


def main() -> int:
    print("=" * 70, flush=True)
    print("Backtest D26: Fade-Whale detector hypothesis", flush=True)
    print("=" * 70, flush=True)

    con = duckdb.connect()
    con.execute("SET memory_limit='10GB'")
    con.execute("SET threads=4")
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("PRAGMA max_temp_directory_size='200GiB'")

    print(f"\n→ users: {USERS_PATH}", flush=True)
    print(f"→ cohorts: {COHORTS_PATH}", flush=True)
    print(f"→ min push: ${MIN_PUSH_USD}, min drift: {MIN_PRICE_DRIFT_PP*100:.1f}pp", flush=True)
    print(f"→ horizons: {HOLD_HOURS}h", flush=True)

    # Stage 1: identify push events for FADE_WHALE and DOUBLE_ELITE wallets
    print("\n[1/3] Building push events table...", flush=True)
    t0 = time.time()

    sql_push_events = f"""
    CREATE OR REPLACE TABLE push_events AS
    WITH minute_buckets AS (
      SELECT
        u.user AS wallet,
        u.condition_id,
        DATE_TRUNC('minute', TO_TIMESTAMP(u.timestamp)) AS minute_ts,
        EXTRACT(EPOCH FROM DATE_TRUNC('minute', TO_TIMESTAMP(u.timestamp)))::BIGINT AS minute_unix,
        SUM(u.usd_amount) AS volume_pushed,
        (LAST(u.price ORDER BY u.timestamp) - FIRST(u.price ORDER BY u.timestamp)) AS price_drift,
        AVG(u.price) AS push_price,
        c.class_label
      FROM read_parquet('{USERS_PATH}') u
      JOIN read_parquet('{COHORTS_PATH}') c ON c.wallet = u.user
      WHERE u.role = 'taker'
        AND u.price BETWEEN 0.01 AND 0.99
        AND u.usd_amount > 0
      GROUP BY u.user, u.condition_id, DATE_TRUNC('minute', TO_TIMESTAMP(u.timestamp)), c.class_label
      HAVING SUM(u.usd_amount) >= {MIN_PUSH_USD}
         AND ABS(LAST(u.price ORDER BY u.timestamp) - FIRST(u.price ORDER BY u.timestamp)) >= {MIN_PRICE_DRIFT_PP}
    )
    SELECT * FROM minute_buckets;
    """
    con.execute(sql_push_events)
    n_pushes = con.execute("SELECT COUNT(*) FROM push_events").fetchone()[0]
    print(f"  Push events: {n_pushes:,}", flush=True)
    for cls, n in con.execute("SELECT class_label, COUNT(*) FROM push_events GROUP BY 1").fetchall():
        print(f"    {cls}: {n:,}", flush=True)
    print(f"  Built in {time.time()-t0:.1f}s", flush=True)

    # Stage 2: для каждого push event найти exit prices через X часов
    # Используем все taker trades на тот же condition_id как proxy для market price.
    print("\n[2/3] Computing reversion returns at horizons...", flush=True)
    t0 = time.time()

    # Subquery: for each push, find avg price within (push_ts + h*3600 - 5min, push_ts + h*3600 + 5min)
    # Это дорого — используем efficient approach с window-like JOIN

    for h in HOLD_HOURS:
        h_sec = h * 3600
        win_sec = 600  # ±10 минут окно
        sql = f"""
        CREATE OR REPLACE TABLE reversion_h{h} AS
        WITH exit_prices AS (
          SELECT
            pe.wallet, pe.condition_id, pe.minute_ts, pe.class_label,
            pe.volume_pushed, pe.price_drift, pe.push_price,
            AVG(u.price) FILTER (WHERE u.timestamp BETWEEN pe.minute_unix + {h_sec} - {win_sec} AND pe.minute_unix + {h_sec} + {win_sec}) AS exit_price
          FROM push_events pe
          LEFT JOIN read_parquet('{USERS_PATH}') u
                ON u.condition_id = pe.condition_id
               AND u.timestamp BETWEEN pe.minute_unix + {h_sec} - {win_sec} AND pe.minute_unix + {h_sec} + {win_sec}
          GROUP BY pe.wallet, pe.condition_id, pe.minute_ts, pe.class_label,
                   pe.volume_pushed, pe.price_drift, pe.push_price, pe.minute_unix
        )
        SELECT *,
               -- Direction: если push UP (price_drift > 0), мы FADE → продаём → sell at push_price, buy back at exit_price
               -- fade_return = (push_price - exit_price) / push_price  для UP-push
               -- fade_return = (exit_price - push_price) / push_price  для DOWN-push (мы BUY обратное)
               -- Унифицированно: SIGN(-price_drift) * (exit - push) / push
               -- =  (push - exit) / push * SIGN(price_drift)
               CASE
                 WHEN exit_price IS NULL OR push_price IS NULL OR push_price = 0 THEN NULL
                 ELSE SIGN(price_drift) * (push_price - exit_price) / push_price
               END AS fade_return_raw
        FROM exit_prices;
        """
        con.execute(sql)
        sample = con.execute(f"SELECT COUNT(*) FILTER (WHERE fade_return_raw IS NOT NULL) FROM reversion_h{h}").fetchone()[0]
        print(f"  h={h}h: {sample:,} valid samples", flush=True)
    print(f"  Built in {time.time()-t0:.1f}s", flush=True)

    # Stage 3: aggregate per (class, horizon)
    print("\n[3/3] Aggregating results by (class, horizon)...", flush=True)
    print()
    print(f"{'class':<14} {'h':>3} {'n':>8} {'WR_pct':>7} {'avg_raw':>9} {'med_raw':>9} {'after_costs':>11}")
    print("-" * 70)
    for h in HOLD_HOURS:
        rows = con.execute(f"""
            SELECT class_label, COUNT(*) AS n,
                   ROUND(100.0 * COUNT(*) FILTER (WHERE fade_return_raw > 0) / NULLIF(COUNT(*), 0)::numeric, 1) AS wr,
                   ROUND(AVG(fade_return_raw)::numeric * 100, 2) AS avg_raw_pct,
                   ROUND(MEDIAN(fade_return_raw)::numeric * 100, 2) AS med_raw_pct
            FROM reversion_h{h}
            WHERE fade_return_raw IS NOT NULL
            GROUP BY class_label ORDER BY class_label;
        """).fetchall()
        for r in rows:
            cls, n, wr, avg_raw, med_raw = r
            net = float(avg_raw) - ROUND_TRIP_FEE_PP - SLIPPAGE_PP
            marker = "✅" if net > 1 else ("⚠️" if net > 0 else "❌")
            print(f"{cls:<14} {h:>3}h {n:>8,} {wr:>6}% {avg_raw:>8}% {med_raw:>8}% {net:>+10.2f}% {marker}")

    print()
    print("Interpretation:")
    print("  - fade_whale: positive avg_raw означает их moves reverse → fade работает")
    print("  - double_elite: NEGATIVE avg_raw означает их moves persist → follow работает")
    print("  - after_costs учитывает fees 0.5pp round-trip + slippage 1.5pp = 2pp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
