"""Backfill whale profiles и smart money registry из исторических агрегатов.

Шаги:
1. DuckDB: агрегировать maker_pnl + taker_pnl → per-wallet stats (wins, losses, ROI, sector WR)
2. SSH → prod PostgreSQL: UPDATE whales для existing, INSERT для новых
3. SSH → prod PostgreSQL: INSERT smart_money_wallets для consistently profitable wallets
"""

import json

import duckdb

OUT = "/mnt/data/polymarket_dataset"
con = duckdb.connect()

# ── 1. Wallet-level stats из PnL файлов ──
print("1/4 Computing wallet stats from PnL data...", flush=True)

wallet_stats = con.execute(f"""
    WITH combined AS (
        SELECT address, category, is_win, realized_pnl, volume_usd, trade_count
        FROM '{OUT}/maker_pnl.parquet'
        UNION ALL
        SELECT address, category, is_win, realized_pnl, volume_usd, trade_count
        FROM '{OUT}/taker_pnl.parquet'
    ),
    per_wallet AS (
        SELECT
            address,
            count(*) as total_positions,
            sum(CASE WHEN is_win THEN 1 ELSE 0 END) as wins,
            sum(CASE WHEN NOT is_win THEN 1 ELSE 0 END) as losses,
            sum(realized_pnl) as total_pnl,
            sum(volume_usd) as total_invested,
            sum(trade_count) as total_trades,
            avg(volume_usd / nullif(trade_count, 0)) as avg_trade_size,
            max(volume_usd) as max_position_size
        FROM combined
        WHERE is_win IS NOT NULL
        GROUP BY address
        HAVING count(*) >= 10
    )
    SELECT
        address,
        total_positions,
        wins,
        losses,
        total_pnl,
        total_invested,
        total_trades,
        avg_trade_size,
        max_position_size,
        CASE WHEN total_invested > 0 THEN (total_pnl / total_invested) * 100 ELSE 0 END as roi_percent,
        wins * 1.0 / nullif(wins + losses, 0) as win_rate,
        CASE
            WHEN avg_trade_size >= 1000000 THEN 'leviathan'
            WHEN avg_trade_size >= 250000 THEN 'orca'
            WHEN avg_trade_size >= 100000 THEN 'whale'
            WHEN avg_trade_size >= 50000 THEN 'shark'
            WHEN avg_trade_size >= 20000 THEN 'dolphin'
            WHEN avg_trade_size >= 10000 THEN 'fish'
            WHEN avg_trade_size >= 7500 THEN 'crab'
            ELSE 'shrimp'
        END as whale_tier
    FROM per_wallet
    ORDER BY total_pnl DESC
""").fetchall()

print(f"   {len(wallet_stats):,} wallets with >=10 resolved positions", flush=True)

# ── 2. Sector win rates per wallet ──
print("2/4 Computing sector win rates...", flush=True)

sector_data = con.execute(f"""
    WITH combined AS (
        SELECT address, category, is_win
        FROM '{OUT}/maker_pnl.parquet'
        WHERE is_win IS NOT NULL AND category != 'Other' AND category != 'Minute Markets'
        UNION ALL
        SELECT address, category, is_win
        FROM '{OUT}/taker_pnl.parquet'
        WHERE is_win IS NOT NULL AND category != 'Other' AND category != 'Minute Markets'
    )
    SELECT
        address,
        category,
        sum(CASE WHEN is_win THEN 1 ELSE 0 END) as wins,
        sum(CASE WHEN NOT is_win THEN 1 ELSE 0 END) as losses
    FROM combined
    GROUP BY address, category
    HAVING count(*) >= 5
""").fetchall()

# Build dict: address -> {category: {wins, losses}}
sector_map = {}
for addr, cat, w, l in sector_data:
    if addr not in sector_map:
        sector_map[addr] = {}
    sector_map[addr][cat] = {"wins": w, "losses": l}

print(f"   {len(sector_map):,} wallets with sector data", flush=True)

# ── 3. Smart money candidates ──
print("3/4 Identifying smart money candidates...", flush=True)

smart_money = []
for row in wallet_stats:
    addr = row[0]
    wins = row[2]
    losses = row[3]
    total_pnl = row[4]
    roi = row[9]
    wr = row[10]
    total_positions = row[1]

    # Criteria: ROI > 15%, WR >= 55%, >= 30 resolved positions, PnL > $1K
    if roi and roi > 15 and wr and wr >= 0.55 and total_positions >= 30 and total_pnl and total_pnl > 1000:
        # Determine specialization from sector data
        specs = []
        if addr in sector_map:
            for cat, data in sector_map[addr].items():
                cat_wr = data["wins"] / max(data["wins"] + data["losses"], 1)
                if cat_wr >= 0.60 and (data["wins"] + data["losses"]) >= 10:
                    specs.append(cat.lower().replace(" & ", "_").replace(" ", "_"))

        smart_money.append({
            "address": addr,
            "total_pnl": float(total_pnl),
            "roi": float(roi),
            "win_rate": float(wr),
            "positions": int(total_positions),
            "specialization": specs or ["general"],
        })

