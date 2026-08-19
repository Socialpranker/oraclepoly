"""Walk-forward smart money registry из исторического датасета.

Запускать на VPS:
    ssh root@<YOUR_SERVER_IP>
    cd ~/oracle
    nohup python scripts/research/historical_smart_money_registry.py > /tmp/sm_registry.log 2>&1 &

Pipeline:
    1. UNION maker_pnl + taker_pnl → 8.3M позиций
    2. Train (2022-2024): per-wallet stats → filter (WR>60%, trades>=50, months>=4, PnL>$1K)
    3. Validate (2025H1): проверить OOS performance
    4. Test (2025H2-2026): final check
    5. Export: validated_smart_money.parquet

Результат: список кошельков, прошедших walk-forward validation.
"""

import json
import os
import time

import duckdb

DATA = "/mnt/data/polymarket_dataset"
OUT = "/mnt/data/polymarket_dataset/analysis"
os.makedirs(OUT, exist_ok=True)

con = duckdb.connect("/tmp/sm_registry.duckdb")
con.execute("SET memory_limit='3GB'")
con.execute("SET threads=3")
con.execute("SET temp_directory='/tmp/duckdb'")
os.makedirs("/tmp/duckdb", exist_ok=True)

print("=" * 60)
print("SMART MONEY WALK-FORWARD REGISTRY")
print("=" * 60)

# ── 1. Unified PnL view: maker + taker + market dates (via market_enriched JOIN) ──
print("\n1/6 Building unified PnL view...", flush=True)
t0 = time.time()
con.execute(f"""
    CREATE OR REPLACE TABLE all_pnl AS
    SELECT
        p.address, p.condition_id, p.category, p.winner, p.answer1, p.answer2,
        p.is_resolved, p.direction, p.nonusdc_side, p.trade_count, p.volume_usd,
        p.buy_usd, p.sell_usd, p.buy_tokens, p.sell_tokens, p.avg_price,
        p.realized_pnl, p.is_win,
        m.end_date as market_end_date,
        m.created_at as market_created_at,
        'maker' as role
    FROM '{DATA}/maker_pnl.parquet' p
    LEFT JOIN '{DATA}/market_enriched.parquet' m ON p.condition_id = m.condition_id
    WHERE p.is_resolved = true AND p.realized_pnl IS NOT NULL
    UNION ALL
    SELECT
        p.address, p.condition_id, p.category, p.winner, p.answer1, p.answer2,
        p.is_resolved, p.direction, p.nonusdc_side, p.trade_count, p.volume_usd,
        p.buy_usd, p.sell_usd, p.buy_tokens, p.sell_tokens, p.avg_price,
        p.realized_pnl, p.is_win,
        m.end_date as market_end_date,
        m.created_at as market_created_at,
        'taker' as role
    FROM '{DATA}/taker_pnl.parquet' p
    LEFT JOIN '{DATA}/market_enriched.parquet' m ON p.condition_id = m.condition_id
    WHERE p.is_resolved = true AND p.realized_pnl IS NOT NULL
""")
total = con.execute("SELECT count(*) FROM all_pnl").fetchone()[0]
print(f"   Unified: {total:,} positions, {time.time()-t0:.0f}s", flush=True)

# ── 2. Train period (до 2025-01-01): per-wallet stats ──
print("\n2/6 Train period stats (< 2025-01-01)...", flush=True)
t0 = time.time()
con.execute("""
    CREATE OR REPLACE TABLE train_wallets AS
    SELECT
        address,
        count(*) as total_positions,
        sum(CASE WHEN is_win THEN 1 ELSE 0 END) as wins,
        sum(CASE WHEN NOT is_win THEN 1 ELSE 0 END) as losses,
        sum(CASE WHEN is_win THEN 1 ELSE 0 END) * 100.0 / count(*) as win_rate,
        sum(realized_pnl) as total_pnl,
        sum(volume_usd) as total_volume,
        avg(realized_pnl) as avg_pnl_per_trade,
        sum(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END)
            / nullif(-sum(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 END), 0) as profit_factor,
        count(DISTINCT category) as categories_traded,
        -- Months active (по датам рынков)
        (extract(epoch FROM max(market_end_date)::timestamp - min(market_created_at)::timestamp)) / (30*86400) as months_active,
        min(market_created_at) as first_trade,
        max(market_end_date) as last_trade
    FROM all_pnl
    WHERE market_end_date < '2025-01-01'
    GROUP BY address
    HAVING count(*) >= 50
""")
train_total = con.execute("SELECT count(*) FROM train_wallets").fetchone()[0]
print(f"   Wallets with >=50 trades before 2025: {train_total:,}, {time.time()-t0:.0f}s", flush=True)

