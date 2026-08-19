"""Фаза 2: wallet-market агрегаты + enrichment с outcomes и categories.

Делаем в 4 шага без UNION ALL:
1. maker_market_agg — per maker per market
2. taker_market_agg — per taker per market
3. market_enriched — markets + outcomes + category (из markets.parquet)
4. wallet_pnl — JOIN wallet_market с resolved outcomes → ROI per wallet
"""

import os
import time

import duckdb

TRADES = "/root/oracle/research/data/historical/trades.parquet"
MKT = "/mnt/data/polymarket_dataset/markets.parquet"
OUT = "/mnt/data/polymarket_dataset"

con = duckdb.connect("/tmp/agg_p2.duckdb")
con.execute("SET memory_limit='3GB'")
con.execute("SET threads=3")
con.execute("SET temp_directory='/tmp/duckdb'")
con.execute("SET preserve_insertion_order=false")
os.makedirs("/tmp/duckdb", exist_ok=True)

# ── 1. Market enriched: outcomes + categories ──
print("1/5 Market enriched (outcomes + categories)...", flush=True)
t0 = time.time()
con.execute(f"""
    COPY (
        SELECT
            condition_id,
            id as market_id_gamma,
            question,
            answer1, answer2,
            closed,
            outcome_prices,
            -- Определяем winner: closed=1 и outcome_prices содержит '1' на позиции 0 или 1
            CASE
                WHEN closed = 1 AND outcome_prices LIKE '[''1''%' THEN answer1
                WHEN closed = 1 AND outcome_prices LIKE '%''1'']' THEN answer2
                WHEN closed = 1 AND outcome_prices LIKE '["1"%' THEN answer1
                WHEN closed = 1 AND outcome_prices LIKE '%"1"]' THEN answer2
                ELSE NULL
            END as winner,
            CASE WHEN closed = 1 THEN true ELSE false END as is_resolved,
            volume,
            neg_risk,
            -- Infer category from question keywords
            CASE
                WHEN lower(question) SIMILAR TO '%(bitcoin|btc|eth|crypto|solana|xrp|doge|token|defi|nft)%' THEN 'Crypto'
                WHEN lower(question) SIMILAR TO '%(president|election|trump|biden|congress|senate|governor|democrat|republican|vote|poll)%' THEN 'Politics'
                WHEN lower(question) SIMILAR TO '%(nfl|nba|mlb|nhl|soccer|football|game|match|championship|super bowl|world cup|ufc|boxing)%' THEN 'Sports'
                WHEN lower(question) SIMILAR TO '%(ai |openai|chatgpt|google|apple|microsoft|tech|software|startup)%' THEN 'Tech & AI'
                WHEN lower(question) SIMILAR TO '%(movie|oscar|grammy|album|celebrity|kardashian|swift|music|film)%' THEN 'Culture'
                WHEN lower(question) SIMILAR TO '%(fed |inflation|gdp|interest rate|stock|s&p|nasdaq|dow|market cap|earnings)%' THEN 'Finance'
                WHEN lower(question) SIMILAR TO '%(war|russia|ukraine|china|nato|sanctions|missile|military)%' THEN 'Geopolitics'
                WHEN lower(question) SIMILAR TO '%(weather|temperature|hurricane|earthquake|climate)%' THEN 'Weather'
                ELSE 'Other'
            END as category,
            created_at,
            end_date,
            token1, token2
        FROM '{MKT}'
    ) TO '{OUT}/market_enriched.parquet' (FORMAT PARQUET, COMPRESSION SNAPPY)
""")
cnt = con.execute(f"SELECT count(*) FROM '{OUT}/market_enriched.parquet'").fetchone()[0]
resolved = con.execute(f"SELECT count(*) FROM '{OUT}/market_enriched.parquet' WHERE is_resolved = true").fetchone()[0]
with_winner = con.execute(f"SELECT count(*) FROM '{OUT}/market_enriched.parquet' WHERE winner IS NOT NULL").fetchone()[0]
print(f"   Done: {cnt:,} markets, {resolved:,} resolved, {with_winner:,} with winner, {time.time()-t0:.0f}s", flush=True)

# Проверка категорий
print("   Categories:", flush=True)
cats = con.execute(f"SELECT category, count(*) as cnt FROM '{OUT}/market_enriched.parquet' GROUP BY category ORDER BY cnt DESC").fetchall()
for cat, c in cats:
    print(f"     {cat}: {c:,}", flush=True)

con.close()
try:
    os.remove("/tmp/agg_p2.duckdb")
except OSError:
    pass
print("Step 1 complete. Now downloading trades for wallet-market aggs...\n", flush=True)

# ── Шаги 2-5 требуют trades.parquet ──
# Проверяем наличие
if not os.path.exists(TRADES):
    print("trades.parquet not found. Downloading...", flush=True)
    from huggingface_hub import hf_hub_download
    t0 = time.time()
    hf_hub_download(
        "SII-WANGZJ/Polymarket_data",
        "trades.parquet",
        repo_type="dataset",
        local_dir="/root/oracle/research/data/historical",
    )
    print(f"   Downloaded in {time.time()-t0:.0f}s", flush=True)