print(f"   {len(smart_money):,} smart money candidates (ROI>15%, WR>=55%, >=30 pos, PnL>$1K)", flush=True)

# Top 20 preview
print("\n   Top 20 by PnL:", flush=True)
for sm in smart_money[:20]:
    print(
        f"     {sm['address'][:12]}... PnL=${sm['total_pnl']:>12,.0f} "
        f"ROI={sm['roi']:>6.1f}% WR={sm['win_rate']:.1%} "
        f"pos={sm['positions']:>4} spec={sm['specialization']}"
    )

# ── 4. Generate SQL ──
print("\n4/4 Generating SQL...", flush=True)

sql_lines = []

# 4a. UPDATE existing whales
sql_lines.append("-- Whale backfill from historical dataset (2022-2026)")
sql_lines.append("-- Generated by scripts/research/backfill_whales.py")
sql_lines.append("")

update_count = 0
for row in wallet_stats[:5000]:  # Top 5000 by PnL
    addr = row[0]
    wins = int(row[2])
    losses = int(row[3])
    total_pnl = float(row[4]) if row[4] else 0
    total_invested = float(row[5]) if row[5] else 0
    total_trades = int(row[6]) if row[6] else 0
    avg_trade = float(row[7]) if row[7] else 0
    max_pos = float(row[8]) if row[8] else 0
    roi = float(row[9]) if row[9] else 0
    wr = float(row[10]) if row[10] else 0
    tier = row[11]

    sector_wr = json.dumps(sector_map.get(addr, {}))

    # Only update if our historical data has MORE data than what's in the DB
    sql_lines.append(f"""
UPDATE whales SET
    wins = GREATEST(wins, {wins}),
    losses = GREATEST(losses, {losses}),
    pnl_usd = CASE WHEN {wins + losses} > COALESCE(wins + losses, 0) THEN {total_pnl:.2f} ELSE pnl_usd END,
    roi_percent = CASE WHEN {wins + losses} > COALESCE(wins + losses, 0) THEN {roi:.4f} ELSE roi_percent END,
    total_invested = GREATEST(COALESCE(total_invested, 0), {total_invested:.2f}),
    bet_count = GREATEST(COALESCE(bet_count, 0), {total_trades}),
    avg_bet_size = CASE WHEN {total_trades} > COALESCE(bet_count, 0) THEN {avg_trade:.2f} ELSE avg_bet_size END,
    max_bet_size = GREATEST(COALESCE(max_bet_size, 0), {max_pos:.2f}),
    whale_tier = CASE WHEN {wins + losses} > COALESCE(wins + losses, 0) THEN '{tier}' ELSE whale_tier END,
    win_rate = CASE WHEN {wins + losses} > COALESCE(wins + losses, 0) THEN {wr:.4f} ELSE win_rate END,
    sector_win_rates = CASE WHEN {wins + losses} > COALESCE(wins + losses, 0) THEN '{sector_wr}'::jsonb ELSE sector_win_rates END,
    metrics_updated_at = NOW()
WHERE address = '{addr}';""")
    update_count += 1

# 4b. INSERT smart_money_wallets (ON CONFLICT skip)
sql_lines.append("\n-- Smart Money Registry Expansion")
insert_count = 0
for sm in smart_money[:500]:  # Top 500
    addr = sm["address"]
    pnl = sm["total_pnl"]
    specs = sm["specialization"]
    specs_sql = "ARRAY[" + ",".join(f"'{s}'" for s in specs) + "]"
    label = f"hist_roi{sm['roi']:.0f}_wr{sm['win_rate']:.0%}"

    sql_lines.append(f"""
INSERT INTO smart_money_wallets (address, label, source, specialization, total_pnl, is_active)
VALUES ('{addr}', '{label}', 'historical_backfill', {specs_sql}, {pnl:.2f}, true)
ON CONFLICT (address) DO UPDATE SET
    total_pnl = GREATEST(smart_money_wallets.total_pnl, EXCLUDED.total_pnl),
    specialization = EXCLUDED.specialization,
    last_synced_at = NOW();""")
    insert_count += 1

# Write SQL file
sql_path = "/mnt/data/polymarket_dataset/backfill.sql"
with open(sql_path, "w") as f:
    f.write("\n".join(sql_lines))

print(f"   SQL written to {sql_path}")
print(f"   {update_count:,} whale UPDATEs")
print(f"   {insert_count:,} smart_money INSERTs")

# File size
import os

sz = os.path.getsize(sql_path) / 1024 / 1024
print(f"   File size: {sz:.1f} MB")

print("\nTo apply:")
print(f"  cat {sql_path} | docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly")
print("\nDONE")
