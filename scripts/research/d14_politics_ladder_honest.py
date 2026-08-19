#!/usr/bin/env python3
"""D14 PoliticsLadderHonest — Politics-only NO favorite-fade с строгими фильтрами.

Гипотеза (из ресёрча entry_filters_research_2026-05-02 + d7b_zone_split_test):
  Politics — самая глубокая liquidity на Polymarket ($811K avg per PANews) +
  стабильный favorite-longshot bias (Whelan Kalshi paper). Если применить:
    - Узкая zone [0.87, 0.93] (избегает HFT thick + retail thin extremes)
    - Politics-only (исключаем Crypto/Tech&AI/military)
    - exclude neg_risk (MM-floor от арб-ботов)
    - 12-48h sweet spot (4-12h adverse selection, >48h мало edge)

  → Получим detector с самым стабильным edge и наименьшим variance из всех
  candidates Top-5 deep research.

Expected (из ресёрча, до empirical validation):
  - ROI +40-60% raw
  - WR 22-28%
  - n_events/day 3-5
  - Daily realistic @ $5K = $30-50

Stage workflow (lifecycle_canonical):
  Stage Backtest → Stage Shadow → Stage Virtual → Stage Live

  Promote criteria:
    - Per-event ROI ≥ +30% с slippage 3pp
    - n_events ≥ 200
    - Per-anchor positive
    - Per-3-month rolling positive

Скрипт: cd ~/oracle && /root/oracle/.venv-research/bin/python \\
            scripts/research/d14_politics_ladder_honest.py
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import duckdb

# === Constants ==============================================================

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"

# D14 specific filters
CATEGORIES = ["Politics"]  # only Politics
EXCLUDE_NEG_RISK = True
ZONE_LO = 0.87  # narrow zone (vs D7b 0.85-0.97)
ZONE_HI = 0.93
ANCHORS_HOURS = [12, 24, 48]  # sweet spot (no 4h/72h)
VOL_FLOOR_USD = 200_000  # higher than D7b $5K — Politics has deep liquidity

# Excluded keywords в question (military / insider markets)
EXCLUDE_KEYWORDS = [
    "missile", "strike", "ukraine", "russia", "iran", "war", "attack",
    "military", "weapon", "drone",
]

# Stage thresholds
ROI_THRESHOLD = 0.30  # +30% net ROI per-bucket
N_THRESHOLD_EVENTS = 200
N_THRESHOLD_TRADES = 500

# Execution sim
LIVE_BANKROLL = 5_000.0
FRACTIONAL_KELLY = 0.20
TOTAL_EDGE_THRESHOLD = 7.0
MAX_STAKE_PCT = 0.02
DEFAULT_FEE_PCT = 1.5
SLIPPAGE_3PP = 0.03  # full-price slippage approximation для honest projection
HONEST_HAIRCUT = 0.5  # Standard для D7b-class detectors

# Time window для projection (canonical V2 dataset spans 821 days)
N_DAYS_DATASET = 821


def stage1_collect(con) -> dict:
    """Stage 1: collect raw signals + per-anchor + per-zone WR."""
    print("\n" + "=" * 70)
    print("STAGE 1: D14 Raw collection")
    print(f"  Filter: Politics only, exclude neg_risk, [{ZONE_LO}, {ZONE_HI}], "
          f"vol>=${VOL_FLOOR_USD:,}, anchors {ANCHORS_HOURS}")
    print("=" * 70)

    cats_str = "', '".join(CATEGORIES)
    anchors_str = ",".join(str(h) for h in ANCHORS_HOURS)
    excl_kw_filter = " AND ".join(
        f"LOWER(m.question) NOT LIKE '%{kw}%'" for kw in EXCLUDE_KEYWORDS
    )

    df = con.execute(f"""
        WITH signals AS (
            SELECT
                m.condition_id,
                m.event_id,
                m.category,
                m.question,
                m.lifetime_volume_usd,
                m.yes_won,
                m.neg_risk,
                s.yes_price,
                s.hours_to_resolve,
                ROW_NUMBER() OVER (
                    PARTITION BY m.condition_id ORDER BY s.hours_to_resolve
                ) AS rn
            FROM read_parquet('{MARKETS}') m
            JOIN read_parquet('{SNAPS}') s ON s.condition_id = m.condition_id
            WHERE s.hours_to_resolve IN ({anchors_str})
              AND s.yes_price BETWEEN {ZONE_LO} AND {ZONE_HI}
              AND m.yes_won IS NOT NULL
              AND m.lifetime_volume_usd >= {VOL_FLOOR_USD}
              AND m.category IN ('{cats_str}')
              AND ({"m.neg_risk = 0" if EXCLUDE_NEG_RISK else "TRUE"})
              AND {excl_kw_filter}
        )
        SELECT condition_id, event_id, category, lifetime_volume_usd,
               yes_won, yes_price, hours_to_resolve, neg_risk, question
        FROM signals
        WHERE rn = 1
        ORDER BY condition_id
    """).df()

    print(f"  Total signals: {len(df):,}")
    if len(df) == 0:
        return {"n": 0}

    print(f"  Unique events: {df['event_id'].nunique():,}")
    print(f"  Median lifetime_volume: ${df['lifetime_volume_usd'].median():,.0f}")

    # Per-anchor WR
    print(f"\n  Per-anchor breakdown:")
    print(f"  {'anchor':>7} {'n':>6} {'WR':>6} {'avg_yes':>8} {'med_vol':>10}")
    print("  " + "-" * 45)
    by_anchor = []
    for anchor in ANCHORS_HOURS:
        sub = df[df["hours_to_resolve"] == anchor]
        if len(sub) == 0:
            continue
        no_wins = (sub["yes_won"] == 0).sum()
        wr = no_wins / len(sub)
        avg_yes = sub["yes_price"].mean()
        med_vol = sub["lifetime_volume_usd"].median()
        by_anchor.append({
            "anchor": anchor, "n": len(sub), "wr": wr,
            "avg_yes": avg_yes, "med_vol": med_vol,
        })
        print(f"  {anchor:>5}h {len(sub):>6,} {wr:>5.1%} {avg_yes:>7.3f} ${med_vol:>8,.0f}")

    return {"n": len(df), "df": df, "by_anchor": by_anchor}


def stage2_economics(stage1: dict) -> dict:
    """Stage 2: Apply slippage + economics. honest_haircut проекция."""
    print("\n" + "=" * 70)
    print("STAGE 2: Economics with slippage sensitivity")
    print("=" * 70)

    if stage1["n"] == 0:
        print("  No signals — abort.")
        return {"verdict": "INSUFFICIENT_DATA"}

    df = stage1["df"].copy()
    df["cost_no"] = 1.0 - df["yes_price"]
    df["payout"] = (df["yes_won"] == 0).astype(int)

    # Stake calculation via fractional Kelly
    stake_usd = LIVE_BANKROLL * MAX_STAKE_PCT  # $100 cap

    # Sensitivity sweep по slippage
    sensitivity_rows = []
    print(f"\n  Slippage sensitivity (stake=${stake_usd:.0f}):")
    print(f"  {'slip_pp':>8} {'avg_cost':>9} {'WR':>6} {'avg_ROI':>8} {'net_PnL':>9} "
          f"{'daily':>7} {'daily_real':>11}")
    print("  " + "-" * 65)
    for slip_pp in [0, 2, 3, 5, 8, 10]:
        cost_real = (df["cost_no"] + slip_pp / 100.0).clip(0.01, 0.99)
        avg_cost = cost_real.mean()
        roi_per_trade = ((df["payout"] / cost_real) - 1.0)
        wr = float(df["payout"].mean())
        avg_roi = float(roi_per_trade.mean())
        # net PnL per trade = stake × (payout/cost - 1) - fee
        fee_drag = stake_usd * DEFAULT_FEE_PCT / 100.0
        net_pnl_per_trade = stake_usd * roi_per_trade - fee_drag
        total_pnl = net_pnl_per_trade.sum()
        daily_pnl = total_pnl / N_DAYS_DATASET
        daily_realistic = daily_pnl * HONEST_HAIRCUT
        sensitivity_rows.append({
            "slip_pp": slip_pp, "avg_cost": avg_cost, "wr": wr,
            "avg_roi": avg_roi, "net_pnl": total_pnl,
            "daily_raw": daily_pnl, "daily_realistic": daily_realistic,
        })
        print(f"  {slip_pp:>6}pp ${avg_cost:>7.4f} {wr:>5.1%} "
              f"{avg_roi:>+7.1%} ${total_pnl:>+8,.0f} ${daily_pnl:>+6.2f} "
              f"${daily_realistic:>+10.2f}")

    return {
        "n_trades": len(df),
        "n_events": df["event_id"].nunique(),
        "trades_per_day": len(df) / N_DAYS_DATASET,
        "sensitivity": sensitivity_rows,
    }


def stage3_per_event(stage1: dict) -> dict:
    """Stage 3: per-event aggregation (n_events vs n_trades для honest n)."""
    print("\n" + "=" * 70)
    print("STAGE 3: Per-event aggregation (honest n)")
    print("=" * 70)

    if stage1["n"] == 0:
        return {}

    df = stage1["df"].copy()
    df["cost_no"] = 1.0 - df["yes_price"]
    df["payout"] = (df["yes_won"] == 0).astype(int)
    df["roi"] = (df["payout"] / df["cost_no"]) - 1.0

    # Per-event aggregation (multiple markets per event coalesce to one)
    by_event = df.groupby("event_id").agg(
        n_markets=("condition_id", "count"),
        avg_yes=("yes_price", "mean"),
        avg_roi=("roi", "mean"),
        any_win=("payout", "max"),
    ).reset_index()

    n_events = len(by_event)
    avg_event_roi = by_event["avg_roi"].mean()
    event_wr = by_event["any_win"].mean()

    print(f"  n_unique_events: {n_events:,}")
    print(f"  n_trades (per-market): {stage1['n']:,}")
    print(f"  avg markets per event: {by_event['n_markets'].mean():.2f}")
    print(f"  per-event avg_ROI: {avg_event_roi:+.1%}")
    print(f"  per-event WR (any market won as NO): {event_wr:.1%}")

    return {
        "n_events": n_events,
        "per_event_avg_roi": avg_event_roi,
        "per_event_wr": event_wr,
        "avg_markets_per_event": by_event["n_markets"].mean(),
    }


def stage4_verdict(stage1: dict, stage2: dict, stage3: dict) -> dict:
    """Stage 4: PASS/FAIL verdict + Stage Shadow recommendation."""
    print("\n" + "=" * 70)
    print("STAGE 4: VERDICT")
    print("=" * 70)

    if stage1["n"] == 0:
        print("  ❌ FAIL — no signals")
        return {"verdict": "FAIL_NO_SIGNALS"}

    # Use 3pp slippage row для primary verdict
    sens_3pp = next(r for r in stage2["sensitivity"] if r["slip_pp"] == 3)
    daily_realistic_3pp = sens_3pp["daily_realistic"]

    # Promote criteria
    criteria = {
        "n_events_>= {N_THRESHOLD_EVENTS}".format(N_THRESHOLD_EVENTS=N_THRESHOLD_EVENTS):
            stage3["n_events"] >= N_THRESHOLD_EVENTS,
        f"n_trades_>= {N_THRESHOLD_TRADES}":
            stage1["n"] >= N_THRESHOLD_TRADES,
        f"per_event_ROI_3pp_slip_>= {ROI_THRESHOLD * 100:.0f}%":
            sens_3pp["avg_roi"] >= ROI_THRESHOLD,
        "per_event_avg_roi_positive":
            stage3["per_event_avg_roi"] > 0,
        "daily_realistic_3pp_>= $20":
            daily_realistic_3pp >= 20,
    }

    print(f"\n  Criteria checks:")
    all_pass = True
    for crit, ok in criteria.items():
        marker = "✅" if ok else "❌"
        print(f"  {marker} {crit}")
        if not ok:
            all_pass = False

    print(f"\n  Daily realistic @ $5K bankroll, 3pp slippage: ${daily_realistic_3pp:+.2f}")

    if all_pass:
        verdict = "PASS_SHADOW_READY"
        print(f"\n  ✅ {verdict}")
        print(f"  Next: запустить D14 в Stage Shadow на 2-4 недели")
    elif daily_realistic_3pp >= 5:
        verdict = "MARGINAL_RETHINK"
        print(f"\n  ⚠️ {verdict} — pos PnL но ниже $20/day threshold")
    else:
        verdict = "FAIL_KILL"
        print(f"\n  ❌ {verdict} — гипотеза не подтверждена")

    return {
        "verdict": verdict,
        "daily_realistic_3pp": daily_realistic_3pp,
        "criteria": criteria,
    }


VOL_FLOORS_TO_SWEEP = [5_000, 30_000, 100_000, 200_000]
ZONE_VARIANTS = [
    (0.85, 0.95, "wide [0.85, 0.95]"),
    (0.87, 0.93, "narrow [0.87, 0.93]"),
    (0.85, 0.97, "extended [0.85, 0.97]"),
]
ANCHOR_VARIANTS = [
    ([4, 8, 12, 24, 48, 72], "all_6_anchors"),
    ([12, 24, 48], "mid_3_anchors"),
    ([24], "24h_only"),
]


def sweep_filters(con) -> list[dict]:
    """Sweep vol_floor × zone × anchors для выбора оптимума."""
    print("\n" + "=" * 90)
    print("FILTER SWEEP — Politics-only, NO-side, exclude neg_risk + military")
    print("=" * 90)

    rows = []
    excl_kw_filter = " AND ".join(
        f"LOWER(m.question) NOT LIKE '%{kw}%'" for kw in EXCLUDE_KEYWORDS
    )

    for vol in VOL_FLOORS_TO_SWEEP:
        for zlo, zhi, zlabel in ZONE_VARIANTS:
            for anchors, alabel in ANCHOR_VARIANTS:
                anchors_str = ",".join(str(h) for h in anchors)
                df = con.execute(f"""
                    WITH signals AS (
                        SELECT m.condition_id, m.event_id, m.yes_won,
                               s.yes_price, s.hours_to_resolve, m.lifetime_volume_usd,
                               ROW_NUMBER() OVER (PARTITION BY m.condition_id ORDER BY s.hours_to_resolve) AS rn
                        FROM read_parquet('{MARKETS}') m
                        JOIN read_parquet('{SNAPS}') s ON s.condition_id = m.condition_id
                        WHERE s.hours_to_resolve IN ({anchors_str})
                          AND s.yes_price BETWEEN {zlo} AND {zhi}
                          AND m.yes_won IS NOT NULL
                          AND m.lifetime_volume_usd >= {vol}
                          AND m.category = 'Politics'
                          AND m.neg_risk = 0
                          AND {excl_kw_filter}
                    )
                    SELECT condition_id, event_id, yes_won, yes_price
                    FROM signals WHERE rn = 1
                """).df()
                if len(df) == 0:
                    rows.append({
                        "vol": vol, "zone": zlabel, "anchors": alabel,
                        "n": 0, "n_events": 0, "wr": None, "avg_roi_3pp": None, "daily_real": None,
                    })
                    continue
                df["cost_no"] = (1 - df["yes_price"] + 0.03).clip(0.01, 0.99)
                df["payout"] = (df["yes_won"] == 0).astype(int)
                df["roi"] = (df["payout"] / df["cost_no"]) - 1.0
                stake = LIVE_BANKROLL * MAX_STAKE_PCT
                fee_drag = stake * DEFAULT_FEE_PCT / 100.0
                net_pnl = stake * df["roi"].sum() - fee_drag * len(df)
                daily = net_pnl / N_DAYS_DATASET * HONEST_HAIRCUT
                rows.append({
                    "vol": vol, "zone": zlabel, "anchors": alabel,
                    "n": len(df), "n_events": df["event_id"].nunique(),
                    "wr": float(df["payout"].mean()),
                    "avg_roi_3pp": float(df["roi"].mean()),
                    "daily_real": daily,
                })

    print(f"\n{'vol':>8} {'zone':22} {'anchors':18} {'n':>5} {'n_ev':>5} "
          f"{'WR':>6} {'avg_ROI_3pp':>11} {'daily_real':>11}")
    print("-" * 100)
    for r in sorted(rows, key=lambda x: -(x["daily_real"] or -999)):
        if r["n"] == 0:
            continue
        print(f"{r['vol']:>8,} {r['zone']:22} {r['anchors']:18} "
              f"{r['n']:>5,} {r['n_events']:>5,} {r['wr']:>5.1%} "
              f"{r['avg_roi_3pp']:>+10.1%} ${r['daily_real']:>+9.2f}")

    return rows


def main():
    t0 = time.time()
    print("D14 PoliticsLadderHonest — Canonical Backtest + Filter Sweep")
    print(f"Default filters: Politics only, NO @ [{ZONE_LO}, {ZONE_HI}], 12-48h, "
          f"vol>=${VOL_FLOOR_USD:,}, exclude neg_risk + military keywords")

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    # Sweep first, выберем лучший комбо
    sweep_rows = sweep_filters(con)
    valid = [r for r in sweep_rows if r["n"] >= 100 and r["daily_real"] is not None]
    if valid:
        best = max(valid, key=lambda x: x["daily_real"])
        print(f"\n>>> BEST combo (n>=100): vol=${best['vol']:,} {best['zone']} {best['anchors']}")
        print(f"    n={best['n']:,}, n_events={best['n_events']:,}, "
              f"WR={best['wr']:.1%}, daily_real=${best['daily_real']:+.2f}")

    # Default config
    stage1 = stage1_collect(con)
    stage2 = stage2_economics(stage1) if stage1["n"] > 0 else {}
    stage3 = stage3_per_event(stage1) if stage1["n"] > 0 else {}
    verdict = stage4_verdict(stage1, stage2, stage3) if stage1["n"] > 0 else {"verdict": "NO_DATA"}

    # Save report
    out_dir = Path("/root/oracle/products/d14-politics-ladder-honest")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"d14_canonical_{date.today().isoformat()}.md"

    lines = [
        f"# D14 PoliticsLadderHonest — Canonical Backtest ({date.today().isoformat()})",
        "",
        f"**Filters:** Politics only, NO @ [{ZONE_LO}, {ZONE_HI}], 12-48h, "
        f"vol>=${VOL_FLOOR_USD:,}, exclude neg_risk + military keywords",
        "",
        f"**Verdict:** `{verdict.get('verdict', 'N/A')}`",
        "",
        f"- n_signals: {stage1['n']:,}",
        f"- n_events: {stage3.get('n_events', 0):,}",
        f"- trades/day: {stage2.get('trades_per_day', 0):.1f}",
        "",
        "## Stage 2 — Slippage Sensitivity",
        "",
        "| slip_pp | avg_cost | WR | avg_ROI | net_PnL | daily_raw | daily_realistic |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in stage2.get("sensitivity", []):
        lines.append(
            f"| {r['slip_pp']}pp | ${r['avg_cost']:.4f} | {r['wr']:.1%} | "
            f"{r['avg_roi']:+.1%} | ${r['net_pnl']:+,.0f} | ${r['daily_raw']:+.2f} | "
            f"${r['daily_realistic']:+.2f} |"
        )

    if stage3:
        lines.extend([
            "",
            "## Stage 3 — Per-event",
            "",
            f"- n_unique_events: {stage3['n_events']:,}",
            f"- avg markets per event: {stage3['avg_markets_per_event']:.2f}",
            f"- per-event avg_ROI: {stage3['per_event_avg_roi']:+.1%}",
            f"- per-event WR: {stage3['per_event_wr']:.1%}",
        ])

    out_path.write_text("\n".join(lines))
    print(f"\nReport saved: {out_path}")
    print(f"Total: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
