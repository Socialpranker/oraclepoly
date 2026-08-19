"""Research: Maker vs Taker edge profile на Polymarket.

Исследование различий прибыльности между makers (limit orders) и takers (market orders).

Запуск: cd ~/oracle && python scripts/research/research_maker_vs_taker.py
"""

import os
import time

import duckdb

D = "/mnt/data/polymarket_dataset"
OUT = "/mnt/data/polymarket_dataset/analysis"
os.makedirs(OUT, exist_ok=True)


def main():
    print("=" * 70)
    print("RESEARCH: Maker vs Taker Edge Profile")
    print("=" * 70)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'")
    con.execute("SET threads=3")
    con.execute("SET temp_directory='/tmp/duckdb'")
    os.makedirs("/tmp/duckdb", exist_ok=True)

    # ── Build unified dataset ──
    print("\n0. Building dataset (test period 2025-H1)...", flush=True)

    for role in ["maker", "taker"]:
        con.execute("""
            CREATE OR REPLACE TABLE %s_pos AS
            SELECT
                p.address, p.condition_id, p.is_win, p.avg_price,
                p.realized_pnl, p.volume_usd, p.category,
                CASE WHEN p.nonusdc_side = 'token1' THEN 'YES' ELSE 'NO' END as side,
                CASE WHEN p.nonusdc_side = 'token1' THEN p.avg_price ELSE 1.0 - p.avg_price END as eff_entry,
                '%s' as role,
                a.total_volume_usd as mkt_vol,
                a.taker_buy_volume, a.taker_sell_volume,
                a.whale_trade_count, a.price_stddev,
                m.end_date, m.created_at as mkt_created
            FROM '%s/%s_pnl.parquet' p
            JOIN '%s/market_aggregates.parquet' a ON p.condition_id = a.condition_id
            JOIN '%s/market_enriched.parquet' m ON p.condition_id = m.condition_id
            WHERE p.is_resolved AND p.is_win IS NOT NULL
              AND p.avg_price BETWEEN 0.01 AND 0.99
              AND m.end_date >= '2025-01-01' AND m.end_date < '2025-07-01'
              AND a.total_volume_usd >= 100
        """ % (role, role, D, role, D, D))

    mk_n = con.execute("SELECT count(*) FROM maker_pos").fetchone()[0]
    tk_n = con.execute("SELECT count(*) FROM taker_pos").fetchone()[0]
    print("   maker: %s, taker: %s" % (format(mk_n, ","), format(tk_n, ",")))

    # ═══════════════════════════════════════════════════════════════
    # 1. БАЗОВОЕ СРАВНЕНИЕ
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("1. БАЗОВОЕ СРАВНЕНИЕ: Maker vs Taker")
    print("=" * 70)

    for side_val in ["NO", "YES"]:
        print("\n  --- %s side ---" % side_val)
        print("  %-8s %8s %6s %14s %8s %8s %8s %8s" % (
            "Role", "Trades", "WR", "Total PnL", "EV/$", "Median", "P25", "P75"))
        for role in ["maker", "taker"]:
            r = con.execute("""
                SELECT
                    count(*) as n,
                    round(100.0 * avg(CASE WHEN is_win THEN 1 ELSE 0 END), 1),
                    round(sum(realized_pnl)::numeric, 0),
                    round(avg(realized_pnl / nullif(volume_usd, 0))::numeric, 4),
                    round(percentile_cont(0.50) WITHIN GROUP (ORDER BY realized_pnl / nullif(volume_usd, 0))::numeric, 4),
                    round(percentile_cont(0.25) WITHIN GROUP (ORDER BY realized_pnl / nullif(volume_usd, 0))::numeric, 4),
                    round(percentile_cont(0.75) WITHIN GROUP (ORDER BY realized_pnl / nullif(volume_usd, 0))::numeric, 4)
                FROM %s_pos WHERE side = '%s'
            """ % (role, side_val)).fetchone()
            print("  %-8s %8s %5.1f%% %14s %8.4f %8.4f %8.4f %8.4f" % (
                role, format(r[0], ","), r[1], "$"+format(r[2], ","), r[3], r[4], r[5], r[6]))

    # По категориям
    print("\n  --- NO side by Category ---")
    print("  %-15s %8s %8s %8s %8s %8s %8s" % (
        "Category", "Mk EV/$", "Mk Trades", "Tk EV/$", "Tk Trades", "Δ EV/$", "Winner"))
    cats = con.execute("SELECT DISTINCT category FROM maker_pos ORDER BY category").fetchall()
    for (cat,) in cats:
        mk = con.execute("""
            SELECT round(avg(realized_pnl / nullif(volume_usd, 0))::numeric, 4), count(*)
            FROM maker_pos WHERE side = 'NO' AND category = '%s'
        """ % cat).fetchone()
        tk = con.execute("""
            SELECT round(avg(realized_pnl / nullif(volume_usd, 0))::numeric, 4), count(*)
            FROM taker_pos WHERE side = 'NO' AND category = '%s'
        """ % cat).fetchone()
        if mk[1] < 50 or tk[1] < 50:
            continue
        delta = (mk[0] or 0) - (tk[0] or 0)
        winner = "MAKER" if delta > 0 else "TAKER"
        print("  %-15s %8.4f %8s %8.4f %8s %+8.4f %8s" % (
            cat, mk[0] or 0, format(mk[1], ","), tk[0] or 0, format(tk[1], ","), delta, winner))

    # По entry price zone
    print("\n  --- NO side by Entry Price Zone ---")
    print("  %-10s %8s %8s %8s %8s %8s" % ("Zone", "Mk EV/$", "Mk N", "Tk EV/$", "Tk N", "Winner"))
    for lo, hi, label in [(0, 0.10, "0-10¢"), (0.10, 0.25, "10-25¢"), (0.25, 0.40, "25-40¢"), (0.40, 1.0, "40¢+")]:
        mk = con.execute("""
            SELECT round(avg(realized_pnl / nullif(volume_usd, 0))::numeric, 4), count(*)
            FROM maker_pos WHERE side = 'NO' AND eff_entry >= %s AND eff_entry < %s
        """ % (lo, hi)).fetchone()
        tk = con.execute("""
            SELECT round(avg(realized_pnl / nullif(volume_usd, 0))::numeric, 4), count(*)
            FROM taker_pos WHERE side = 'NO' AND eff_entry >= %s AND eff_entry < %s
        """ % (lo, hi)).fetchone()
        if (mk[1] or 0) < 50 or (tk[1] or 0) < 50:
            continue
        winner = "MAKER" if (mk[0] or 0) > (tk[0] or 0) else "TAKER"
        print("  %-10s %8.4f %8s %8.4f %8s %8s" % (
            label, mk[0] or 0, format(mk[1], ","), tk[0] or 0, format(tk[1], ","), winner))

    # ═══════════════════════════════════════════════════════════════
    # 2. ОДИН И ТОТ ЖЕ MARKET: maker vs taker
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("2. ОДИН MARKET: если maker зарабатывает, taker теряет?")
    print("=" * 70)

    r = con.execute("""
        WITH mk AS (
            SELECT condition_id, avg(realized_pnl / nullif(volume_usd, 0)) as mk_ev
            FROM maker_pos WHERE side = 'NO' GROUP BY 1
        ),
        tk AS (
            SELECT condition_id, avg(realized_pnl / nullif(volume_usd, 0)) as tk_ev
            FROM taker_pos WHERE side = 'NO' GROUP BY 1
        )
        SELECT
            CASE WHEN mk_ev > 0 AND tk_ev > 0 THEN 'both_profit'
                 WHEN mk_ev > 0 AND tk_ev <= 0 THEN 'maker_only'
                 WHEN mk_ev <= 0 AND tk_ev > 0 THEN 'taker_only'
                 ELSE 'both_lose' END as outcome,
            count(*) as markets,
            round(avg(mk_ev)::numeric, 4) as avg_mk_ev,
            round(avg(tk_ev)::numeric, 4) as avg_tk_ev
        FROM mk JOIN tk USING (condition_id)
        GROUP BY 1 ORDER BY markets DESC
    """).fetchall()
    print("  %-15s %8s %10s %10s" % ("Outcome", "Markets", "Maker EV/$", "Taker EV/$"))
    for row in r:
        print("  %-15s %8s %10.4f %10.4f" % (row[0], format(row[1], ","), row[2], row[3]))

    # ═══════════════════════════════════════════════════════════════
    # 3. TIMING: makers входят раньше или позже?
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("3. TIMING: кто входит раньше?")
    print("=" * 70)

    r = con.execute("""
        WITH mk_timing AS (
            SELECT condition_id, min(first_ts) as mk_first, avg(first_ts) as mk_avg
            FROM '%s/maker_market_agg.parquet' GROUP BY 1
        ),
        tk_timing AS (
            SELECT condition_id, min(first_ts) as tk_first, avg(first_ts) as tk_avg
            FROM '%s/taker_market_agg.parquet' GROUP BY 1
        )
        SELECT
            count(*) as markets,
            round(avg(CASE WHEN mk_first < tk_first THEN 1.0 ELSE 0.0 END) * 100, 1) as maker_first_pct,
            round(avg((tk_first - mk_first) / 3600.0)::numeric, 1) as avg_hours_diff,
            round(percentile_cont(0.50) WITHIN GROUP (ORDER BY (tk_first - mk_first) / 3600.0)::numeric, 1) as median_hours_diff
        FROM mk_timing JOIN tk_timing USING (condition_id)
    """ % (D, D)).fetchone()
    print("  Markets with both maker+taker: %s" % format(r[0], ","))
    print("  Maker enters first: %.1f%% of markets" % r[1])
    print("  Avg maker leads by: %.1f hours" % r[2])
    print("  Median lead: %.1f hours" % r[3])

    # Maker early → is that market more profitable?
    r = con.execute("""
        WITH timing AS (
            SELECT
                mk.condition_id,
                mk.first_ts as mk_first,
                tk.first_ts as tk_first,
                (mk.first_ts - tk.first_ts) / 3600.0 as maker_lead_hours
            FROM (SELECT condition_id, min(first_ts) as first_ts FROM '%s/maker_market_agg.parquet' GROUP BY 1) mk
            JOIN (SELECT condition_id, min(first_ts) as first_ts FROM '%s/taker_market_agg.parquet' GROUP BY 1) tk
            USING (condition_id)
        )
        SELECT
            CASE
                WHEN maker_lead_hours < -24 THEN 'maker >24h early'
                WHEN maker_lead_hours < -1 THEN 'maker 1-24h early'
                WHEN maker_lead_hours < 1 THEN 'simultaneous'
                WHEN maker_lead_hours < 24 THEN 'taker 1-24h early'
                ELSE 'taker >24h early'
            END as who_first,
            count(*) as markets,
            round(avg(p.realized_pnl / nullif(p.volume_usd, 0))::numeric, 4) as taker_ev
        FROM timing t
        JOIN '%s/taker_pnl.parquet' p ON t.condition_id = p.condition_id
        JOIN '%s/market_enriched.parquet' m ON t.condition_id = m.condition_id
        WHERE p.is_resolved AND p.is_win IS NOT NULL AND p.nonusdc_side = 'token2'
          AND m.end_date >= '2025-01-01' AND m.end_date < '2025-07-01'
        GROUP BY 1 ORDER BY taker_ev DESC
    """ % (D, D, D, D)).fetchall()
    print("\n  Timing lead → Taker NO EV/$:")
    for row in r:
        print("    %-25s %8s markets, taker EV/$=%+.4f" % (row[0], format(row[1], ","), row[2]))

    # ═══════════════════════════════════════════════════════════════
    # 4. MAKER DIRECTION AS SIGNAL
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("4. MAKER DIRECTION: предиктивный ли maker net direction?")
    print("=" * 70)

    r = con.execute("""
        WITH maker_dir AS (
            SELECT
                condition_id,
                sum(CASE WHEN nonusdc_side = 'token1' THEN volume_usd ELSE 0 END) as maker_yes_vol,
                sum(CASE WHEN nonusdc_side = 'token2' THEN volume_usd ELSE 0 END) as maker_no_vol,
                sum(volume_usd) as maker_total
            FROM '%s/maker_market_agg.parquet'
            GROUP BY 1
            HAVING sum(volume_usd) >= 100
        )
        SELECT
            CASE
                WHEN maker_no_vol / nullif(maker_total, 0) > 0.70 THEN 'maker_strong_NO (>70%%)'
                WHEN maker_no_vol / nullif(maker_total, 0) > 0.55 THEN 'maker_lean_NO (55-70%%)'
                WHEN maker_yes_vol / nullif(maker_total, 0) > 0.70 THEN 'maker_strong_YES (>70%%)'
                WHEN maker_yes_vol / nullif(maker_total, 0) > 0.55 THEN 'maker_lean_YES (55-70%%)'
                ELSE 'maker_balanced'
            END as maker_direction,
            count(*) as markets,
            -- Does maker direction predict outcome?
            round(100.0 * avg(CASE WHEN m.winner = m.answer2 THEN 1.0 ELSE 0.0 END), 1) as no_win_rate,
            -- Taker NO side EV when following maker signal
            round(avg(CASE WHEN p.nonusdc_side = 'token2' THEN p.realized_pnl / nullif(p.volume_usd, 0) END)::numeric, 4) as taker_no_ev
        FROM maker_dir d
        JOIN '%s/market_enriched.parquet' m ON d.condition_id = m.condition_id
        LEFT JOIN '%s/taker_pnl.parquet' p ON d.condition_id = p.condition_id AND p.is_resolved
        WHERE m.is_resolved AND m.winner IS NOT NULL
          AND m.end_date >= '2025-01-01' AND m.end_date < '2025-07-01'
        GROUP BY 1
        ORDER BY taker_no_ev DESC
    """ % (D, D, D)).fetchall()
    print("  %-30s %8s %8s %10s" % ("Maker Direction", "Markets", "NO wins", "Taker EV/$"))
    for row in r:
        print("  %-30s %8s %7.1f%% %10.4f" % (row[0], format(row[1], ","), row[2], row[3] or 0))

    # ═══════════════════════════════════════════════════════════════
    # 5. SIZE: large maker vs small taker
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("5. POSITION SIZE: кто зарабатывает больше?")
    print("=" * 70)

    for role in ["maker", "taker"]:
        print("\n  --- %s (NO side) ---" % role)
        print("  %-16s %8s %6s %8s" % ("Size", "Trades", "WR", "EV/$"))
        for lo, hi, label in [(10, 100, "$10-100"), (100, 500, "$100-500"), (500, 2000, "$500-2K"),
                               (2000, 10000, "$2K-10K"), (10000, 1e9, "$10K+")]:
            r = con.execute("""
                SELECT count(*),
                       round(100.0 * avg(CASE WHEN is_win THEN 1 ELSE 0 END), 1),
                       round(avg(realized_pnl / nullif(volume_usd, 0))::numeric, 4)
                FROM %s_pos WHERE side = 'NO' AND volume_usd >= %s AND volume_usd < %s
            """ % (role, lo, hi)).fetchone()
            if (r[0] or 0) >= 50:
                print("  %-16s %8s %5.1f%% %8.4f" % (label, format(r[0], ","), r[1], r[2]))

    # ═══════════════════════════════════════════════════════════════
    # 6. EXECUTION MODE: GTC vs FOK impact
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("6. ПРИМЕНИМОСТЬ: наш бот использует GTC (maker) и FOK (taker)")
    print("=" * 70)

    # Для рынков где наш бот реально ставит (NO, div 5-15%, no whale)
    print("\n  Simulation: 'наш типичный trade' — NO, entry 15-40¢")
    for role in ["maker", "taker"]:
        r = con.execute("""
            SELECT count(*),
                   round(100.0 * avg(CASE WHEN is_win THEN 1 ELSE 0 END), 1),
                   round(avg(realized_pnl / nullif(volume_usd, 0))::numeric, 4),
                   round(sum(realized_pnl)::numeric, 0)
            FROM %s_pos WHERE side = 'NO' AND eff_entry BETWEEN 0.15 AND 0.40
        """ % role).fetchone()
        print("    %s: %s trades, WR=%.1f%%, EV/$=%.4f, total PnL=$%s" % (
            role, format(r[0], ","), r[1], r[2], format(r[3], ",")))

    print("\n  By category (NO, entry 15-40¢):")
    print("  %-15s %8s %8s %8s %8s %8s" % ("Category", "Mk EV/$", "Mk N", "Tk EV/$", "Tk N", "Better"))
    for (cat,) in cats:
        mk = con.execute("""
            SELECT round(avg(realized_pnl / nullif(volume_usd, 0))::numeric, 4), count(*)
            FROM maker_pos WHERE side = 'NO' AND eff_entry BETWEEN 0.15 AND 0.40 AND category = '%s'
        """ % cat).fetchone()
        tk = con.execute("""
            SELECT round(avg(realized_pnl / nullif(volume_usd, 0))::numeric, 4), count(*)
            FROM taker_pos WHERE side = 'NO' AND eff_entry BETWEEN 0.15 AND 0.40 AND category = '%s'
        """ % cat).fetchone()
        if (mk[1] or 0) < 30 or (tk[1] or 0) < 30:
            continue
        better = "MAKER" if (mk[0] or 0) > (tk[0] or 0) else "TAKER"
        print("  %-15s %8.4f %8s %8.4f %8s %8s" % (
            cat, mk[0] or 0, format(mk[1], ","), tk[0] or 0, format(tk[1], ","), better))

    # Save
    print("\n\nTotal time: %ds" % (time.time() - t0))


if __name__ == "__main__":
    main()
