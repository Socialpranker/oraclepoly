#!/usr/bin/env python3
"""G-series alchemy batch: smart_money_taker_share × outcomes на canonical.

10 гипотез, все используют wallet_score_at_anchor:
  G1 smart_money_share > 0.5 → outcome predicts
  G2 top_decile_share > 0.6 → WR
  G3 low smart share <0.2 + retail heavy → mispricing
  G4 first_trade recency (skipped — нужен per-wallet)
  G5 share growth pattern
  G6 top_decile_share × zone → direction by pnl_weighted sign
  G7 negative roi_weighted + continued buying → fade
  G8 smart volume divergence across anchors
  G9 concentration × n_takers interaction
  G10 pnl × roi same sign signal
"""
from __future__ import annotations
import duckdb, sys, time

CANONICAL = "/mnt/data/canonical"

def main():
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.execute("SET threads=8")

    # Snapshot at 168h (1 week before resolution) — самый информативный anchor
    # Берём только markets с known yes_won
    con.execute(f"""
      CREATE OR REPLACE TEMP TABLE base AS
      SELECT
        m.condition_id, m.category, m.yes_won, m.lifetime_volume_usd,
        ws.anchor_hours,
        ws.n_takers_24h_before,
        ws.taker_volume_usd_total,
        ws.smart_money_taker_volume_usd,
        ws.top_decile_taker_volume_usd,
        ws.taker_roi_weighted,
        ws.taker_pnl_weighted_usd,
        ws.smart_money_taker_share,
        ws.top_decile_taker_share,
        ps.yes_price
      FROM read_parquet('{CANONICAL}/markets_clean.parquet') m
      JOIN read_parquet('{CANONICAL}/wallet_score_at_anchor.parquet') ws
        ON ws.condition_id = m.condition_id
      LEFT JOIN read_parquet('{CANONICAL}/price_snapshots.parquet') ps
        ON ps.condition_id = m.condition_id AND ABS(ps.hours_to_resolve - ws.anchor_hours) < 4
      WHERE m.yes_won IS NOT NULL
        AND m.lifetime_volume_usd >= 1000
        AND ws.anchor_hours IN (4, 24, 168, 720)
        AND ws.n_takers_24h_before >= 10
        AND ps.yes_price IS NOT NULL
        AND ps.yes_price > 0.02 AND ps.yes_price < 0.98
    """)
    n = con.execute("SELECT count(*) FROM base").fetchone()[0]
    print(f"base set: {n:,}")
    print()

    FEE = 1.25  # mixed average

    # =================================================================
    # G1: smart_money_taker_share > threshold + direction of their buy
    # =================================================================
    print("=" * 90)
    print("G1: smart_money_taker_share predicts outcome (per anchor × share threshold)")
    print("=" * 90)
    print(f"{'anchor':>8} {'share_min':>10} {'n':>5} {'WR_yes':>7} {'avg_p':>7} {'edge_pp':>8} {'side_ROI':>12}")
    for anchor in [4, 24, 168, 720]:
        for share_min in [0.3, 0.5, 0.7]:
            row = con.execute(f"""
              SELECT count(*),
                     avg(yes_price)::DOUBLE AS avg_p,
                     avg(yes_won)::DOUBLE AS wr,
                     (avg(yes_won)::DOUBLE - avg(yes_price)::DOUBLE) * 100.0 AS edge,
                     -- best side ROI: если WR > price → YES else NO
                     CASE
                       WHEN avg(yes_won)::DOUBLE > avg(yes_price)::DOUBLE
                         THEN (avg(yes_won)::DOUBLE / NULLIF(avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                       ELSE ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                     END AS roi
              FROM base
              WHERE anchor_hours = {anchor}
                AND smart_money_taker_share >= {share_min}
            """).fetchone()
            n_r, avg_p, wr, edge, roi = row
            if n_r is None or n_r < 200: continue
            net = roi - 2*FEE
            side = "YES" if wr > avg_p else "NO"
            marker = " STAR" if abs(edge) >= 3 and n_r >= 500 and net >= 8 else ""
            print(f"  {anchor:>6}h {share_min:>10.2f} {n_r:>5} {wr:>7.3f} {avg_p:>7.3f} {edge:>+8.2f} {side}_{net:>+8.2f}%{marker}")

    # =================================================================
    # G2: top_decile_taker_share > threshold
    # =================================================================
    print()
    print("=" * 90)
    print("G2: top_decile_taker_share predicts outcome")
    print("=" * 90)
    print(f"{'anchor':>8} {'share_min':>10} {'n':>5} {'WR_yes':>7} {'avg_p':>7} {'edge_pp':>8} {'side_ROI':>12}")
    for anchor in [4, 24, 168, 720]:
        for share_min in [0.3, 0.5, 0.7]:
            row = con.execute(f"""
              SELECT count(*), avg(yes_price)::DOUBLE, avg(yes_won)::DOUBLE,
                     (avg(yes_won)::DOUBLE - avg(yes_price)::DOUBLE) * 100.0,
                     CASE
                       WHEN avg(yes_won)::DOUBLE > avg(yes_price)::DOUBLE
                         THEN (avg(yes_won)::DOUBLE / NULLIF(avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                       ELSE ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                     END
              FROM base
              WHERE anchor_hours = {anchor}
                AND top_decile_taker_share >= {share_min}
            """).fetchone()
            n_r, avg_p, wr, edge, roi = row
            if n_r is None or n_r < 200: continue
            net = roi - 2*FEE
            side = "YES" if wr > avg_p else "NO"
            marker = " STAR" if abs(edge) >= 3 and n_r >= 500 and net >= 8 else ""
            print(f"  {anchor:>6}h {share_min:>10.2f} {n_r:>5} {wr:>7.3f} {avg_p:>7.3f} {edge:>+8.2f} {side}_{net:>+8.2f}%{marker}")

    # =================================================================
    # G3: low_smart < 0.2 → retail-dominated mispricing
    # =================================================================
    print()
    print("=" * 90)
    print("G3: low smart_money_share (<0.2) + zone → retail mispricing")
    print("=" * 90)
    print(f"{'anchor':>8} {'zone':>10} {'n':>5} {'WR_yes':>7} {'avg_p':>7} {'edge_pp':>8} {'side_ROI':>12}")
    for anchor in [24, 168]:
        for zlo, zhi in [(0.05, 0.20), (0.20, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 0.95)]:
            row = con.execute(f"""
              SELECT count(*), avg(yes_price)::DOUBLE, avg(yes_won)::DOUBLE,
                     (avg(yes_won)::DOUBLE - avg(yes_price)::DOUBLE) * 100.0,
                     CASE
                       WHEN avg(yes_won)::DOUBLE > avg(yes_price)::DOUBLE
                         THEN (avg(yes_won)::DOUBLE / NULLIF(avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                       ELSE ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                     END
              FROM base
              WHERE anchor_hours = {anchor}
                AND smart_money_taker_share < 0.2
                AND yes_price >= {zlo} AND yes_price < {zhi}
            """).fetchone()
            n_r, avg_p, wr, edge, roi = row
            if n_r is None or n_r < 100: continue
            net = roi - 2*FEE
            side = "YES" if wr > avg_p else "NO"
            marker = " STAR" if abs(edge) >= 3 and n_r >= 200 and net >= 8 else ""
            print(f"  {anchor:>6}h [{zlo:.2f},{zhi:.2f}) {n_r:>5} {wr:>7.3f} {avg_p:>7.3f} {edge:>+8.2f} {side}_{net:>+8.2f}%{marker}")

    # =================================================================
    # G6: top_decile + zone + direction by pnl_weighted
    # =================================================================
    print()
    print("=" * 90)
    print("G6: top_decile > 0.6 + zone + direction by sign(taker_pnl_weighted)")
    print("=" * 90)
    print(f"{'anchor':>8} {'zone':>10} {'pnl_pos':>8} {'n':>5} {'WR_yes':>7} {'edge_pp':>8} {'side_ROI':>12}")
    for anchor in [24, 168]:
        for zlo, zhi in [(0.20, 0.50), (0.50, 0.80)]:
            for pnl_sign in ['pos', 'neg']:
                cond = "taker_pnl_weighted_usd > 0" if pnl_sign == 'pos' else "taker_pnl_weighted_usd < 0"
                row = con.execute(f"""
                  SELECT count(*), avg(yes_price)::DOUBLE, avg(yes_won)::DOUBLE,
                         (avg(yes_won)::DOUBLE - avg(yes_price)::DOUBLE) * 100.0,
                         CASE
                           WHEN avg(yes_won)::DOUBLE > avg(yes_price)::DOUBLE
                             THEN (avg(yes_won)::DOUBLE / NULLIF(avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                           ELSE ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                         END
                  FROM base
                  WHERE anchor_hours = {anchor}
                    AND top_decile_taker_share >= 0.6
                    AND yes_price >= {zlo} AND yes_price < {zhi}
                    AND {cond}
                """).fetchone()
                n_r, avg_p, wr, edge, roi = row
                if n_r is None or n_r < 100: continue
                net = roi - 2*FEE
                side = "YES" if wr > avg_p else "NO"
                marker = " STAR" if abs(edge) >= 5 and n_r >= 200 and net >= 10 else ""
                print(f"  {anchor:>6}h [{zlo:.2f},{zhi:.2f}) {pnl_sign:>8} {n_r:>5} {wr:>7.3f} {edge:>+8.2f} {side}_{net:>+8.2f}%{marker}")

    # =================================================================
    # G7: lossy smart money continuing to buy → fade them
    # =================================================================
    print()
    print("=" * 90)
    print("G7: smart money losing (roi < 0) but buying → fade their direction")
    print("=" * 90)
    print(f"{'anchor':>8} {'condition':>22} {'n':>5} {'WR_yes':>7} {'edge_pp':>8} {'side_ROI':>12}")
    for anchor in [24, 168]:
        # Когда taker_roi_weighted < -10% и smart_share > 0.3 → buying overpriced
        row = con.execute(f"""
          SELECT count(*), avg(yes_price)::DOUBLE, avg(yes_won)::DOUBLE,
                 (avg(yes_won)::DOUBLE - avg(yes_price)::DOUBLE) * 100.0,
                 CASE
                   WHEN avg(yes_won)::DOUBLE > avg(yes_price)::DOUBLE
                     THEN (avg(yes_won)::DOUBLE / NULLIF(avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                   ELSE ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                 END
          FROM base
          WHERE anchor_hours = {anchor}
            AND taker_roi_weighted < -0.10
            AND smart_money_taker_share > 0.3
        """).fetchone()
        n_r, avg_p, wr, edge, roi = row
        if n_r is None or n_r < 100: continue
        net = roi - 2*FEE
        side = "YES" if wr > avg_p else "NO"
        print(f"  {anchor:>6}h {'roi<-10%+share>30%':>22} {n_r:>5} {wr:>7.3f} {edge:>+8.2f} {side}_{net:>+8.2f}%")

        # Same но losing > 20%
        row = con.execute(f"""
          SELECT count(*), avg(yes_price)::DOUBLE, avg(yes_won)::DOUBLE,
                 (avg(yes_won)::DOUBLE - avg(yes_price)::DOUBLE) * 100.0,
                 CASE
                   WHEN avg(yes_won)::DOUBLE > avg(yes_price)::DOUBLE
                     THEN (avg(yes_won)::DOUBLE / NULLIF(avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                   ELSE ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                 END
          FROM base
          WHERE anchor_hours = {anchor}
            AND taker_roi_weighted < -0.20
            AND smart_money_taker_share > 0.3
        """).fetchone()
        n_r, avg_p, wr, edge, roi = row
        if n_r is None or n_r < 100: continue
        net = roi - 2*FEE
        side = "YES" if wr > avg_p else "NO"
        marker = " STAR" if abs(edge) >= 5 and net >= 10 else ""
        print(f"  {anchor:>6}h {'roi<-20%+share>30%':>22} {n_r:>5} {wr:>7.3f} {edge:>+8.2f} {side}_{net:>+8.2f}%{marker}")

    # =================================================================
    # G10: pnl × roi same sign concentration
    # =================================================================
    print()
    print("=" * 90)
    print("G10: pnl_weighted_usd × roi_weighted same sign → strong signal")
    print("=" * 90)
    print(f"{'anchor':>8} {'sign':>12} {'n':>5} {'WR_yes':>7} {'edge_pp':>8} {'side_ROI':>12}")
    for anchor in [24, 168]:
        for sign_lbl, cond in [('both_positive', 'taker_pnl_weighted_usd > 100 AND taker_roi_weighted > 0.05'),
                                ('both_negative', 'taker_pnl_weighted_usd < -100 AND taker_roi_weighted < -0.05')]:
            row = con.execute(f"""
              SELECT count(*), avg(yes_price)::DOUBLE, avg(yes_won)::DOUBLE,
                     (avg(yes_won)::DOUBLE - avg(yes_price)::DOUBLE) * 100.0,
                     CASE
                       WHEN avg(yes_won)::DOUBLE > avg(yes_price)::DOUBLE
                         THEN (avg(yes_won)::DOUBLE / NULLIF(avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                       ELSE ((1.0 - avg(yes_won)::DOUBLE) / NULLIF(1.0 - avg(yes_price)::DOUBLE, 0) - 1) * 100.0
                     END
              FROM base
              WHERE anchor_hours = {anchor} AND {cond}
            """).fetchone()
            n_r, avg_p, wr, edge, roi = row
            if n_r is None or n_r < 100: continue
            net = roi - 2*FEE
            side = "YES" if wr > avg_p else "NO"
            marker = " STAR" if abs(edge) >= 5 and net >= 10 else ""
            print(f"  {anchor:>6}h {sign_lbl:>12} {n_r:>5} {wr:>7.3f} {edge:>+8.2f} {side}_{net:>+8.2f}%{marker}")

    print(f"\nElapsed: {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
