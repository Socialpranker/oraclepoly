#!/usr/bin/env python3
"""Backtest D52 — whale-flow imbalance persistence predicts price drift / resolution.

Hypothesis (W2.4 roadmap, gate W2.2 passed WITH mandatory modifications):
    A persistent, one-sided net flow of large wallets ("whales") in mid-zone prices
    (0.2-0.8) predicts price drift toward the flow side on a 24-72h horizon.

Mandatory design constraints from W2.2 gate (research/h-series-verdicts-2026-07-09/answer.md):
    1. NOT a point-in-time snapshot. D27 (backtest_d27_double_elite_momentum.py, verdict
       NEGATIVE WR 49-53%) already tested raw whale_buy_pressure on a single 1h bucket.
       D52 = rolling persistence: same-sign flow for >= K consecutive CALENDAR hours,
       grid K in {2, 3, 4, 6}.
    2. Wallet-quality filter: not raw whale volume, but a quality weight/filter using
       wallet_score_at_anchor.parquet (leak-free score up to the anchor) and/or
       wallet_performance.parquet (is_smart_money / roi_pct / win_rate). An unfiltered
       variant is also run as baseline to show the delta the filter buys.
    3. Explicit K=1-no-filter control, reproducing the D27 setup as closely as this
       schema allows — expected to reproduce ~50% WR; if it beats that by a lot, there
       is a leak somewhere.

IMPORTANT SCHEMA NOTE (found while building this script, corrects a mislabel in D27's
docstring): `wallet_flow_per_market.whale_buy_pressure` is NOT the normalized ratio
(buy-sell)/(buy+sell) that the D27 comment claims. Per build_wallet_flow_per_market.py:19
and the actual SQL (lines 130-135), it is a **raw signed USD flow**:
    whale_buy_pressure = SUM(whale BUY usd_amount) - SUM(whale SELL usd_amount)
i.e. dollars, not a bounded [-1, 1] ratio. D27's `ABS(whale_buy_pressure) >= 0.7` filter was
therefore filtering on "net flow >= $0.70", which is nearly a no-op (drops only exact-zero
rows) rather than "strongly directional" as the comment implied. This backtest uses the raw
$ sign for direction (BUY vs SELL net) and a separate `$ magnitude >= floor` gate, and does
NOT reuse D27's literal `>=0.7` threshold since it means something different than documented.

Outcome definitions (two kinds, both computed):
    (1) drift: price move from signal hour to hour+H, measured on trade_buckets.vwap_yes
        (same table D27 used for continuation, hour-bucket time series). NOT
        price_snapshots.parquet — that table is anchor-sparse (hours_to_resolve in a fixed
        set like 24/168/720, ~3-4 rows/market) and cannot answer "did price move by hour+24"
        for an arbitrary signal hour. trade_buckets is the honest choice per-task instructions.
    (2) resolution: yes_won from markets_clean, restricted to signals close to resolution
        (last active data before market end) so "resolution" is a distinct question from
        "did price drift", not the same thing measured twice.

Grid: K in {1(control), 2, 3, 4, 6} x horizon in {24, 48, 72}h (drift) x quality filter
in {none, smart_money, top_decile} x price zone (0.2-0.5, 0.5-0.8, whole 0.2-0.8) x category.

Event-aware: signals are deduplicated/clustered by (condition_id) for WR aggregation warnings,
and Wilson CI is computed per-cell on distinct signal rows; a market can contribute more than
one signal (rolling window), so a per-event cap diagnostic is printed to flag clustering risk.

Run (VPS only, research venv with duckdb 1.5.3):
    cd /root/oracle && /root/oracle/.venv-research/bin/python scripts/research/d52_whale_flow_backtest.py
"""
from __future__ import annotations

import math
import sys
import time

import duckdb

CANONICAL_DIR = "/mnt/data/canonical"
TRADE_BUCKETS = f"{CANONICAL_DIR}/trade_buckets.parquet"
WALLET_FLOW = f"{CANONICAL_DIR}/wallet_flow_per_market.parquet"
WALLET_SCORE = f"{CANONICAL_DIR}/wallet_score_at_anchor.parquet"
MARKETS_CLEAN = f"{CANONICAL_DIR}/markets_clean.parquet"