# Reconnect
con = duckdb.connect("/tmp/agg_p2.duckdb")
con.execute("SET memory_limit='3GB'")
con.execute("SET threads=1")
con.execute("SET temp_directory='/tmp/duckdb'")
con.execute("SET preserve_insertion_order=false")

# ── 2. Maker-market aggregates ──
print("2/5 Maker-market aggregates...", flush=True)
t0 = time.time()
con.execute(f"""
    COPY (
        SELECT
            maker as address,
            t.condition_id,
            maker_direction as direction,
            nonusdc_side,
            count(*) as trade_count,
            sum(usd_amount) as volume_usd,
            sum(token_amount) as total_tokens,
            avg(price) as avg_price,
            sum(CASE WHEN maker_direction = 'BUY' THEN usd_amount ELSE 0 END) as buy_usd,
            sum(CASE WHEN maker_direction = 'SELL' THEN usd_amount ELSE 0 END) as sell_usd,
            sum(CASE WHEN maker_direction = 'BUY' THEN token_amount ELSE 0 END) as buy_tokens,
            sum(CASE WHEN maker_direction = 'SELL' THEN token_amount ELSE 0 END) as sell_tokens,
            min(timestamp) as first_ts,
            max(timestamp) as last_ts
        FROM read_parquet('{TRADES}') t
        WHERE usd_amount >= 50
        GROUP BY maker, t.condition_id, maker_direction, nonusdc_side
        HAVING sum(usd_amount) >= 100
    ) TO '{OUT}/maker_market_agg.parquet' (FORMAT PARQUET, COMPRESSION SNAPPY)
""")
cnt = con.execute(f"SELECT count(*) FROM '{OUT}/maker_market_agg.parquet'").fetchone()[0]
print(f"   Done: {cnt:,} rows, {time.time()-t0:.0f}s", flush=True)

# ── 3. Taker-market aggregates ──
print("3/5 Taker-market aggregates...", flush=True)
t0 = time.time()
con.execute(f"""
    COPY (
        SELECT
            taker as address,
            t.condition_id,
            taker_direction as direction,
            nonusdc_side,
            count(*) as trade_count,
            sum(usd_amount) as volume_usd,
            sum(token_amount) as total_tokens,
            avg(price) as avg_price,
            sum(CASE WHEN taker_direction = 'BUY' THEN usd_amount ELSE 0 END) as buy_usd,
            sum(CASE WHEN taker_direction = 'SELL' THEN usd_amount ELSE 0 END) as sell_usd,
            sum(CASE WHEN taker_direction = 'BUY' THEN token_amount ELSE 0 END) as buy_tokens,
            sum(CASE WHEN taker_direction = 'SELL' THEN token_amount ELSE 0 END) as sell_tokens,
            min(timestamp) as first_ts,
            max(timestamp) as last_ts
        FROM read_parquet('{TRADES}') t
        WHERE usd_amount >= 50
        GROUP BY taker, t.condition_id, taker_direction, nonusdc_side
        HAVING sum(usd_amount) >= 100
    ) TO '{OUT}/taker_market_agg.parquet' (FORMAT PARQUET, COMPRESSION SNAPPY)
""")
cnt = con.execute(f"SELECT count(*) FROM '{OUT}/taker_market_agg.parquet'").fetchone()[0]
print(f"   Done: {cnt:,} rows, {time.time()-t0:.0f}s", flush=True)

