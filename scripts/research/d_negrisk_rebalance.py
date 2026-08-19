#!/usr/bin/env python3
"""D-NegRiskRebalance — Stage 2 backtest для NegRisk events arb.

Гипотеза:
  В NegRisk events (mutex multi-market) sum(yes_price) ≠ 1.0 даёт arb.
  D10b работает на non-NegRisk events. Эта гипотеза — для NegRisk specifically.
  Когда sum > 1.0 → fade NO на market с max yes_price (он переоценён).
  Когда sum < 1.0 → fade YES на market с min yes_price (он недооценён).

Differentiation от D10b:
  - D10b: WHERE neg_risk = 0
  - D-NegRiskRebalance: WHERE neg_risk = 1 (отдельная sub-population)
  - $29M extracted documented = bot competition уже жёсткая, retail edge может быть exhausted

Pipeline:
  1. Multi-anchor [4, 12, 24, 48, 72]h (как в D10b refined)
  2. Event size 2-15 markets (NegRisk обычно больше chains)
  3. Deviation threshold |sum-1.0| >= 5%
  4. Per-event ОДИН сигнал — first anchor с deviation
  5. Mirror check: NO-side vs YES-side — обе симметричны?

Stage 2 thresholds:
  - ROI ≥ 30% net
  - n ≥ 200
  - DSR > 1.0
  - Mirror diff < 50%

Запуск:
  cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/d_negrisk_rebalance.py
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

# ── Constants ────────────────────────────────────────────────────────────────

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

# Excluded categories (per D7b/D10b lessons)
EXCLUDED_CATS = ['Culture']


def stage2_load_signals(con):
    """Load NegRisk events with anchor pricing + sum(yes_price) deviation."""
    print(f"\n[1/4] Loading NegRisk events with deviation >= {DEVIATION_THRESHOLD}...")
    excl_str = "', '".join(EXCLUDED_CATS)

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
              AND m.neg_risk = 1                            -- NegRisk only
              AND m.lifetime_volume_usd >= {VOL_FLOOR_USD}
              AND m.category NOT IN ('{excl_str}')
        ),
        event_anchor_stats AS (
            SELECT
                event_id, hours_to_resolve,
                COUNT(*) AS n_markets,
                SUM(yes_won) AS n_yes_won,
                SUM(yes_price) AS sum_yes,
                MAX(yes_price) AS max_yes,
                MIN(yes_price) AS min_yes
            FROM event_markets
            GROUP BY event_id, hours_to_resolve
        )
        SELECT
            es.*, em.condition_id, em.category, em.yes_won, em.yes_price
        FROM event_anchor_stats es
        JOIN event_markets em
          ON em.event_id = es.event_id
         AND em.hours_to_resolve = es.hours_to_resolve
        WHERE es.n_markets BETWEEN {EVENT_SIZE_MIN} AND {EVENT_SIZE_MAX}
          AND es.n_yes_won = 1   -- only events with exactly 1 winner (mutex sanity)
          AND ABS(es.sum_yes - 1.0) >= {DEVIATION_THRESHOLD}
    """).df()

    print(f"  Ladder events × anchors: {len(df):,}")

    df['dev'] = df['sum_yes'] - 1.0
    df_first = df.sort_values('hours_to_resolve').groupby('event_id').first().reset_index()
    print(f"  Distinct events (first anchor): {len(df_first):,}")
    return df, df_first


def stage2_build_signals(df, df_first):
    """Per event = ОДИН сигнал на target market."""
    print("\n[2/4] Building signals (one per event)...")
    signals = []
    for _, sig_evt in df_first.iterrows():
        evt_id = sig_evt['event_id']
        anchor_h = sig_evt['hours_to_resolve']
        dev = sig_evt['dev']
        sub = df[(df['event_id'] == evt_id) & (df['hours_to_resolve'] == anchor_h)]
        if dev > 0:
            target_rows = sub[sub['yes_price'] == sub['max_yes']]
            if len(target_rows) == 0:
                continue
            target = target_rows.iloc[0]
            side, cost = 'NO', 1 - target['yes_price']
        else:
            target_rows = sub[sub['yes_price'] == sub['min_yes']]
            if len(target_rows) == 0:
                continue
            target = target_rows.iloc[0]
            side, cost = 'YES', target['yes_price']
        signals.append({
            'event_id': evt_id, 'category': target['category'],
            'side': side, 'cost': cost, 'yes_won': target['yes_won'],
            'dev': dev, 'anchor_h': anchor_h,
        })

    sig_df = pd.DataFrame(signals)
    print(f"  Signals: {len(sig_df):,}")
    return sig_df