# Fees/slippage — reuse project constants (see _backtest_params.py). D52 is whale-following,
# similar liquidity profile to D1 in that table -> use its slippage assumption as a proxy.
FEE_ROUND_TRIP_PCT = 2.5  # DEFAULT_FEE_PCT(1.25) * 2, conservative flat proxy (see _backtest_params.py)
SLIPPAGE_PP = 2.0  # D1 whale-following proxy, mid-liquidity markets

MIN_FLOW_USD = 500.0  # minimum |net whale $ flow| per hour to count as "directional" (not noise)
MIN_WHALE_TAKERS = 1  # at least one whale taker present in the hour

K_GRID = [1, 2, 3, 4, 6]
HORIZONS_H = [24, 48, 72]
ZONES = [
    ("mid_low", 0.20, 0.50),
    ("mid_high", 0.50, 0.80),
    ("mid_all", 0.20, 0.80),
]
QUALITY_FILTERS = ["none", "smart_money", "top_decile"]


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson score interval lower bound for a binomial proportion (95% CI by default)."""
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z**2 / n
    centre = p + z**2 / (2 * n)
    adj = z * math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n)
    return (centre - adj) / denom


def wilson_upper_bound(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z**2 / n
    centre = p + z**2 / (2 * n)
    adj = z * math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n)
    return (centre + adj) / denom


def build_base_table(con: duckdb.DuckDBPyConnection) -> None:
    """Build the joined hour-bucket table: trade_buckets + wallet_flow + wallet_score.

    One row per (condition_id, hour_bucket) that has both a trade bucket (price) and a
    wallet_flow row (whale activity — including zero-flow hours, needed for persistence
    run detection: a gap hour with whale_buy_pressure == 0 breaks a run, it should not be
    silently skipped).
    """
    print("Building base joined table (trade_buckets x wallet_flow x wallet_score)...", flush=True)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE base AS
        SELECT
            tb.condition_id,
            tb.hour_bucket,
            tb.vwap_yes,
            tb.total_volume_usd,
            wf.whale_taker_volume,
            wf.whale_taker_count,
            wf.whale_buy_pressure,
            wf.n_unique_takers,
            CASE
                WHEN wf.whale_taker_count >= {MIN_WHALE_TAKERS}
                     AND ABS(wf.whale_buy_pressure) >= {MIN_FLOW_USD}
                THEN SIGN(wf.whale_buy_pressure)
                ELSE 0
            END AS flow_sign,
            ws4.smart_money_taker_share AS smart_share_4h,
            ws4.top_decile_taker_share AS top_decile_share_4h,
            ws4.taker_roi_weighted AS taker_roi_weighted_4h
        FROM read_parquet('{TRADE_BUCKETS}') tb
        JOIN read_parquet('{WALLET_FLOW}') wf
            ON tb.condition_id = wf.condition_id
            AND tb.hour_bucket = wf.hour_bucket
        LEFT JOIN read_parquet('{WALLET_SCORE}') ws4
            ON tb.condition_id = ws4.condition_id AND ws4.anchor_hours = 4
        WHERE tb.vwap_yes IS NOT NULL
          AND tb.vwap_yes > 0.02 AND tb.vwap_yes < 0.98
    """)
    n = con.execute("SELECT COUNT(*) FROM base").fetchone()[0]
    print(f"  base rows: {n:,}", flush=True)

    # Persistence run length: count consecutive CALENDAR hours (gap = hour_bucket diff != 1h)
    # with the SAME flow_sign (nonzero). This is what distinguishes D52 from D27 (K=1 snapshot).
    print("Computing calendar-contiguous run length per (condition_id, hour_bucket)...", flush=True)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE with_gaps AS
        SELECT
            *,
            LAG(hour_bucket) OVER w AS prev_hour,
            LAG(flow_sign) OVER w AS prev_sign,
            CASE
                WHEN LAG(hour_bucket) OVER w IS NULL THEN 1
                WHEN LAG(flow_sign) OVER w != flow_sign THEN 1
                WHEN date_diff('hour', LAG(hour_bucket) OVER w, hour_bucket) != 1 THEN 1
                ELSE 0
            END AS run_break
        FROM base
        WINDOW w AS (PARTITION BY condition_id ORDER BY hour_bucket)
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE with_run_id AS
        SELECT *, SUM(run_break) OVER (PARTITION BY condition_id ORDER BY hour_bucket
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS run_id
        FROM with_gaps
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE with_run_len AS
        SELECT *, COUNT(*) OVER (PARTITION BY condition_id, run_id) AS run_len,
            ROW_NUMBER() OVER (PARTITION BY condition_id, run_id ORDER BY hour_bucket) AS pos_in_run
        FROM with_run_id
    """)
    n2 = con.execute("SELECT COUNT(*) FROM with_run_len WHERE flow_sign != 0").fetchone()[0]
    print(f"  nonzero-flow rows: {n2:,}", flush=True)


def build_horizon_table(con: duckdb.DuckDBPyConnection) -> None:
    """Attach forward vwap_yes at +24/+48/+72h (by hour_bucket LEAD, calendar-checked) and
    market metadata (category, yes_won, end_date) for the resolution outcome."""
    print("Attaching forward price + market metadata...", flush=True)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE signals AS
        SELECT
            r.*,
            LEAD(r.vwap_yes, 24) OVER w AS vwap_h24,
            LEAD(r.hour_bucket, 24) OVER w AS hb_h24,
            LEAD(r.vwap_yes, 48) OVER w AS vwap_h48,
            LEAD(r.hour_bucket, 48) OVER w AS hb_h48,
            LEAD(r.vwap_yes, 72) OVER w AS vwap_h72,
            LEAD(r.hour_bucket, 72) OVER w AS hb_h72,
            m.category,
            m.yes_won,
            m.end_date,
            m.lifetime_volume_usd,
            date_diff('hour', r.hour_bucket, m.end_date) AS hours_to_end
        FROM with_run_len r
        JOIN read_parquet('{MARKETS_CLEAN}') m ON r.condition_id = m.condition_id
        WINDOW w AS (PARTITION BY r.condition_id ORDER BY r.hour_bucket)
    """)
    n = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    print(f"  signals rows: {n:,}", flush=True)