# ── 4. Wallet PnL (maker side — JOIN с outcomes) ──
print("4/5 Wallet PnL — maker side...", flush=True)
t0 = time.time()
con.execute(f"""
    COPY (
        SELECT
            wm.address,
            wm.condition_id,
            m.category,
            m.question,
            m.winner,
            m.answer1,
            m.answer2,
            m.is_resolved,
            wm.direction,
            wm.nonusdc_side,
            wm.trade_count,
            wm.volume_usd,
            wm.buy_usd,
            wm.sell_usd,
            wm.buy_tokens,
            wm.sell_tokens,
            wm.avg_price,
            -- Net tokens held (bought - sold)
            wm.buy_tokens - wm.sell_tokens as net_tokens,
            -- PnL calculation for resolved markets
            CASE
                WHEN NOT m.is_resolved THEN NULL
                -- Token1 (answer1) side: if winner=answer1 and we hold net_tokens > 0 → win
                WHEN wm.nonusdc_side = 'token1' AND m.winner = m.answer1
                    THEN (wm.buy_tokens - wm.sell_tokens) * 1.0 - (wm.buy_usd - wm.sell_usd)
                WHEN wm.nonusdc_side = 'token1' AND m.winner = m.answer2
                    THEN -(wm.buy_usd - wm.sell_usd)
                -- Token2 (answer2) side
                WHEN wm.nonusdc_side = 'token2' AND m.winner = m.answer2
                    THEN (wm.buy_tokens - wm.sell_tokens) * 1.0 - (wm.buy_usd - wm.sell_usd)
                WHEN wm.nonusdc_side = 'token2' AND m.winner = m.answer1
                    THEN -(wm.buy_usd - wm.sell_usd)
                ELSE NULL
            END as realized_pnl,
            CASE
                WHEN NOT m.is_resolved THEN NULL
                WHEN wm.nonusdc_side = 'token1' AND m.winner = m.answer1 THEN true
                WHEN wm.nonusdc_side = 'token2' AND m.winner = m.answer2 THEN true
                WHEN m.winner IS NOT NULL THEN false
                ELSE NULL
            END as is_win
        FROM '{OUT}/maker_market_agg.parquet' wm
        JOIN '{OUT}/market_enriched.parquet' m ON wm.condition_id = m.condition_id
        WHERE m.is_resolved = true
          AND m.winner IS NOT NULL
          AND (wm.buy_tokens - wm.sell_tokens) > 0.01
    ) TO '{OUT}/maker_pnl.parquet' (FORMAT PARQUET, COMPRESSION SNAPPY)
""")
cnt = con.execute(f"SELECT count(*) FROM '{OUT}/maker_pnl.parquet'").fetchone()[0]
wins = con.execute(f"SELECT count(*) FROM '{OUT}/maker_pnl.parquet' WHERE is_win = true").fetchone()[0]
print(f"   Done: {cnt:,} resolved positions, {wins:,} wins ({100*wins/max(cnt,1):.1f}%), {time.time()-t0:.0f}s", flush=True)

# ── 5. Wallet PnL (taker side) ──
print("5/5 Wallet PnL — taker side...", flush=True)
t0 = time.time()
con.execute(f"""
    COPY (
        SELECT
            wm.address,
            wm.condition_id,
            m.category,
            m.question,
            m.winner,
            m.answer1,
            m.answer2,
            m.is_resolved,
            wm.direction,
            wm.nonusdc_side,
            wm.trade_count,
            wm.volume_usd,
            wm.buy_usd,
            wm.sell_usd,
            wm.buy_tokens,
            wm.sell_tokens,
            wm.avg_price,
            wm.buy_tokens - wm.sell_tokens as net_tokens,
            CASE
                WHEN NOT m.is_resolved THEN NULL
                WHEN wm.nonusdc_side = 'token1' AND m.winner = m.answer1
                    THEN (wm.buy_tokens - wm.sell_tokens) * 1.0 - (wm.buy_usd - wm.sell_usd)
                WHEN wm.nonusdc_side = 'token1' AND m.winner = m.answer2
                    THEN -(wm.buy_usd - wm.sell_usd)
                WHEN wm.nonusdc_side = 'token2' AND m.winner = m.answer2
                    THEN (wm.buy_tokens - wm.sell_tokens) * 1.0 - (wm.buy_usd - wm.sell_usd)
                WHEN wm.nonusdc_side = 'token2' AND m.winner = m.answer1
                    THEN -(wm.buy_usd - wm.sell_usd)
                ELSE NULL
            END as realized_pnl,
            CASE
                WHEN NOT m.is_resolved THEN NULL
                WHEN wm.nonusdc_side = 'token1' AND m.winner = m.answer1 THEN true
                WHEN wm.nonusdc_side = 'token2' AND m.winner = m.answer2 THEN true
                WHEN m.winner IS NOT NULL THEN false
                ELSE NULL
            END as is_win
        FROM '{OUT}/taker_market_agg.parquet' wm
        JOIN '{OUT}/market_enriched.parquet' m ON wm.condition_id = m.condition_id
        WHERE m.is_resolved = true
          AND m.winner IS NOT NULL
          AND (wm.buy_tokens - wm.sell_tokens) > 0.01
    ) TO '{OUT}/taker_pnl.parquet' (FORMAT PARQUET, COMPRESSION SNAPPY)
""")
cnt = con.execute(f"SELECT count(*) FROM '{OUT}/taker_pnl.parquet'").fetchone()[0]
wins = con.execute(f"SELECT count(*) FROM '{OUT}/taker_pnl.parquet' WHERE is_win = true").fetchone()[0]
print(f"   Done: {cnt:,} resolved positions, {wins:,} wins ({100*wins/max(cnt,1):.1f}%), {time.time()-t0:.0f}s", flush=True)

con.close()
try:
    os.remove("/tmp/agg_p2.duckdb")
except OSError:
    pass

# ── Summary ──
print("\n=== ALL FILES ===", flush=True)
total = 0
for f in sorted(os.listdir(OUT)):
    fp = os.path.join(OUT, f)
    if os.path.isfile(fp):
        sz = os.path.getsize(fp) / 1024**2
        total += sz
        print(f"  {f}: {sz:.1f} MB")
print(f"\n  TOTAL: {total:.0f} MB", flush=True)
print("PHASE 2 COMPLETE", flush=True)