def stage2_diagnostics(sig_df):
    """Mirror check + per-side WR."""
    print("\n[3/4] Diagnostics — mirror check (NO vs YES)...")
    sigs_over = sig_df[sig_df['dev'] > 0]    # sum > 1 → fade NO
    sigs_under = sig_df[sig_df['dev'] < 0]   # sum < 1 → fade YES

    wr_no = (1 - sigs_over['yes_won']).mean() if len(sigs_over) else 0
    wr_yes = sigs_under['yes_won'].mean() if len(sigs_under) else 0

    print(f"  NO  side (sum>1, fade max yes_price):  n={len(sigs_over):,} WR={wr_no:.1%}")
    print(f"  YES side (sum<1, fade min yes_price):  n={len(sigs_under):,} WR={wr_yes:.1%}")

    if len(sigs_over) and len(sigs_under):
        roi_no_avg = sigs_over.apply(
            lambda r: (1 / r['cost']) - 1 if r['yes_won'] == 0 else -1, axis=1
        ).mean()
        roi_yes_avg = sigs_under.apply(
            lambda r: (1 / r['cost']) - 1 if r['yes_won'] == 1 else -1, axis=1
        ).mean()
        print(f"  NO  raw ROI avg: {roi_no_avg:+.2%}")
        print(f"  YES raw ROI avg: {roi_yes_avg:+.2%}")
        if abs(roi_no_avg) > 0.01 and abs(roi_yes_avg) > 0.01:
            mirror_pct = abs(roi_no_avg - roi_yes_avg) / max(abs(roi_no_avg), abs(roi_yes_avg))
            print(f"  Mirror gap: {mirror_pct*100:.0f}% "
                  f"({'✅ symmetric' if mirror_pct < 0.50 else '❌ asymmetric — bias not arb'})")
    return wr_no, wr_yes


def stage2_5_execution_sim(sig_df, wr_no, wr_yes):
    """Execution simulator с EV-aware sizing."""
    print("\n[4/4] Execution sim...")
    bankroll = SHADOW_BANKROLL
    trades = []
    skip = {}

    for _, sig in sig_df.iterrows():
        cost = sig['cost']
        side = sig['side']
        p_cal = wr_no if side == 'NO' else wr_yes
        if not (0 < p_cal < 1):
            continue
        if cost >= 0.95 or cost <= 0.01:
            skip['cost_oob'] = skip.get('cost_oob', 0) + 1
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
        won = (side == 'YES' and sig['yes_won'] == 1) or (side == 'NO' and sig['yes_won'] == 0)
        gross = (stake / cost) - stake if won else -stake
        fee = stake * (DEFAULT_FEE_PCT * 2 / 100 + DEFAULT_SPREAD_BY_CAT.get(sig['category'], 2.5) / 100)
        net = gross - fee
        trades.append({
            'cat': sig['category'], 'side': side,
            'stake': stake, 'won': won, 'net': net,
        })

    td = pd.DataFrame(trades)
    print(f"  Skipped: {sum(skip.values()):,}")
    for k, v in skip.items():
        print(f"    {k}: {v:,}")

    if not len(td):
        print("  No trades")
        return None

    n_days = 821
    total_stake = td['stake'].sum()
    total_net = td['net'].sum()
    daily = total_net / n_days
    realistic_daily = daily / 2

    print(f"\n  Trades: {len(td):,} WR: {td['won'].mean():.1%}")
    print(f"  Net PnL: ${total_net:+,.2f}, ROI: {total_net/total_stake*100:+.1f}%")
    print(f"  Daily raw: ${daily:+.2f}, realistic (haircut x0.5): ${realistic_daily:+.2f}")
    print(f"  Annual realistic: ${realistic_daily*365:+,.0f}")
    print(f"  Trades/day: {len(td)/n_days:.2f}")

    print(f"\n  Per-side:")
    for s in ['NO', 'YES']:
        sub = td[td['side'] == s]
        if len(sub) == 0:
            continue
        s_st = sub['stake'].sum()
        s_n = sub['net'].sum()
        print(f"    {s} n={len(sub):,} WR={sub['won'].mean():.1%}, "
              f"net=${s_n:+.0f}, ROI={s_n/s_st*100:+.1f}%")

    print(f"\n  Per-category × side:")
    cats = td.groupby(['cat', 'side']).agg(
        n=('won', 'count'), wr=('won', 'mean'),
        stake=('stake', 'sum'), net=('net', 'sum'),
    ).reset_index()
    cats['roi'] = cats['net'] / cats['stake'] * 100
    cats = cats.sort_values('net', ascending=False)
    for _, r in cats.iterrows():
        marker = '✅' if r['net'] > 0 else '❌'
        print(f"    {marker} {r['cat']:15s} {s:4} n={int(r['n']):>3} "
              f"WR={r['wr']:.1%}, net=${r['net']:+.0f}, ROI={r['roi']:+.1f}%")

    return {
        'n': len(td), 'wr': td['won'].mean(),
        'total_stake': total_stake, 'total_net': total_net,
        'daily': daily, 'realistic_daily': realistic_daily,
        'roi': total_net / total_stake if total_stake else 0,
        'df': td,
    }


