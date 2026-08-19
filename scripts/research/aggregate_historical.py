"""Агрегация исторического датасета Polymarket (trades.parquet -> compact aggregates)."""

import os
import time

import duckdb

TRADES = "/root/oracle/research/data/historical/trades.parquet"
OUT = "/mnt/data/polymarket_dataset"

con = duckdb.connect("/tmp/agg.duckdb")
con.execute("SET memory_limit='3GB'")
con.execute("SET threads=3")
con.execute("SET temp_directory='/tmp/duckdb'")
os.makedirs("/tmp/duckdb", exist_ok=True)

# ── 1. Market aggregates ──
print("1/3 Market aggregates...", flush=True)
t0 = time.time()
con.execute(f"""
    COPY (
        SELECT
            market_id, condition_id,
            count(*) as trade_count,
            sum(usd_amount) as total_volume_usd,
            avg(price) as avg_price,
            sum(price * usd_amount) / nullif(sum(usd_amount), 0) as vwap,
            min(price) as min_price, max(price) as max_price,
            stddev(price) as price_stddev,
            min(timestamp) as first_trade_ts, max(timestamp) as last_trade_ts,
            sum(CASE WHEN taker_direction = 'BUY' THEN usd_amount ELSE 0 END) as taker_buy_volume,
            sum(CASE WHEN taker_direction = 'SELL' THEN usd_amount ELSE 0 END) as taker_sell_volume,
            count(CASE WHEN usd_amount >= 1000 THEN 1 END) as whale_trade_count,
            sum(CASE WHEN usd_amount >= 1000 THEN usd_amount ELSE 0 END) as whale_volume,
            count(CASE WHEN usd_amount >= 10000 THEN 1 END) as big_trade_count
        FROM read_parquet('{TRADES}')
        GROUP BY market_id, condition_id
    ) TO '{OUT}/market_aggregates.parquet' (FORMAT PARQUET, COMPRESSION SNAPPY)
""")
print(f"   market_aggregates done in {time.time() - t0:.0f}s", flush=True)

# ── 2. Maker wallet aggregates ──
print("2/3 Maker wallet aggregates...", flush=True)
t0 = time.time()
con.execute(f"""
    COPY (
        SELECT
            maker as address,
            count(*) as trade_count,
            count(DISTINCT market_id) as markets_traded,
            sum(usd_amount) as total_volume_usd,
            avg(usd_amount) as avg_trade_size,
            max(usd_amount) as max_trade_size,
            min(timestamp) as first_trade_ts,
            max(timestamp) as last_trade_ts,
            sum(CASE WHEN maker_direction = 'BUY' THEN usd_amount ELSE 0 END) as buy_volume,
            sum(CASE WHEN maker_direction = 'SELL' THEN usd_amount ELSE 0 END) as sell_volume,
            count(CASE WHEN maker_direction = 'BUY' THEN 1 END) as buy_count,
            count(CASE WHEN maker_direction = 'SELL' THEN 1 END) as sell_count,
            avg(price) as avg_price
        FROM read_parquet('{TRADES}')
        GROUP BY maker
        HAVING count(*) >= 5
    ) TO '{OUT}/wallet_maker_agg.parquet' (FORMAT PARQUET, COMPRESSION SNAPPY)
""")
print(f"   wallet_maker_agg done in {time.time() - t0:.0f}s", flush=True)

# ── 3. Taker wallet aggregates ──
print("3/3 Taker wallet aggregates...", flush=True)
t0 = time.time()
con.execute(f"""
    COPY (
        SELECT
            taker as address,
            count(*) as trade_count,
            count(DISTINCT market_id) as markets_traded,
            sum(usd_amount) as total_volume_usd,
            avg(usd_amount) as avg_trade_size,
            max(usd_amount) as max_trade_size,
            min(timestamp) as first_trade_ts,
            max(timestamp) as last_trade_ts,
            sum(CASE WHEN taker_direction = 'BUY' THEN usd_amount ELSE 0 END) as buy_volume,
            sum(CASE WHEN taker_direction = 'SELL' THEN usd_amount ELSE 0 END) as sell_volume,
            count(CASE WHEN taker_direction = 'BUY' THEN 1 END) as buy_count,
            count(CASE WHEN taker_direction = 'SELL' THEN 1 END) as sell_count,
            avg(price) as avg_price
        FROM read_parquet('{TRADES}')
        GROUP BY taker
        HAVING count(*) >= 5
    ) TO '{OUT}/wallet_taker_agg.parquet' (FORMAT PARQUET, COMPRESSION SNAPPY)
""")
print(f"   wallet_taker_agg done in {time.time() - t0:.0f}s", flush=True)

con.close()
try:
    os.remove("/tmp/agg.duckdb")
except OSError:
    pass

# Summary
print("\n=== RESULTS ===", flush=True)
for f in sorted(os.listdir(OUT)):
    fp = os.path.join(OUT, f)
    if os.path.isfile(fp):
        sz = os.path.getsize(fp) / 1024**2
        print(f"  {f}: {sz:.1f} MB")
print("DONE", flush=True)