# ── 3. Filter: strict smart money criteria ──
print("\n3/6 Filtering smart money candidates...", flush=True)
con.execute("""
    CREATE OR REPLACE TABLE sm_candidates AS
    SELECT *
    FROM train_wallets
    WHERE win_rate >= 60.0
      AND total_pnl >= 1000.0
      AND months_active >= 4.0
      AND profit_factor >= 1.3
""")
candidates = con.execute("SELECT count(*) FROM sm_candidates").fetchone()[0]
print(f"   Candidates (WR>=60%, PnL>=$1K, months>=4, PF>=1.3): {candidates:,}", flush=True)

# Preview top candidates
print("\n   Top 10 by PnL:", flush=True)
top = con.execute("""
    SELECT address, total_positions, win_rate, total_pnl, profit_factor, months_active
    FROM sm_candidates
    ORDER BY total_pnl DESC
    LIMIT 10
""").fetchdf()
print(top.to_string(index=False))

# ── 4. Validate period (2025H1): OOS performance ──
print("\n4/6 Validation period (2025-01-01 to 2025-07-01)...", flush=True)
t0 = time.time()
con.execute("""
    CREATE OR REPLACE TABLE validate_performance AS
    SELECT
        p.address,
        count(*) as val_positions,
        sum(CASE WHEN p.is_win THEN 1 ELSE 0 END) as val_wins,
        sum(CASE WHEN p.is_win THEN 1 ELSE 0 END) * 100.0 / count(*) as val_win_rate,
        sum(p.realized_pnl) as val_pnl,
        sum(CASE WHEN p.realized_pnl > 0 THEN p.realized_pnl ELSE 0 END)
            / nullif(-sum(CASE WHEN p.realized_pnl < 0 THEN p.realized_pnl ELSE 0 END), 0) as val_pf
    FROM all_pnl p
    JOIN sm_candidates c ON p.address = c.address
    WHERE p.market_end_date >= '2025-01-01' AND p.market_end_date < '2025-07-01'
    GROUP BY p.address
    HAVING count(*) >= 10
""")
validated = con.execute("SELECT count(*) FROM validate_performance WHERE val_win_rate >= 55 AND val_pnl > 0").fetchone()[0]
val_total = con.execute("SELECT count(*) FROM validate_performance").fetchone()[0]
print(f"   Active in validation: {val_total:,}, passed (WR>=55%, PnL>0): {validated:,}, {time.time()-t0:.0f}s", flush=True)

# ── 5. Test period (2025H2+): final check ──
print("\n5/6 Test period (>= 2025-07-01)...", flush=True)
t0 = time.time()
con.execute("""
    CREATE OR REPLACE TABLE test_performance AS
    SELECT
        p.address,
        count(*) as test_positions,
        sum(CASE WHEN p.is_win THEN 1 ELSE 0 END) as test_wins,
        sum(CASE WHEN p.is_win THEN 1 ELSE 0 END) * 100.0 / count(*) as test_win_rate,
        sum(p.realized_pnl) as test_pnl,
        sum(CASE WHEN p.realized_pnl > 0 THEN p.realized_pnl ELSE 0 END)
            / nullif(-sum(CASE WHEN p.realized_pnl < 0 THEN p.realized_pnl ELSE 0 END), 0) as test_pf
    FROM all_pnl p
    JOIN validate_performance v ON p.address = v.address
    WHERE v.val_win_rate >= 55 AND v.val_pnl > 0
      AND p.market_end_date >= '2025-07-01'
    GROUP BY p.address
    HAVING count(*) >= 5
""")
tested = con.execute("SELECT count(*) FROM test_performance WHERE test_pnl > 0").fetchone()[0]
test_total = con.execute("SELECT count(*) FROM test_performance").fetchone()[0]
print(f"   Active in test: {test_total:,}, profitable: {tested:,}, {time.time()-t0:.0f}s", flush=True)