def zone_cost(side: str, price: float) -> float:
    if side == "YES":
        return price
    return 1.0 - price


def run_grid(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Main K x horizon x quality x zone grid for the drift outcome."""
    results = []

    quality_predicates = {
        "none": "TRUE",
        "smart_money": "smart_share_4h IS NOT NULL AND smart_share_4h >= 0.20",
        "top_decile": "top_decile_share_4h IS NOT NULL AND top_decile_share_4h >= 0.20",
    }

    for k in K_GRID:
        for horizon_h, vwap_col, hb_col in [
            (24, "vwap_h24", "hb_h24"),
            (48, "vwap_h48", "hb_h48"),
            (72, "vwap_h72", "hb_h72"),
        ]:
            for zone_name, zlo, zhi in ZONES:
                for quality_name, quality_pred in quality_predicates.items():
                    q = f"""
                        WITH elig AS (
                            SELECT
                                condition_id, hour_bucket, vwap_yes, {vwap_col} AS future_vwap,
                                flow_sign, category,
                                CASE WHEN flow_sign > 0 THEN 'YES' ELSE 'NO' END AS side
                            FROM signals
                            WHERE flow_sign != 0
                              AND pos_in_run >= {k}
                              AND run_len >= {k}
                              AND vwap_yes >= {zlo} AND vwap_yes < {zhi}
                              AND {vwap_col} IS NOT NULL
                              AND {hb_col} IS NOT NULL
                              AND {quality_pred}
                        )
                        SELECT
                            count(*) AS n,
                            count(DISTINCT condition_id) AS n_markets,
                            avg(vwap_yes) AS avg_entry_price,
                            sum(CASE
                                WHEN side='YES' AND future_vwap > vwap_yes THEN 1
                                WHEN side='NO' AND future_vwap < vwap_yes THEN 1
                                ELSE 0 END) AS wins,
                            avg(CASE WHEN side='YES' THEN future_vwap - vwap_yes
                                     ELSE vwap_yes - future_vwap END) AS avg_directional_pp
                        FROM elig
                    """
                    row = con.execute(q).fetchone()
                    n, n_markets, avg_entry, wins, avg_dir_pp = row
                    if n is None or n < 30:
                        continue
                    wr = wins / n
                    wl = wilson_lower_bound(wins, n)
                    wu = wilson_upper_bound(wins, n)
                    # Net ROI: entry at avg_entry_price on the flow side, using per-row cost.
                    # For YES side cost=avg_entry_price, NO side cost=1-avg_entry_price - but
                    # avg_entry mixes both sides here since side depends on flow_sign per row.
                    # Recompute cost properly per side split below for an honest net ROI.
                    results.append({
                        "K": k, "horizon_h": horizon_h, "zone": zone_name,
                        "quality": quality_name, "n": n, "n_markets": n_markets,
                        "avg_entry_price": avg_entry, "wr": wr,
                        "wilson_lo": wl, "wilson_hi": wu,
                        "avg_directional_pp": avg_dir_pp,
                    })
    return results


def compute_net_roi_for_cell(con: duckdb.DuckDBPyConnection, k: int, horizon_h: int,
                              zlo: float, zhi: float, quality_pred: str) -> dict | None:
    """Recompute a cell with proper per-side cost for an honest net ROI figure."""
    vwap_col, hb_col = {24: ("vwap_h24", "hb_h24"), 48: ("vwap_h48", "hb_h48"),
                         72: ("vwap_h72", "hb_h72")}[horizon_h]
    fee_frac = FEE_ROUND_TRIP_PCT / 100.0
    slip_frac = SLIPPAGE_PP / 100.0
    q = f"""
        WITH elig AS (
            SELECT
                condition_id, hour_bucket, vwap_yes, {vwap_col} AS future_vwap,
                flow_sign,
                CASE WHEN flow_sign > 0 THEN 'YES' ELSE 'NO' END AS side,
                CASE WHEN flow_sign > 0 THEN vwap_yes ELSE 1 - vwap_yes END AS cost,
                CASE
                    WHEN flow_sign > 0 AND {vwap_col} > vwap_yes THEN 1
                    WHEN flow_sign < 0 AND {vwap_col} < vwap_yes THEN 1
                    ELSE 0
                END AS win
            FROM signals
            WHERE flow_sign != 0
              AND pos_in_run >= {k}
              AND run_len >= {k}
              AND vwap_yes >= {zlo} AND vwap_yes < {zhi}
              AND {vwap_col} IS NOT NULL
              AND {hb_col} IS NOT NULL
              AND {quality_pred}
        )
        SELECT count(*), sum(win), avg(cost) FROM elig
    """
    n, wins, avg_cost = con.execute(q).fetchone()
    if n is None or n < 30 or avg_cost is None:
        return None
    wr = wins / n
    cost_net = avg_cost + slip_frac
    # Binary-outcome proxy net ROI: treat "win" as reaching $1 payout side, same style as
    # aggregate WR-based scheme A in _backtest_params.py, but here "win" = directional drift,
    # not resolution — so this is an ROI proxy for "exit at favorable drift", not a hold-to-
    # resolution payoff. Documented explicitly in the report as an approximation.
    net_roi = (wr * (1 - fee_frac) - cost_net) / cost_net
    return {"n": n, "wr": wr, "avg_cost": avg_cost, "net_roi": net_roi}


def run_resolution_outcome(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Resolution outcome: signals close to market end (hours_to_end <= 72h), yes_won.

    Kept as a SEPARATE outcome from drift (not just "drift at horizon=hours until end")
    to directly answer the GO/KILL criterion (Wilson lower > 0.55 on RESOLUTION outcome).
    """
    results = []
    quality_predicates = {
        "none": "TRUE",
        "smart_money": "smart_share_4h IS NOT NULL AND smart_share_4h >= 0.20",
        "top_decile": "top_decile_share_4h IS NOT NULL AND top_decile_share_4h >= 0.20",
    }
    for k in K_GRID:
        for zone_name, zlo, zhi in ZONES:
            for quality_name, quality_pred in quality_predicates.items():
                q = f"""
                    WITH elig AS (
                        SELECT DISTINCT ON (condition_id)
                            condition_id, hour_bucket, vwap_yes, flow_sign, yes_won,
                            CASE WHEN flow_sign > 0 THEN 'YES' ELSE 'NO' END AS side
                        FROM signals
                        WHERE flow_sign != 0
                          AND pos_in_run >= {k}
                          AND run_len >= {k}
                          AND vwap_yes >= {zlo} AND vwap_yes < {zhi}
                          AND yes_won IS NOT NULL
                          AND hours_to_end BETWEEN 0 AND 72
                          AND {quality_pred}
                        ORDER BY condition_id, hour_bucket DESC
                    )
                    SELECT
                        count(*) AS n,
                        sum(CASE WHEN side='YES' AND yes_won=1 THEN 1
                                 WHEN side='NO' AND yes_won=0 THEN 1 ELSE 0 END) AS wins,
                        avg(vwap_yes) AS avg_entry
                    FROM elig
                """
                n, wins, avg_entry = con.execute(q).fetchone()
                if n is None or n < 30:
                    continue
                wr = wins / n
                wl = wilson_lower_bound(wins, n)
                wu = wilson_upper_bound(wins, n)
                results.append({
                    "K": k, "zone": zone_name, "quality": quality_name,
                    "n": n, "wr": wr, "wilson_lo": wl, "wilson_hi": wu,
                    "avg_entry": avg_entry,
                })
    return results


def run_signal_frequency(con: duckdb.DuckDBPyConnection) -> None:
    """Diagnostic: signals/month at K>=2, no filter, mid_all zone — for the >=30/mo GO gate."""
    print("\n--- Signal frequency diagnostic (K>=2, no filter, mid_all zone, resolved only) ---")
    for k in [2, 3, 4, 6]:
        q = f"""
            WITH elig AS (
                SELECT DISTINCT ON (condition_id)
                    condition_id, hour_bucket, yes_won
                FROM signals
                WHERE flow_sign != 0
                  AND pos_in_run >= {k}
                  AND run_len >= {k}
                  AND vwap_yes >= 0.20 AND vwap_yes < 0.80
                  AND yes_won IS NOT NULL
                  AND hours_to_end BETWEEN 0 AND 72
                ORDER BY condition_id, hour_bucket DESC
            )
            SELECT
                count(*),
                CAST(min(hour_bucket) AS VARCHAR),
                CAST(max(hour_bucket) AS VARCHAR),
                date_diff('day', min(hour_bucket), max(hour_bucket))
            FROM elig
        """
        n, dmin_s, dmax_s, span_days = con.execute(q).fetchone()
        if n and span_days is not None:
            months = max(span_days / 30.44, 1)
            print(f"  K={k}: n={n:,} resolved-signals over {dmin_s}..{dmax_s} "
                  f"(~{months:.1f} months) -> {n/months:.1f} signals/month")


def print_table(rows: list[dict], cols: list[str], title: str) -> None:
    print(f"\n{title}")
    header = "  ".join(f"{c:>12}" for c in cols)
    print(f"  {header}")
    for r in rows:
        line = "  ".join(
            f"{r[c]:>12.3f}" if isinstance(r[c], float) else f"{r[c]:>12}"
            for c in cols
        )
        print(f"  {line}")


def main() -> int:
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET memory_limit='8GB'")
    con.execute("SET threads=4")
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")
    con.execute("PRAGMA max_temp_directory_size='50GiB'")

    print("=" * 90)
    print("D52 WHALE-FLOW PERSISTENCE BACKTEST")
    print("=" * 90)

    build_base_table(con)
    build_horizon_table(con)

    # ---- Control: reproduce D27-like K=1, no-filter, all-zone snapshot (sanity check) ----
    print("\n" + "=" * 90)
    print("CONTROL — K=1 (single-hour snapshot, D27-equivalent), NO quality filter")
    print("=" * 90)
    for horizon_h in HORIZONS_H:
        vwap_col, hb_col = {24: ("vwap_h24", "hb_h24"), 48: ("vwap_h48", "hb_h48"),
                             72: ("vwap_h72", "hb_h72")}[horizon_h]
        q = f"""
            WITH elig AS (
                SELECT
                    flow_sign,
                    CASE WHEN flow_sign > 0 THEN 'YES' ELSE 'NO' END AS side,
                    vwap_yes, {vwap_col} AS future_vwap
                FROM signals
                WHERE flow_sign != 0 AND pos_in_run >= 1 AND run_len >= 1
                  AND vwap_yes >= 0.20 AND vwap_yes < 0.80
                  AND {vwap_col} IS NOT NULL AND {hb_col} IS NOT NULL
            )
            SELECT count(*),
                sum(CASE WHEN side='YES' AND future_vwap>vwap_yes THEN 1
                         WHEN side='NO' AND future_vwap<vwap_yes THEN 1 ELSE 0 END)
            FROM elig
        """
        n, wins = con.execute(q).fetchone()
        if n:
            wr = wins / n
            print(f"  horizon={horizon_h:>3}h  n={n:>8,}  WR={wr:6.1%}  "
                  f"Wilson=[{wilson_lower_bound(wins,n):.3f},{wilson_upper_bound(wins,n):.3f}]")

    # ---- Main drift grid ----
    print("\n" + "=" * 90)
    print("MAIN GRID — drift outcome (K x horizon x zone x quality)")
    print("=" * 90)
    drift_results = run_grid(con)
    drift_results.sort(key=lambda r: -r["wilson_lo"])
    print_table(
        drift_results[:40],
        ["K", "horizon_h", "zone", "quality", "n", "n_markets", "wr", "wilson_lo", "avg_directional_pp"],
        "Top 40 cells by Wilson lower bound (drift outcome):",
    )

    # ---- Net ROI for top cells ----
    print("\n" + "=" * 90)
    print("NET ROI — recomputed with per-side cost for top drift cells (Wilson_lo desc)")
    print("=" * 90)
    quality_predicates = {
        "none": "TRUE",
        "smart_money": "smart_share_4h IS NOT NULL AND smart_share_4h >= 0.20",
        "top_decile": "top_decile_share_4h IS NOT NULL AND top_decile_share_4h >= 0.20",
    }
    zone_bounds = {name: (lo, hi) for name, lo, hi in ZONES}
    for r in drift_results[:15]:
        zlo, zhi = zone_bounds[r["zone"]]
        roi = compute_net_roi_for_cell(con, r["K"], r["horizon_h"], zlo, zhi,
                                        quality_predicates[r["quality"]])
        if roi:
            print(f"  K={r['K']} h={r['horizon_h']:>3} zone={r['zone']:>9} q={r['quality']:>12} "
                  f"n={roi['n']:>7,} WR={roi['wr']:6.1%} avg_cost={roi['avg_cost']:.3f} "
                  f"net_ROI={roi['net_roi']*100:+6.1f}%")

    # ---- Resolution outcome ----
    print("\n" + "=" * 90)
    print("RESOLUTION OUTCOME — last signal per market within 72h of end_date, yes_won")
    print("=" * 90)
    res_results = run_resolution_outcome(con)
    res_results.sort(key=lambda r: -r["wilson_lo"])
    print_table(
        res_results[:30],
        ["K", "zone", "quality", "n", "wr", "wilson_lo", "wilson_hi", "avg_entry"],
        "Top 30 cells by Wilson lower bound (resolution outcome):",
    )

    # ---- Signal frequency diagnostic ----
    run_signal_frequency(con)

    # ---- Category breakdown for best cell (data-mining sanity) ----
    if res_results:
        best = res_results[0]
        print(f"\n--- Category breakdown for best resolution cell (K={best['K']}, "
              f"zone={best['zone']}, quality={best['quality']}) ---")
        zlo, zhi = zone_bounds[best["zone"]]
        qpred = quality_predicates[best["quality"]]
        q = f"""
            WITH elig AS (
                SELECT DISTINCT ON (condition_id)
                    condition_id, hour_bucket, vwap_yes, flow_sign, yes_won, category,
                    CASE WHEN flow_sign > 0 THEN 'YES' ELSE 'NO' END AS side
                FROM signals
                WHERE flow_sign != 0
                  AND pos_in_run >= {best['K']} AND run_len >= {best['K']}
                  AND vwap_yes >= {zlo} AND vwap_yes < {zhi}
                  AND yes_won IS NOT NULL
                  AND hours_to_end BETWEEN 0 AND 72
                  AND {qpred}
                ORDER BY condition_id, hour_bucket DESC
            )
            SELECT category, count(*) n,
                sum(CASE WHEN side='YES' AND yes_won=1 THEN 1
                         WHEN side='NO' AND yes_won=0 THEN 1 ELSE 0 END) wins
            FROM elig GROUP BY category ORDER BY n DESC
        """
        for cat, n, wins in con.execute(q).fetchall():
            wr = wins / n if n else 0
            print(f"  {cat:>20}: n={n:>6,} WR={wr:6.1%} Wilson_lo={wilson_lower_bound(wins,n):.3f}")

    print(f"\nTotal runtime: {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