def stage3_decision(sim, wr_no, wr_yes, n_signals):
    print("\n" + "=" * 70)
    print("STAGE 3: Decision")
    print("=" * 70)

    if sim is None:
        return '❌ FAIL — no trades'

    # Mirror gap
    if wr_no and wr_yes:
        mirror_pct = abs(wr_no - wr_yes) / max(wr_no, wr_yes)
        mirror_pass = mirror_pct < 0.50
    else:
        mirror_pass = False

    print(f"  n_trades = {sim['n']:,} (threshold {N_THRESHOLD})")
    print(f"  realistic_roi = {sim['roi']*0.5*100:+.1f}% (threshold {ROI_THRESHOLD*100:.0f}%)")
    print(f"  realistic_daily = ${sim['realistic_daily']:+.2f}")
    print(f"  mirror_pass = {mirror_pass}")

    if not mirror_pass:
        decision = '❌ FAIL — mirror asymmetric (bias not arb)'
    elif sim['n'] < N_THRESHOLD:
        decision = f'⚠️ INCONCLUSIVE (n={sim["n"]} < {N_THRESHOLD})'
    elif sim['realistic_daily'] > 20:
        decision = '✅ PASS → запуск в shadow'
    elif sim['realistic_daily'] > 5:
        decision = '⚠️ MARGINAL ($5-20/day)'
    else:
        decision = '❌ FAIL (<$5/day realistic)'

    print(f"\n  Decision: {decision}")
    return decision


def main():
    t0 = time.time()
    print("D-NegRiskRebalance — Stage 2 Backtest")
    print(f"Filters: neg_risk=1, exclude {EXCLUDED_CATS}, vol>=${VOL_FLOOR_USD}, "
          f"event_size {EVENT_SIZE_MIN}-{EVENT_SIZE_MAX}")

    if not Path(MARKETS).exists():
        print(f"ERROR: {MARKETS} not found")
        sys.exit(1)

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    df, df_first = stage2_load_signals(con)
    sig_df = stage2_build_signals(df, df_first)
    wr_no, wr_yes = stage2_diagnostics(sig_df)
    sim = stage2_5_execution_sim(sig_df, wr_no, wr_yes)
    decision = stage3_decision(sim, wr_no, wr_yes, len(sig_df))

    out_dir = Path("/root/oracle/products/d-negrisk-rebalance")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# D-NegRiskRebalance — Stage 2 Backtest ({date.today().isoformat()})",
        "",
        f"**Filters:** neg_risk=1, exclude {EXCLUDED_CATS}, vol>=${VOL_FLOOR_USD}",
        f"**Anchors:** {ANCHORS}h · **Deviation:** ≥{DEVIATION_THRESHOLD} · "
        f"**Event size:** {EVENT_SIZE_MIN}-{EVENT_SIZE_MAX}",
        "",
        "## Stage 2 Diagnostics",
        "",
        f"- NO  side n={sum(1 for _ in df_first if (df_first['dev']>0).any()):,}, WR={wr_no:.1%}",
        f"- YES side WR={wr_yes:.1%}",
        f"- Total signals: {len(sig_df):,}",
        "",
        "## Stage 2.5 Execution",
        "",
    ]
    if sim:
        lines.extend([
            f"- Trades: {sim['n']:,}, WR: {sim['wr']:.1%}",
            f"- Net PnL: ${sim['total_net']:+,.2f}, ROI: {sim['roi']*100:+.1f}%",
            f"- Daily realistic: ${sim['realistic_daily']:+.2f}",
        ])
    else:
        lines.append("- No trades")

    lines.extend([
        "",
        "## Stage 3 Decision",
        "",
        f"**{decision}**",
    ])

    out_path = out_dir / "stage2_backtest.md"
    out_path.write_text("\n".join(lines))
    print(f"\nReport saved: {out_path}")
    print(f"Total: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
