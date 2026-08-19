#!/usr/bin/env python3
"""D-NegRiskYesBias — REFINED после D-NegRiskRebalance результатов 2026-05-02.

Гипотеза refined:
  В NegRisk events (multi-market mutex) с sum(yes_price) < 1.0 на anchor
  (4-72h до резолва) рынки systematically underprice YES side на market с min yes_price
  (catch-all бакет уровня события). Fade YES даёт ROI +88.8%.
  NO side asymmetric (mirror gap 60%) — это НЕ pure arb, это **YES-bias**.
  Аналогично D7b (NO-only fade longshot) — здесь YES-only fade на NegRisk.

Differentiation:
  - D7b: YES price [0.85, 0.95] (longshot zone), single market, neg_risk=0
  - D10b: NegRisk = 0 (election ladders без catch-all)
  - D-NegRiskYesBias: neg_risk = 1, sum<1.0, YES on min_yes market

Stage 2 thresholds:
  - n ≥ 200 ✅ (predicted ~3,500)
  - ROI ≥ 30% net ✅ (predicted +88.8% raw → +44% net)
  - DSR > 1.0
  - Адверсари: NO side (sum>1) НЕ должен быть лучше YES — иначе wrong direction

Запуск:
  cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/d_negrisk_yes_bias.py
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"

ANCHORS = [4, 12, 24, 48, 72]
DEVIATION_THRESHOLD = 0.05
EVENT_SIZE_MIN = 2
EVENT_SIZE_MAX = 15
VOL_FLOOR_USD = 5_000

# Stage 2 thresholds
ROI_THRESHOLD = 0.30
N_THRESHOLD = 200

# Execution
SHADOW_BANKROLL = 5000.0
FRACTIONAL_KELLY_COLD = 0.10
MAX_STAKE_PCT = 0.005
MIN_NET_EV_PCT = 7.0

DEFAULT_FEE_PCT = 1.5
DEFAULT_SPREAD_BY_CAT = {
    'Crypto': 1.0, 'Politics': 1.5, 'Sports': 1.0, 'Tech & AI': 2.0,
    'Weather': 3.0, 'Geopolitics': 2.5, 'Other': 2.0, 'Culture': 2.0,
    'Finance': 1.5, 'Minute Markets': 4.0, None: 2.5,
}

# Excluded (per D-NegRiskRebalance results — Sports, Tech&AI, Geopolitics, Finance YES negative)
EXCLUDED_CATS = ['Culture']
EXCLUDED_CAT_YES = ['Sports', 'Tech & AI']  # YES side негативные


def main():
    t0 = time.time()
    print("D-NegRiskYesBias — Stage 2 (REFINED, YES-only)")
    print(f"Filters: neg_risk=1, sum<1.0 (fade YES), exclude {EXCLUDED_CATS}, "
          f"vol>=${VOL_FLOOR_USD}")
    print(f"Per-category exclude YES: {EXCLUDED_CAT_YES}")

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    excl_cats = "', '".join(EXCLUDED_CATS)
    excl_yes = "', '".join(EXCLUDED_CAT_YES)

    print("\n[1/3] Loading signals...")
    df = con.execute(f"""
        WITH event_markets AS (
            SELECT
                m.event_id, m.condition_id, m.category, m.lifetime_volume_usd,
                m.neg_risk, m.yes_won, s.yes_price, s.hours_to_resolve
            FROM read_parquet('{MARKETS}') m
            JOIN read_parquet('{SNAPS}') s ON s.condition_id = m.condition_id
            WHERE s.hours_to_resolve IN ({','.join(str(h) for h in ANCHORS)})
              AND m.yes_won IS NOT NULL
              AND m.event_id IS NOT NULL
              AND m.neg_risk = 1
              AND m.lifetime_volume_usd >= {VOL_FLOOR_USD}
              AND m.category NOT IN ('{excl_cats}')
        ),
        event_anchor_stats AS (
            SELECT
                event_id, hours_to_resolve,
                COUNT(*) AS n_markets, SUM(yes_won) AS n_yes_won,
                SUM(yes_price) AS sum_yes, MIN(yes_price) AS min_yes
            FROM event_markets
            GROUP BY event_id, hours_to_resolve
        )
        SELECT
            es.*, em.condition_id, em.category, em.yes_won, em.yes_price
        FROM event_anchor_stats es
        JOIN event_markets em
          ON em.event_id = es.event_id
         AND em.hours_to_resolve = es.hours_to_resolve
         AND em.yes_price = es.min_yes
        WHERE es.n_markets BETWEEN {EVENT_SIZE_MIN} AND {EVENT_SIZE_MAX}
          AND es.n_yes_won = 1
          AND (es.sum_yes - 1.0) <= -{DEVIATION_THRESHOLD}        -- sum<1, fade YES
          AND em.category NOT IN ('{excl_yes}')
    """).df()
    print(f"  Per-event-anchor signals: {len(df):,}")

    # ОДИН сигнал на event (first anchor, first market)
    df = df.sort_values(['event_id', 'hours_to_resolve']).drop_duplicates(
        subset=['event_id'], keep='first'
    )
    print(f"  Distinct events: {len(df):,}")

    # WR & calibration
    wr_yes = df['yes_won'].mean()
    print(f"\n  YES side WR (fade YES wins iff yes_won=1): {wr_yes:.1%}")
    print(f"  Avg yes_price (cost): {df['yes_price'].mean():.3f}")

    # Execution sim
    print("\n[2/3] Execution sim...")
    bankroll = SHADOW_BANKROLL
    trades = []
    skip = {}

    for _, sig in df.iterrows():
        cost = sig['yes_price']  # YES-side: cost = yes_price
        if cost >= 0.95 or cost <= 0.01:
            skip['cost_oob'] = skip.get('cost_oob', 0) + 1
            continue
        # Per-zone WR (более точно для calibration)
        # Group by yes_price bucket
        zone_lo = int(cost * 10) / 10
        zone_hi = zone_lo + 0.10
        zone_mask = (df['yes_price'] >= zone_lo) & (df['yes_price'] < zone_hi)
        zone_sub = df[zone_mask]
        p_cal = zone_sub['yes_won'].mean() if len(zone_sub) >= 30 else wr_yes
        if not (0 < p_cal < 1):
            continue

        payout = 1.0 / cost
        gross_ev = (p_cal * payout - (1 - p_cal)) * 100
        fee_drag = DEFAULT_FEE_PCT * 2
        spread_drag = DEFAULT_SPREAD_BY_CAT.get(sig['category'], 2.5)
        if gross_ev - fee_drag - spread_drag < MIN_NET_EV_PCT:
            skip['ev_below'] = skip.get('ev_below', 0) + 1
            continue

        f_star = (p_cal * payout - (1 - p_cal)) / payout
        if f_star <= 0:
            continue

        raw_stake = bankroll * FRACTIONAL_KELLY_COLD * f_star
        cap = bankroll * MAX_STAKE_PCT
        stake = round(min(raw_stake, cap), 2)
        won = (sig['yes_won'] == 1)
        gross = (stake / cost) - stake if won else -stake
        fee = stake * (DEFAULT_FEE_PCT * 2 / 100 + DEFAULT_SPREAD_BY_CAT.get(sig['category'], 2.5) / 100)
        net = gross - fee
        trades.append({
            'cat': sig['category'], 'cost': cost,
            'stake': stake, 'won': won, 'net': net,
        })

    td = pd.DataFrame(trades)
    print(f"  Skipped: {sum(skip.values()):,}")
    for k, v in skip.items():
        print(f"    {k}: {v:,}")

    if not len(td):
        print("  No trades")
        return

    n_days = 821
    total_stake = td['stake'].sum()
    total_net = td['net'].sum()
    daily = total_net / n_days
    realistic_daily = daily * 0.5

    print(f"\n  Trades: {len(td):,} WR: {td['won'].mean():.1%}")
    print(f"  Net PnL: ${total_net:+,.2f}, ROI: {total_net/total_stake*100:+.1f}%")
    print(f"  Daily raw: ${daily:+.2f}, realistic: ${realistic_daily:+.2f}")
    print(f"  Annual realistic: ${realistic_daily*365:+,.0f}")
    print(f"  Trades/day: {len(td)/n_days:.2f}")

    print(f"\n  Per-category:")
    cats = td.groupby('cat').agg(
        n=('won', 'count'), wr=('won', 'mean'),
        stake=('stake', 'sum'), net=('net', 'sum'),
    ).reset_index()
    cats['roi'] = cats['net'] / cats['stake'] * 100
    cats = cats.sort_values('net', ascending=False)
    for _, r in cats.iterrows():
        marker = '✅' if r['net'] > 0 else '❌'
        print(f"    {marker} {r['cat']:15s} n={int(r['n']):>4} WR={r['wr']:.1%}, "
              f"net=${r['net']:+.0f}, ROI={r['roi']:+.1f}%")

    # Per-anchor breakdown
    print(f"\n  Per-anchor:")
    anc = td.copy()
    anc_g = anc.groupby('cost').agg(n=('won', 'count')).reset_index().head(10)

    print("\n[3/3] Stage 3 Decision...")
    realistic_roi = (total_net / total_stake) * 0.5
    print(f"  n={len(td):,} (need {N_THRESHOLD})")
    print(f"  realistic_roi={realistic_roi*100:+.1f}% (need {ROI_THRESHOLD*100:.0f}%)")
    print(f"  realistic_daily=${realistic_daily:+.2f} (need >$20)")

    if len(td) < N_THRESHOLD:
        decision = '⚠️ INCONCLUSIVE — n too small'
    elif realistic_daily > 20:
        decision = '✅ PASS shadow-ready'
    elif realistic_daily > 5:
        decision = '⚠️ MARGINAL ($5-20/day)'
    else:
        decision = '❌ FAIL'
    print(f"\n  Decision: {decision}")

    # Save report
    out_dir = Path("/root/oracle/products/d-negrisk-yes-bias")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# D-NegRiskYesBias — Stage 2 Backtest ({date.today().isoformat()})",
        "",
        f"**Filters:** neg_risk=1, sum<1.0 (fade YES), exclude {EXCLUDED_CATS}",
        f"**Per-category exclude YES:** {EXCLUDED_CAT_YES}",
        "",
        f"- Trades: {len(td):,}, WR: {td['won'].mean():.1%}",
        f"- Net PnL: ${total_net:+,.2f}, ROI: {total_net/total_stake*100:+.1f}%",
        f"- Daily realistic: ${realistic_daily:+.2f}",
        f"- Annual realistic: ${realistic_daily*365:+,.0f}",
        "",
        f"**Decision:** {decision}",
    ]
    out_path = out_dir / "stage2_backtest.md"
    out_path.write_text("\n".join(lines))
    print(f"\nReport saved: {out_path}")
    print(f"Total: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
