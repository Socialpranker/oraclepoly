#!/usr/bin/env python3
"""D4 SmallMarketFade — Canonical RA on /mnt/data/canonical/.

Hypothesis: на маленьких рынках (lifetime_volume < $5K) с entry_price 0.50-0.85
retail FOMO систематически overpriced YES в категориях Culture/Weather/Politics/
Geopolitics/Sports.

Filters:
  - lifetime_volume_usd < $5K
  - entry zone: high (0.70-0.85) or mid (0.50-0.70)
  - whitelist categories
  - anchor 6h
  - exclude Crypto

Output: docs/backtests/D4_small_market_fade.md
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb

from _backtest_params import DEFAULT_FEE_PCT, SLIPPAGE_PP as _SLIP, BACKTEST_DAYS

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"

ANCHOR_HOURS = 4  # canonical snaps available at 4h; production uses 6h (closest match)
VOL_THRESHOLD = 5000.0

WHITELIST = ["Culture", "Weather", "Politics", "Geopolitics", "Sports"]
EXCLUDE = ["Crypto"]

SLIPPAGE_PP = _SLIP["D4"]


def zone(price):
    if 0.50 <= price < 0.70:
        return "mid"
    if 0.70 <= price < 0.85:
        return "high"
    return None


def main():
    print("=" * 70)
    print("D4 SmallMarketFade — Canonical RA")
    print(f"Source: {MARKETS}, anchor={ANCHOR_HOURS}h, vol<${VOL_THRESHOLD}")
    print("=" * 70)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=4")

    print("\n1. Loading small market signals...")
    df = con.execute(
        f"""
        SELECT
            m.condition_id,
            m.category,
            m.lifetime_volume_usd,
            m.yes_won,
            s.yes_price
        FROM read_parquet('{MARKETS}') m
        JOIN read_parquet('{SNAPS}') s
          ON s.condition_id = m.condition_id
        WHERE s.hours_to_resolve = {ANCHOR_HOURS}
          AND m.yes_won IS NOT NULL
          AND m.lifetime_volume_usd < {VOL_THRESHOLD}
          AND m.lifetime_volume_usd > 0
          AND m.category IN ({','.join(f"'{c}'" for c in WHITELIST)})
          AND s.yes_price >= 0.50
          AND s.yes_price < 0.85
        """
    ).fetchdf()
    print(f"   {len(df):,} candidate signals")

    # Compute zone
    df["zone_label"] = df["yes_price"].apply(zone)
    df = df.dropna(subset=["zone_label"])

    # Bucket: category × zone
    print("\n2. Per-bucket WR (NO bet on overpriced YES)")
    print(f"{'category':<14} {'zone':<6} {'n':>7} {'n_no_win':>10} {'WR':>8} {'avg_no_cost':>12}")
    print("-" * 70)

    bucket_results = []
    for cat in WHITELIST:
        for zone_label in ["mid", "high"]:
            sub = df[(df["category"] == cat) & (df["zone_label"] == zone_label)]
            n = len(sub)
            if n < 10:
                continue
            n_no_win = (sub["yes_won"] == 0).sum()
            wr = n_no_win / n * 100
            avg_cost = (1 - sub["yes_price"]).mean()
            print(f"{cat:<14} {zone_label:<6} {n:>7,} {n_no_win:>10,} {wr:>7.1f}% {avg_cost:>12.4f}")
            bucket_results.append(
                {
                    "category": cat,
                    "zone": zone_label,
                    "n": n,
                    "wr_pct": round(wr, 1),
                    "avg_no_cost": round(avg_cost, 4),
                }
            )

    # Realistic ROI net
    n_total = len(df)
    n_wins = (df["yes_won"] == 0).sum()
    wr_total = n_wins / n_total * 100
    avg_cost = (1 - df["yes_price"]).mean()
    gross_payout = wr_total / 100 * 1.0
    gross_roi = (gross_payout - avg_cost) / avg_cost * 100

    cost_net = avg_cost + SLIPPAGE_PP / 100
    fee_drag = DEFAULT_FEE_PCT * 2 / 100
    net_payout = wr_total / 100 * (1.0 - fee_drag)
    net_roi = (net_payout - cost_net) / cost_net * 100

    days = BACKTEST_DAYS
    trades_per_day = n_total / days
    avg_stake = 25
    daily_pnl = trades_per_day * avg_stake * (net_roi / 100)

    print(f"\n3. Aggregate: n={n_total:,}, WR={wr_total:.1f}%, gross ROI={gross_roi:+.1f}%")
    print(f"   Net ROI (fees + {SLIPPAGE_PP}pp slip): {net_roi:+.1f}%")
    print(f"   Trades/day: {trades_per_day:.2f}, daily PnL @ $5K: ${daily_pnl:+.2f}")

    verdict = "PASS" if (net_roi > 0 and n_total >= 1000) else "MARGINAL" if net_roi > 0 else "FAIL"
    haircut_factor = round(net_roi / max(gross_roi, 1), 2)
    honest_daily_5k = daily_pnl

    out_path = Path("docs/backtests/D4_small_market_fade.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md = f"""---
