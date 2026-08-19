#!/usr/bin/env python3
"""D15 SportsUnderdogNO — Sports-only NO favorite-fade с filter sweep.

Гипотеза (из ресёрча entry_filters_research_2026-05-02 Top-5):
  Sports — другой механизм vs Politics. Game outcomes более binary, моментум.
  Если применить:
    - Sports only (exclude Crypto/Politics/etc)
    - NO @ [0.85, 0.92] underdog NO
    - 4-24h до резолва (game close)
    - Exclude fight/UFC keywords (heavy variance, insider-heavy)
    - Exclude neg_risk
    - vol_floor для liquid book

  → Может найти incremental edge сверх D7b extended (если Sports-specific
  механизм отличается от general FLB).

Если KILL — D7b general filters covers Sports correctly.

Sweep: vol_floor × zone × anchors × fight_exclude.
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

CATEGORY = "Sports"
EXCLUDE_NEG_RISK = True

# Default config (research recommendation)
DEFAULT_VOL = 5_000  # learn from D14 — high vol_floor убивает sample
DEFAULT_ZONE = (0.85, 0.92)
DEFAULT_ANCHORS = [4, 8, 12, 24]

# Fight/UFC keywords (exclude — heavy variance + insider edge)
FIGHT_KEYWORDS = [
    "ufc", "knockout", "boxing", "mma", "championship belt",
]

# Stage thresholds
ROI_THRESHOLD = 0.30
N_THRESHOLD_EVENTS = 200
N_THRESHOLD_TRADES = 500

# Execution sim
LIVE_BANKROLL = 5_000.0
MAX_STAKE_PCT = 0.02
DEFAULT_FEE_PCT = 1.5
HONEST_HAIRCUT = 0.5
N_DAYS_DATASET = 821


def fetch_signals(con, vol_floor: int, zone: tuple[float, float],
                  anchors: list[int], exclude_fight: bool) -> "duckdb.DuckDBPyConnection":
    anchors_str = ",".join(str(h) for h in anchors)
    fight_filter = ""
    if exclude_fight:
        fight_filter = " AND " + " AND ".join(
            f"LOWER(m.question) NOT LIKE '%{kw}%'" for kw in FIGHT_KEYWORDS
        )

    return con.execute(f"""
        WITH signals AS (
            SELECT m.condition_id, m.event_id, m.yes_won,
                   s.yes_price, s.hours_to_resolve, m.lifetime_volume_usd, m.question,
                   ROW_NUMBER() OVER (PARTITION BY m.condition_id ORDER BY s.hours_to_resolve) AS rn
            FROM read_parquet('{MARKETS}') m
            JOIN read_parquet('{SNAPS}') s ON s.condition_id = m.condition_id
            WHERE s.hours_to_resolve IN ({anchors_str})
              AND s.yes_price BETWEEN {zone[0]} AND {zone[1]}
              AND m.yes_won IS NOT NULL
              AND m.lifetime_volume_usd >= {vol_floor}
              AND m.category = '{CATEGORY}'
              AND ({"m.neg_risk = 0" if EXCLUDE_NEG_RISK else "TRUE"})
              {fight_filter}
        )
        SELECT condition_id, event_id, yes_won, yes_price, hours_to_resolve,
               lifetime_volume_usd, question
        FROM signals WHERE rn = 1
    """).df()


def calc_economics(df, slip_pp: float = 3.0) -> dict:
    if len(df) == 0:
        return {"n": 0, "n_events": 0, "wr": None, "avg_roi": None, "daily_real": None}
    df = df.copy()
    df["cost_no"] = (1 - df["yes_price"] + slip_pp / 100.0).clip(0.01, 0.99)
    df["payout"] = (df["yes_won"] == 0).astype(int)
    df["roi"] = (df["payout"] / df["cost_no"]) - 1.0
    stake = LIVE_BANKROLL * MAX_STAKE_PCT
    fee_drag = stake * DEFAULT_FEE_PCT / 100.0
    net_pnl = stake * df["roi"].sum() - fee_drag * len(df)
    daily = net_pnl / N_DAYS_DATASET * HONEST_HAIRCUT
    return {
        "n": len(df),
        "n_events": df["event_id"].nunique(),
        "wr": float(df["payout"].mean()),
        "avg_roi": float(df["roi"].mean()),
        "daily_real": daily,
        "df": df,
    }


def sweep_filters(con) -> list[dict]:
    """Sweep vol_floor × zone × anchors × fight_exclude."""
    print("\n" + "=" * 100)
    print("D15 FILTER SWEEP — Sports-only, NO-side, exclude neg_risk")
    print("=" * 100)

    rows = []
    for vol in [5_000, 30_000, 100_000]:
        for zone, zlabel in [
            ((0.85, 0.92), "narrow [0.85, 0.92]"),
            ((0.85, 0.95), "wide [0.85, 0.95]"),
            ((0.85, 0.97), "extended [0.85, 0.97]"),
        ]:
            for anchors, alabel in [
                ([4, 8, 12, 24], "4-24h_short"),
                ([4, 8, 12, 24, 48, 72], "all_6_anchors"),
                ([24], "24h_only"),
            ]:
                for exclude_fight in [True, False]:
                    df = fetch_signals(con, vol, zone, anchors, exclude_fight)
                    e = calc_economics(df)
                    rows.append({
                        "vol": vol, "zone": zlabel, "anchors": alabel,
                        "fight_excl": exclude_fight,
                        **{k: v for k, v in e.items() if k != "df"},
                    })

    print(f"\n{'vol':>8} {'zone':22} {'anchors':16} {'fight_excl':>10} "
          f"{'n':>5} {'n_ev':>5} {'WR':>6} {'avg_ROI_3pp':>11} {'daily_real':>11}")
    print("-" * 110)
    for r in sorted(rows, key=lambda x: -(x["daily_real"] or -999))[:25]:
        if r["n"] == 0:
            continue
        print(f"{r['vol']:>8,} {r['zone']:22} {r['anchors']:16} "
              f"{str(r['fight_excl']):>10} {r['n']:>5,} {r['n_events']:>5,} "
              f"{r['wr']:>5.1%} {r['avg_roi']:>+10.1%} ${r['daily_real']:>+9.2f}")

    return rows


def stage_per_anchor_breakdown(con, vol: int, zone: tuple[float, float],
                                anchors: list[int], exclude_fight: bool) -> None:
    """Стандартная разбивка по anchor для best combo."""
    print("\n" + "=" * 90)
    print(f"PER-ANCHOR breakdown — vol=${vol:,} {zone} fight_excl={exclude_fight}")
    print("=" * 90)
    print(f"{'anchor':>7} {'n':>6} {'n_ev':>5} {'WR':>6} "
          f"{'avg_ROI_3pp':>11} {'daily_real':>11}")
    print("-" * 70)
    total_pnl = 0.0
    for anchor in anchors:
        df = fetch_signals(con, vol, zone, [anchor], exclude_fight)
        e = calc_economics(df)
        if e["n"] == 0:
            continue
        total_pnl += e.get("daily_real", 0) or 0
        print(f"{anchor:>5}h {e['n']:>6,} {e['n_events']:>5,} {e['wr']:>5.1%} "
              f"{e['avg_roi']:>+10.1%} ${e['daily_real']:>+9.2f}")


def stage_per_sport_breakdown(con, vol: int, zone: tuple[float, float],
                               anchors: list[int]) -> None:
    """Разбивка по типу sport чтобы понять где edge concentrated."""
    print("\n" + "=" * 90)
    print("PER-SPORT breakdown (без exclude_fight чтобы видеть всё)")
    print("=" * 90)
    df = fetch_signals(con, vol, zone, anchors, exclude_fight=False)
    if len(df) == 0:
        print("  No signals.")
        return

    df["sport_type"] = df["question"].str.lower().apply(_sport_type)
    print(f"{'sport':>14} {'n':>6} {'WR':>6} {'avg_ROI_3pp':>11}")
    print("-" * 50)
    for sport, sub in sorted(df.groupby("sport_type"), key=lambda x: -len(x[1])):
        if len(sub) < 30:
            continue
        sub = sub.copy()
        sub["cost_no"] = (1 - sub["yes_price"] + 0.03).clip(0.01, 0.99)
        sub["payout"] = (sub["yes_won"] == 0).astype(int)
        sub["roi"] = (sub["payout"] / sub["cost_no"]) - 1.0
        print(f"{sport:>14} {len(sub):>6,} {float(sub['payout'].mean()):>5.1%} "
              f"{float(sub['roi'].mean()):>+10.1%}")


def _sport_type(q: str) -> str:
    if any(kw in q for kw in ["ufc", "knockout", "boxing", "mma"]):
        return "fight"
    if any(kw in q for kw in ["nfl", "super bowl"]):
        return "nfl"
    if any(kw in q for kw in ["nba", "basketball"]):
        return "nba"
    if any(kw in q for kw in ["mlb", "baseball", "world series"]):
        return "mlb"
    if any(kw in q for kw in ["nhl", "hockey", "stanley"]):
        return "nhl"
    if any(kw in q for kw in ["soccer", "football", "premier league", "la liga"]):
        return "soccer"
    if any(kw in q for kw in ["tennis", "wimbledon", "grand slam"]):
        return "tennis"
    return "other"


def main():
    t0 = time.time()
    print("D15 SportsUnderdogNO — Canonical Backtest + Filter Sweep")

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    # Sweep first
    sweep_rows = sweep_filters(con)
    valid = [r for r in sweep_rows if r["n"] >= 100 and r["daily_real"] is not None]
    if not valid:
        print("\n>>> NO valid combos with n>=100. KILL D15.")
        return

    best = max(valid, key=lambda x: x["daily_real"])
    print(f"\n>>> BEST combo (n>=100): vol=${best['vol']:,} {best['zone']} "
          f"{best['anchors']} fight_excl={best['fight_excl']}")
    print(f"    n={best['n']:,}, n_events={best['n_events']:,}, "
          f"WR={best['wr']:.1%}, daily_real=${best['daily_real']:+.2f}")

    # Compare research-recommended vs best
    research = next(
        (r for r in sweep_rows
         if r["vol"] == 5_000 and r["zone"] == "narrow [0.85, 0.92]"
         and r["anchors"] == "4-24h_short" and r["fight_excl"] is True),
        None,
    )
    if research and research["n"] > 0:
        print(f"\n>>> RESEARCH recommendation: vol=$5K [0.85, 0.92] 4-24h fight_excl=True")
        print(f"    n={research['n']:,}, n_events={research['n_events']:,}, "
              f"WR={research['wr']:.1%}, daily_real=${research['daily_real']:+.2f}")

    # Per-anchor breakdown for best combo
    best_zone = (0.85, 0.92) if "narrow" in best["zone"] else (
        (0.85, 0.95) if "wide" in best["zone"] else (0.85, 0.97)
    )
    best_anchors = (
        [4, 8, 12, 24] if "4-24h" in best["anchors"]
        else ([4, 8, 12, 24, 48, 72] if "all_6" in best["anchors"] else [24])
    )
    stage_per_anchor_breakdown(con, best["vol"], best_zone, best_anchors, best["fight_excl"])
    stage_per_sport_breakdown(con, best["vol"], best_zone, best_anchors)

    # Slippage sensitivity для best combo
    print("\n" + "=" * 90)
    print(f"SLIPPAGE SENSITIVITY — best combo")
    print("=" * 90)
    df = fetch_signals(con, best["vol"], best_zone, best_anchors, best["fight_excl"])
    avg_cost_quoted = float(1 - df["yes_price"].mean()) if len(df) > 0 else 0.0
    print(f"{'slip_pp':>8} {'avg_cost':>9} {'WR':>6} {'avg_ROI':>10} {'daily_real':>11}")
    print("-" * 60)
    sensitivity = []
    for slip_pp in [0, 2, 3, 5, 8, 10]:
        e = calc_economics(df, slip_pp=slip_pp)
        sensitivity.append({"slip_pp": slip_pp, **{k: v for k, v in e.items() if k != "df"}})
        avg_cost_real = avg_cost_quoted + slip_pp / 100.0
        print(f"{slip_pp:>6}pp ${avg_cost_real:>7.4f} {e['wr']:>5.1%} "
              f"{e['avg_roi']:>+9.1%} ${e['daily_real']:>+10.2f}")

    # Verdict
    print("\n" + "=" * 90)
    print("VERDICT")
    print("=" * 90)
    sens_3pp = next(r for r in sensitivity if r["slip_pp"] == 3)
    print(f"\n  Best combo daily_real @ 3pp slip: ${sens_3pp['daily_real']:+.2f}")
    print(f"  Trades/day: {sens_3pp['n'] / N_DAYS_DATASET:.1f}")

    if sens_3pp["daily_real"] >= 20 and sens_3pp["n_events"] >= N_THRESHOLD_EVENTS:
        verdict = "PASS_SHADOW_READY"
    elif sens_3pp["daily_real"] >= 5:
        verdict = "MARGINAL"
    else:
        verdict = "FAIL_KILL"
    print(f"\n  >>> {verdict}")

    # Critical: D7b extended overlap check
    print(f"\n  D7b extended overlap: D7b also covers Sports + zone [0.85, 0.97].")
    print(f"  D15 incremental alpha = D15_daily - D7b_Sports_subset_daily")
    print(f"  Если D15 ≈ D7b Sports subset → KILL (нет incremental)")
    print(f"  Если D15 > D7b Sports subset → keep (Sports-specific structure)")

    # Save report
    out_dir = Path("/root/oracle/products/d15-sports-underdog-no")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"d15_canonical_{date.today().isoformat()}.md"

    lines = [
        f"# D15 SportsUnderdogNO — Canonical ({date.today().isoformat()})",
        "",
        f"**Verdict:** `{verdict}`",
        "",
        f"## Best combo",
        f"- vol_floor: ${best['vol']:,}",
        f"- zone: {best['zone']}",
        f"- anchors: {best['anchors']}",
        f"- fight_excl: {best['fight_excl']}",
        f"- n: {best['n']:,}, n_events: {best['n_events']:,}",
        f"- WR: {best['wr']:.1%}",
        f"- daily_real @ 3pp slip: ${sens_3pp['daily_real']:+.2f}",
        "",
        "## Slippage sensitivity",
        "",
        "| slip_pp | WR | avg_ROI | daily_real |",
        "|---|---|---|---|",
    ]
    for s in sensitivity:
        lines.append(f"| {s['slip_pp']}pp | {s['wr']:.1%} | "
                     f"{s['avg_roi']:+.1%} | ${s['daily_real']:+.2f} |")
    lines.extend([
        "",
        "## Top sweep results",
        "",
        "| vol | zone | anchors | fight_excl | n | n_ev | WR | avg_ROI_3pp | daily_real |",
        "|---|---|---|---|---|---|---|---|---|",
    ])
    for r in sorted(sweep_rows, key=lambda x: -(x["daily_real"] or -999))[:15]:
        if r["n"] == 0:
            continue
        lines.append(
            f"| ${r['vol']:,} | {r['zone']} | {r['anchors']} | {r['fight_excl']} | "
            f"{r['n']:,} | {r['n_events']:,} | {r['wr']:.1%} | "
            f"{r['avg_roi']:+.1%} | ${r['daily_real']:+.2f} |"
        )
    out_path.write_text("\n".join(lines))
    print(f"\nReport saved: {out_path}")
    print(f"Total: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
