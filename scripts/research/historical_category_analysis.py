"""Анализ исторического датасета: category thresholds + VWAP deviation + optimism tax.

Запускать на VPS:
    ssh root@<YOUR_SERVER_IP>
    cd ~/oracle
    nohup python scripts/research/historical_category_analysis.py > /tmp/cat_analysis.log 2>&1 &

Результат: JSON с оптимальными thresholds per category → использовать в config.py.
"""

import json
import os

import duckdb

DATA = "/mnt/data/polymarket_dataset"
OUT = "/mnt/data/polymarket_dataset/analysis"
os.makedirs(OUT, exist_ok=True)

con = duckdb.connect()
con.execute("SET memory_limit='3GB'")
con.execute("SET threads=3")

print("=" * 60)
print("HISTORICAL CATEGORY ANALYSIS")
print("=" * 60)

# ── 1. Category distribution + resolved stats ──
print("\n1/5 Category distribution...", flush=True)
cat_stats = con.execute(f"""
    SELECT
        category,
        count(*) as total,
        count(*) FILTER (WHERE is_resolved) as resolved,
        count(*) FILTER (WHERE winner IS NOT NULL) as with_winner,
        sum(volume) FILTER (WHERE is_resolved) as resolved_volume
    FROM '{DATA}/market_enriched.parquet'
    GROUP BY category
    ORDER BY resolved DESC
""").fetchdf()
print(cat_stats.to_string(index=False))

# ── 2. VWAP deviation от fair value по категориям ──
# winner_price = 1.0 для winning token, 0.0 для losing
# vwap_deviation = vwap - winner_implied (positive = YES overpriced)
print("\n2/5 VWAP deviation analysis (optimism tax)...", flush=True)
vwap_analysis = con.execute(f"""
    WITH resolved AS (
        SELECT
            m.condition_id,
            m.category,
            m.winner,
            m.answer1,
            a.vwap,
            a.avg_price,
            a.total_volume_usd,
            a.taker_buy_volume,
            a.taker_sell_volume,
            a.whale_trade_count,
            a.whale_volume,
            a.trade_count,
            -- winner_price: 1.0 если answer1 победил (YES token), 0.0 если answer2
            CASE WHEN m.winner = m.answer1 THEN 1.0 ELSE 0.0 END as winner_price,
            -- VWAP deviation: насколько средняя цена отклонялась от итога
            a.vwap - (CASE WHEN m.winner = m.answer1 THEN 1.0 ELSE 0.0 END) as vwap_deviation,
            -- taker imbalance: (buy - sell) / total
            (a.taker_buy_volume - a.taker_sell_volume)
                / nullif(a.taker_buy_volume + a.taker_sell_volume, 0) as taker_imbalance
        FROM '{DATA}/market_enriched.parquet' m
        JOIN '{DATA}/market_aggregates.parquet' a
            ON m.condition_id = a.condition_id
        WHERE m.is_resolved = true
          AND m.winner IS NOT NULL
          AND a.vwap IS NOT NULL
          AND a.vwap > 0
          AND a.total_volume_usd >= 100
    )
    SELECT
        category,
        count(*) as n_markets,
        -- VWAP deviation: positive = market overpriced YES token
        avg(vwap_deviation) as avg_vwap_deviation,
        percentile_cont(0.25) WITHIN GROUP (ORDER BY vwap_deviation) as p25_vwap_dev,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY vwap_deviation) as median_vwap_dev,
        percentile_cont(0.75) WITHIN GROUP (ORDER BY vwap_deviation) as p75_vwap_dev,
        stddev(vwap_deviation) as std_vwap_dev,
        -- Taker imbalance корреляция с outcome
        corr(taker_imbalance, winner_price) as taker_imbalance_corr,
        -- Whale volume fraction
        avg(whale_volume / nullif(total_volume_usd, 0)) as avg_whale_fraction,
        -- Среднее число trades
        avg(trade_count) as avg_trades
    FROM resolved
    GROUP BY category
    ORDER BY n_markets DESC
""").fetchdf()
print(vwap_analysis.to_string(index=False))

# ── 3. Divergence threshold analysis per category ──
# Для каждой категории: при каком |vwap - winner_price| win_rate > 60% и PF > 1.3
print("\n3/5 Optimal min_divergence per category...", flush=True)
div_thresholds = con.execute(f"""
    WITH resolved AS (
        SELECT
            m.category,
            a.vwap,
            CASE WHEN m.winner = m.answer1 THEN 1.0 ELSE 0.0 END as winner_price,
            abs(a.vwap - (CASE WHEN m.winner = m.answer1 THEN 1.0 ELSE 0.0 END)) as abs_vwap_dev,
            -- Simulated bet: если vwap < 0.5 → ставим NO (winner_price=0), иначе YES (winner_price=1)
            CASE
                WHEN a.vwap < 0.5 AND m.winner = m.answer2 THEN 1  -- bet NO, NO won
                WHEN a.vwap >= 0.5 AND m.winner = m.answer1 THEN 1 -- bet YES, YES won
                ELSE 0
            END as naive_win
        FROM '{DATA}/market_enriched.parquet' m
        JOIN '{DATA}/market_aggregates.parquet' a
            ON m.condition_id = a.condition_id
        WHERE m.is_resolved = true
          AND m.winner IS NOT NULL
          AND a.vwap IS NOT NULL
          AND a.vwap BETWEEN 0.05 AND 0.95
          AND a.total_volume_usd >= 100
    ),
    thresholds AS (
        SELECT unnest([0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.15, 0.20]) as threshold
    )
    SELECT
        r.category,
        t.threshold,
        count(*) as n_trades,
        avg(r.naive_win) * 100 as win_rate_pct,
        -- Profit factor approximation: avg_win / avg_loss
        -- При ставке на VWAP-implied side: win pays (1-vwap)/vwap, loss pays -1
        sum(CASE WHEN r.naive_win = 1 THEN 1.0 / nullif(r.abs_vwap_dev, 0) ELSE 0 END)
            / nullif(sum(CASE WHEN r.naive_win = 0 THEN 1 ELSE 0 END), 0) as profit_factor_approx
    FROM resolved r
    CROSS JOIN thresholds t
    WHERE r.abs_vwap_dev >= t.threshold
    GROUP BY r.category, t.threshold
    HAVING count(*) >= 30
    ORDER BY r.category, t.threshold
""").fetchdf()
print(div_thresholds.to_string(index=False))