detector: D4_small_market_fade
verdict: {verdict}
backtest_date: 2026-05-03
canonical_source: scripts/research/d4_canonical.py
data_source: /mnt/data/canonical/markets_clean.parquet + price_snapshots.parquet
n_signals: {n_total}
backtest_days: {days}
roi_net_pct: {round(net_roi, 1)}
wr_pct: {round(wr_total, 1)}
daily_realistic_usd_at_5k: {round(daily_pnl, 2)}
daily_realistic_usd_at_1k: {round(daily_pnl / 5, 2)}
honest_haircut_factor: {haircut_factor}
honest_daily_usd_at_5k: {round(honest_daily_5k, 2)}
honest_daily_usd_at_1k: {round(honest_daily_5k / 5, 2)}
honest_haircut_reason: net of fees ({DEFAULT_FEE_PCT}% × 2) + slippage ({SLIPPAGE_PP}pp, thin book на small markets). Production filter может использовать volume_24h vs canonical lifetime_volume.
trades_per_day: {round(trades_per_day, 2)}
adversarial_mirror_pass: false
adversarial_mirror_note: NO-only edge (categorical YES-mispricing на small markets, asymmetric)
post_v2_split: {verdict.lower()}
---

# D4 SmallMarketFade — Canonical Backtest

**Hypothesis:** На маленьких категорийных рынках (`lifetime_volume_usd < $5K`) с entry_price 0.50-0.85 retail FOMO систематически overpriced YES в whitelisted categories.

**Verdict:** {verdict}

## BUCKET_WR table (canonical {days} days, NO-only)

| category    | zone | n     | WR    | avg_no_cost |
|-------------|------|-------|-------|-------------|
"""
    for b in bucket_results:
        md += f"| {b['category']:<11} | {b['zone']:<4} | {b['n']:>5} | {b['wr_pct']:>4.1f}% | {b['avg_no_cost']:>11.4f} |\n"

    md += f"""
## Filters

- **Categories whitelist:** {', '.join(WHITELIST)}
- **Excluded:** Crypto
- **Volume:** `lifetime_volume_usd < ${VOL_THRESHOLD:.0f}`
- **Zone:** YES [0.50, 0.85) — split mid/high
- **Anchor:** {ANCHOR_HOURS}h до резолва
- **Side:** NO

## Honest expectations vs canonical

- **Canonical gross ROI:** {gross_roi:+.1f}% (n={n_total} signals)
- **Net ROI:** {net_roi:+.1f}% (after fees + {SLIPPAGE_PP}pp slip)
- **Honest daily @ $5K:** ${daily_pnl:+.2f}
- **Trades/day:** {trades_per_day:.2f}

Generated by `scripts/research/d4_canonical.py` on canonical parquet ({days}-day HF).
"""

    out_path.write_text(md)
    print(f"\n4. Written → {out_path}")
    print(f"\nDone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