# ── 6. Final registry: sector win rates ──
print("\n6/6 Building final registry with sector win rates...", flush=True)
t0 = time.time()

# Sector win rates для validated wallets
con.execute("""
    CREATE OR REPLACE TABLE sector_stats AS
    SELECT
        p.address,
        p.category,
        count(*) as sector_positions,
        sum(CASE WHEN p.is_win THEN 1 ELSE 0 END) * 100.0 / count(*) as sector_win_rate,
        sum(p.realized_pnl) as sector_pnl
    FROM all_pnl p
    WHERE p.address IN (
        SELECT address FROM validate_performance WHERE val_win_rate >= 55 AND val_pnl > 0
    )
    GROUP BY p.address, p.category
    HAVING count(*) >= 5
""")

# Export: validated registry
con.execute(f"""
    COPY (
        SELECT
            c.address,
            c.total_positions as train_positions,
            c.win_rate as train_wr,
            c.total_pnl as train_pnl,
            c.profit_factor as train_pf,
            c.months_active,
            c.categories_traded,
            v.val_positions,
            v.val_win_rate,
            v.val_pnl,
            v.val_pf,
            t.test_positions,
            t.test_win_rate,
            t.test_pnl,
            t.test_pf,
            -- Tier: по совокупности train + validation + test
            CASE
                WHEN c.total_pnl >= 100000 AND v.val_pnl >= 10000 THEN 'leviathan'
                WHEN c.total_pnl >= 50000 AND v.val_pnl >= 5000 THEN 'orca'
                WHEN c.total_pnl >= 10000 AND v.val_pnl >= 1000 THEN 'whale'
                WHEN c.total_pnl >= 1000 THEN 'shark'
                ELSE 'dolphin'
            END as tier
        FROM sm_candidates c
        JOIN validate_performance v ON c.address = v.address
        LEFT JOIN test_performance t ON c.address = t.address
        WHERE v.val_win_rate >= 55 AND v.val_pnl > 0
        ORDER BY c.total_pnl DESC
    ) TO '{OUT}/validated_smart_money.parquet' (FORMAT PARQUET, COMPRESSION SNAPPY)
""")

# Export sector stats
con.execute(f"""
    COPY (SELECT * FROM sector_stats ORDER BY address, category)
    TO '{OUT}/smart_money_sectors.parquet' (FORMAT PARQUET, COMPRESSION SNAPPY)
""")

# Summary stats
registry_count = con.execute(f"SELECT count(*) FROM '{OUT}/validated_smart_money.parquet'").fetchone()[0]
sector_count = con.execute(f"SELECT count(*) FROM '{OUT}/smart_money_sectors.parquet'").fetchone()[0]

print(f"\n   Registry: {registry_count:,} wallets", flush=True)
print(f"   Sector stats: {sector_count:,} records", flush=True)

# Tier distribution
tiers = con.execute(f"""
    SELECT tier, count(*) as cnt
    FROM '{OUT}/validated_smart_money.parquet'
    GROUP BY tier ORDER BY cnt DESC
""").fetchdf()
print("\n   Tier distribution:", flush=True)
print(tiers.to_string(index=False))

# Walk-forward funnel
summary = {
    "funnel": {
        "total_positions": int(total),
        "wallets_50plus_trades_pre2025": int(train_total),
        "candidates_strict_filter": int(candidates),
        "validated_2025h1": int(validated),
        "tested_2025h2": int(tested),
        "final_registry": int(registry_count),
    },
    "files": {
        "registry": f"{OUT}/validated_smart_money.parquet",
        "sectors": f"{OUT}/smart_money_sectors.parquet",
    },
}

summary_path = f"{OUT}/smart_money_summary.json"
with open(summary_path, "w") as f:
    json.dump(summary, f, indent=2)
print(f"\n   Summary: {summary_path}", flush=True)

con.close()
try:
    os.remove("/tmp/sm_registry.duckdb")
except OSError:
    pass

print(f"\n{'='*60}")
print("WALK-FORWARD FUNNEL:")
print(f"  {total:,} positions → {train_total:,} wallets (50+) → {candidates:,} candidates")
print(f"  → {validated:,} validated (2025H1) → {tested:,} tested (2025H2+)")
print(f"  → {registry_count:,} final registry")
print(f"{'='*60}")
print("DONE", flush=True)