# ── 4. Optimism tax: YES overpricing по price zones ──
print("\n4/5 Optimism tax by price zone...", flush=True)
optimism_tax = con.execute(f"""
    WITH resolved AS (
        SELECT
            m.category,
            a.vwap,
            CASE WHEN m.winner = m.answer1 THEN 1.0 ELSE 0.0 END as winner_price,
            -- Price zone
            CASE
                WHEN a.vwap < 0.10 THEN '01-10'
                WHEN a.vwap < 0.20 THEN '10-20'
                WHEN a.vwap < 0.30 THEN '20-30'
                WHEN a.vwap < 0.40 THEN '30-40'
                WHEN a.vwap < 0.50 THEN '40-50'
                WHEN a.vwap < 0.60 THEN '50-60'
                WHEN a.vwap < 0.70 THEN '60-70'
                WHEN a.vwap < 0.80 THEN '70-80'
                WHEN a.vwap < 0.90 THEN '80-90'
                ELSE '90-99'
            END as price_zone
        FROM '{DATA}/market_enriched.parquet' m
        JOIN '{DATA}/market_aggregates.parquet' a
            ON m.condition_id = a.condition_id
        WHERE m.is_resolved = true
          AND m.winner IS NOT NULL
          AND a.vwap IS NOT NULL
          AND a.vwap BETWEEN 0.01 AND 0.99
          AND a.total_volume_usd >= 100
    )
    SELECT
        price_zone,
        count(*) as n_markets,
        avg(vwap) as avg_vwap,
        avg(winner_price) as actual_win_rate,
        avg(vwap) - avg(winner_price) as optimism_bias,
        -- Positive = market overpriced this zone
        CASE
            WHEN avg(vwap) - avg(winner_price) > 0.02 THEN 'OVERPRICED'
            WHEN avg(vwap) - avg(winner_price) < -0.02 THEN 'UNDERPRICED'
            ELSE 'FAIR'
        END as verdict
    FROM resolved
    GROUP BY price_zone
    ORDER BY price_zone
""").fetchdf()
print(optimism_tax.to_string(index=False))

# ── 5. Taker imbalance как предиктор outcome по категориям ──
print("\n5/5 Taker imbalance predictive power...", flush=True)
taker_analysis = con.execute(f"""
    WITH resolved AS (
        SELECT
            m.category,
            a.taker_buy_volume,
            a.taker_sell_volume,
            (a.taker_buy_volume - a.taker_sell_volume)
                / nullif(a.taker_buy_volume + a.taker_sell_volume, 0) as taker_imbalance,
            CASE WHEN m.winner = m.answer1 THEN 1.0 ELSE 0.0 END as winner_price,
            a.total_volume_usd
        FROM '{DATA}/market_enriched.parquet' m
        JOIN '{DATA}/market_aggregates.parquet' a
            ON m.condition_id = a.condition_id
        WHERE m.is_resolved = true
          AND m.winner IS NOT NULL
          AND a.taker_buy_volume IS NOT NULL
          AND a.taker_sell_volume IS NOT NULL
          AND (a.taker_buy_volume + a.taker_sell_volume) > 100
    )
    SELECT
        category,
        count(*) as n_markets,
        corr(taker_imbalance, winner_price) as imbalance_outcome_corr,
        -- Направленность: если imbalance > 0 (больше покупок), чаще ли YES побеждает?
        avg(winner_price) FILTER (WHERE taker_imbalance > 0.1) as wr_when_buy_dominant,
        avg(winner_price) FILTER (WHERE taker_imbalance < -0.1) as wr_when_sell_dominant,
        avg(winner_price) FILTER (WHERE abs(taker_imbalance) <= 0.1) as wr_when_balanced
    FROM resolved
    GROUP BY category
    HAVING count(*) >= 50
    ORDER BY abs(corr(taker_imbalance, winner_price)) DESC
""").fetchdf()
print(taker_analysis.to_string(index=False))

# ── Export results as JSON ──
results = {
    "category_stats": cat_stats.to_dict(orient="records"),
    "vwap_analysis": vwap_analysis.to_dict(orient="records"),
    "divergence_thresholds": div_thresholds.to_dict(orient="records"),
    "optimism_tax": optimism_tax.to_dict(orient="records"),
    "taker_analysis": taker_analysis.to_dict(orient="records"),
}

out_path = f"{OUT}/category_analysis.json"
with open(out_path, "w") as f:
    json.dump(results, f, indent=2, default=str)
print(f"\nResults saved to {out_path}")

con.close()
print("\nDONE", flush=True)
