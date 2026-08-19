#!/usr/bin/env python3
"""D10 ElectionLadderNotNegRisk — Canonical test.

Гипотеза: в multi-market events (binary/ternary elections) sum(yes_price) ≠ 1.0
из-за first-mover liquidity bias. Когда sum > 1.05 → SHORT overpriced markets
(SELL highest-yes). Когда sum < 0.95 → BUY underpriced (BUY lowest-yes).

Trigger:
  - event с 2-4 markets (mutex: ровно 1 winner)
  - sum(yes_price_at_anchor) ≠ 1.0 на >= 5%
  - все markets с yes_won IS NOT NULL (resolved)
  - exclude neg_risk events (D3 уже covers)

Signal:
  - Если sum > 1.05: ставим NO на market с самым высоким yes_price (overpriced)
  - Если sum < 0.95: ставим YES на market с самым низким yes_price (underpriced)
  - Anchor: 24h до резолва (есть price snapshot)

Adversarial: per-direction симметрия. Если только sum>1 работает, это YES-bias
не структурный arb.
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import duckdb

from _backtest_params import DEFAULT_FEE_PCT, FEE_ROUND_TRIP_PCT, SLIPPAGE_PP as _SLIP, BACKTEST_DAYS, STAKE_USD_AT_5K

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"

ANCHOR_HOURS = 24
EVENT_SIZE_MIN = 2
EVENT_SIZE_MAX = 4

DEVIATION_THRESHOLD = 0.05  # |sum - 1.0| >= 5%

# Stage thresholds
ROI_THRESHOLD = 0.30
DSR_THRESHOLD = 1.0
N_THRESHOLD = 100  # lower than D7b — ladder events rarer

# Execution sim
SHADOW_BANKROLL = 1000.0
LIVE_BANKROLL = 5000.0
FRACTIONAL_KELLY_COLD = 0.10
TOTAL_EDGE_THRESHOLD = 7.0
ENTRY_PRICE_CEILING = 0.95
MAX_STAKE_PCT = 0.005
DEFAULT_SPREAD_BY_CAT = {
    'Crypto': 1.0, 'Politics': 1.5, 'Sports': 1.0, 'Tech & AI': 2.0,
    'Weather': 3.0, 'Geopolitics': 2.5, 'Other': 2.0, 'Culture': 2.0,
    'Finance': 1.5, 'Minute Markets': 4.0, None: 2.5,
}


def find_ladder_events(con):
    """Find resolved multi-market events с sum(yes_price) deviation."""
    print("\n" + "=" * 70)
    print("STAGE 1: Find ladder events (mutex 2-4 markets, sum dev >= 5%)")
    print("=" * 70)

    df = con.execute(f"""
        WITH event_markets AS (
            SELECT
                m.event_id,
                m.condition_id,
                m.category,
                m.lifetime_volume_usd,
                m.neg_risk,
                m.yes_won,
                s.yes_price
            FROM read_parquet('{MARKETS}') m
            JOIN read_parquet('{SNAPS}') s
              ON s.condition_id = m.condition_id
            WHERE s.hours_to_resolve = {ANCHOR_HOURS}
              AND m.yes_won IS NOT NULL
              AND m.event_id IS NOT NULL
              AND m.neg_risk = 0  -- exclude neg-risk (D3 covers)
        ),
        event_stats AS (
            SELECT
                event_id,
                COUNT(*) AS n_markets,
                SUM(yes_won) AS n_yes_won,
                SUM(yes_price) AS sum_yes_price,
                MAX(yes_price) AS max_yes_price,
                MIN(yes_price) AS min_yes_price,
                STRING_AGG(DISTINCT category, ',') AS cats
            FROM event_markets
            GROUP BY event_id
        )
        SELECT
            es.event_id,
            es.n_markets,
            es.n_yes_won,
            ROUND(es.sum_yes_price::NUMERIC, 4) AS sum_yes,
            ROUND(es.max_yes_price::NUMERIC, 4) AS max_yes,
            ROUND(es.min_yes_price::NUMERIC, 4) AS min_yes,
            es.cats,
            ROUND((es.sum_yes_price - 1.0)::NUMERIC, 4) AS dev
        FROM event_stats es
        WHERE es.n_markets BETWEEN {EVENT_SIZE_MIN} AND {EVENT_SIZE_MAX}
          AND es.n_yes_won = 1  -- mutex events (exactly 1 winner)
          AND ABS(es.sum_yes_price - 1.0) >= {DEVIATION_THRESHOLD}
    """).df()

    print(f"  Total ladder events: {len(df):,}")
    print(f"  sum > 1.05 (overpriced): {(df['dev'] > 0).sum():,}")
    print(f"  sum < 0.95 (underpriced): {(df['dev'] < 0).sum():,}")
    print(f"  Per-size:")
    print(df.groupby('n_markets').size())
    return df


def stage2_backtest(con, ladder_events):
    """Per-direction backtest."""
    print("\n" + "=" * 70)
    print("STAGE 2: D10 per-direction backtest")
    print("=" * 70)

    # Get all markets in ladder events с yes_price + outcome
    event_ids = ladder_events['event_id'].tolist()
    if not event_ids:
        return []

    # Convert to DuckDB-friendly list literal
    event_ids_str = "', '".join(event_ids)

    df = con.execute(f"""
        SELECT
            m.event_id,
            m.condition_id,
            m.category,
            m.yes_won,
            s.yes_price
        FROM read_parquet('{MARKETS}') m
        JOIN read_parquet('{SNAPS}') s
          ON s.condition_id = m.condition_id
        WHERE s.hours_to_resolve = {ANCHOR_HOURS}
          AND m.event_id IN ('{event_ids_str}')
          AND m.yes_won IS NOT NULL
    """).df()

    # Merge with event-level sum_yes
    df = df.merge(ladder_events[['event_id', 'sum_yes', 'max_yes', 'min_yes', 'dev']],
                  on='event_id', how='left')

    rows = []
    for direction, dev_lo, dev_hi, side, label in [
        ('SELL_overpriced', 0.05, 0.20, 'NO', 'sum>1.05_NO_max'),
        ('SELL_overpriced', 0.20, 1.0, 'NO', 'sum>1.20_NO_max'),
        ('BUY_underpriced', -0.20, -0.05, 'YES', 'sum<0.95_YES_min'),
        ('BUY_underpriced', -1.0, -0.20, 'YES', 'sum<0.80_YES_min'),
    ]:
        # Filter events in deviation range
        sub_events = df[(df['dev'] >= dev_lo) & (df['dev'] <= dev_hi)]
        if len(sub_events) == 0:
            continue

        # For each event, take TARGET market based on side:
        # NO side → market with max_yes (overpriced)
        # YES side → market with min_yes (underpriced)
        if side == 'NO':
            target = sub_events[sub_events['yes_price'] == sub_events['max_yes']]
        else:
            target = sub_events[sub_events['yes_price'] == sub_events['min_yes']]

        if len(target) == 0:
            continue

        # Compute ROI
        target = target.copy()
        if side == 'NO':
            target['cost'] = 1 - target['yes_price']
            target['win'] = (target['yes_won'] == 0).astype(int)
        else:
            target['cost'] = target['yes_price']
            target['win'] = target['yes_won']

        target = target[(target['cost'] > 0.01) & (target['cost'] < 0.99)]
        if len(target) == 0:
            continue

        target['roi'] = target.apply(
            lambda r: (1.0 / r['cost']) - 1 if r['win'] == 1 else -1.0, axis=1
        )

        n = len(target)
        wr = target['win'].mean()
        avg_roi = target['roi'].mean()
        std_roi = target['roi'].std() or 0
        avg_cost = target['cost'].mean()
        dsr = (avg_roi / std_roi) * (max(n, 50) ** 0.5) / 2.0 if std_roi > 0 else 0
        # fee per-share: FEE_ROUND_TRIP_PCT/100 + slippage_pp/100/avg_cost
        fee = FEE_ROUND_TRIP_PCT / 100 + _SLIP["D10b"] / 100 / max(avg_cost, 0.01)
        net_roi = avg_roi - fee

        rows.append({
            'direction': direction, 'label': label, 'side': side,
            'n': n, 'wr': wr, 'avg_cost': avg_cost,
            'raw_roi': avg_roi, 'net_roi': net_roi, 'dsr': dsr,
        })

    print(f"\n{'label':22} {'side':4} {'n':>6} {'avg_cost':>9} {'WR':>6} "
          f"{'raw_ROI':>8} {'net_ROI':>8} {'DSR':>6}")
    print("-" * 80)
    for r in rows:
        thr = []
        if r['net_roi'] > ROI_THRESHOLD: thr.append('ROI✅')
        if r['dsr'] > DSR_THRESHOLD: thr.append('DSR✅')
        if r['n'] >= N_THRESHOLD: thr.append('N✅')
        print(f"{r['label']:22} {r['side']:4} {r['n']:>6,} {r['avg_cost']:>9.3f} "
              f"{r['wr']:>5.1%} {r['raw_roi']:>+7.1%} {r['net_roi']:>+7.1%} "
              f"{r['dsr']:>+5.2f} {' '.join(thr)}")

    return rows


def stage2_5_execution_sim(con, ladder_events):
    print("\n" + "=" * 70)
    print("STAGE 2.5: D10 Execution Simulation")
    print("=" * 70)

    if len(ladder_events) == 0:
        return {'verdict': 'FAIL — no triggers'}

    event_ids = ladder_events['event_id'].tolist()
    event_ids_str = "', '".join(event_ids)

    df = con.execute(f"""
        SELECT
            m.event_id, m.condition_id, m.category, m.yes_won, s.yes_price
        FROM read_parquet('{MARKETS}') m
        JOIN read_parquet('{SNAPS}') s ON s.condition_id = m.condition_id
        WHERE s.hours_to_resolve = {ANCHOR_HOURS}
          AND m.event_id IN ('{event_ids_str}')
          AND m.yes_won IS NOT NULL
    """).df()

    df = df.merge(ladder_events[['event_id', 'dev', 'max_yes', 'min_yes']],
                  on='event_id', how='left')

    # Build signals: ONE per event
    signals = []
    for evt_id in df['event_id'].unique():
        sub = df[df['event_id'] == evt_id]
        if len(sub) == 0:
            continue
        dev = sub.iloc[0]['dev']
        if dev > 0:
            target_rows = sub[sub['yes_price'] == sub['max_yes']]
            if len(target_rows) == 0:
                continue
            target = target_rows.iloc[0]
            side = 'NO'
            cost = 1 - target['yes_price']
        else:
            target_rows = sub[sub['yes_price'] == sub['min_yes']]
            if len(target_rows) == 0:
                continue
            target = target_rows.iloc[0]
            side = 'YES'
            cost = target['yes_price']
        signals.append({
            'event_id': evt_id,
            'condition_id': target['condition_id'],
            'category': target['category'],
            'side': side,
            'cost': cost,
            'yes_won': target['yes_won'],
            'dev': dev,
        })

    print(f"  Signals (one per event): {len(signals):,}")
    if not signals:
        return {'verdict': 'FAIL'}

    # Empirical WR per direction
    import pandas as pd
    sig_df = pd.DataFrame(signals)
    sigs_overpriced = sig_df[sig_df['dev'] > 0]
    sigs_underpriced = sig_df[sig_df['dev'] < 0]
    wr_no = (1 - sigs_overpriced['yes_won']).mean() if len(sigs_overpriced) else 0
    wr_yes = sigs_underpriced['yes_won'].mean() if len(sigs_underpriced) else 0
    print(f"  Empirical WR sum>1 fade NO: {wr_no:.1%}")
    print(f"  Empirical WR sum<1 fade YES: {wr_yes:.1%}")

    bankroll = SHADOW_BANKROLL
    trades = []
    skip_reasons = {}

    for sig in signals:
        cost = sig['cost']
        side = sig['side']
        if side == 'NO':
            p_cal = wr_no
        else:
            p_cal = wr_yes
        if not (0 < p_cal < 1):
            skip_reasons['no_p'] = skip_reasons.get('no_p', 0) + 1
            continue
        if cost >= ENTRY_PRICE_CEILING or cost <= 0.01:
            skip_reasons['cost_invalid'] = skip_reasons.get('cost_invalid', 0) + 1
            continue

        payout = 1.0 / cost
        gross_ev = (p_cal * payout - (1 - p_cal)) * 100
        fee_drag = DEFAULT_FEE_PCT * 2
        spread_drag = DEFAULT_SPREAD_BY_CAT.get(sig['category'], 2.5)
        net_ev = gross_ev - fee_drag - spread_drag
        if net_ev < TOTAL_EDGE_THRESHOLD:
            skip_reasons['ev_below'] = skip_reasons.get('ev_below', 0) + 1
            continue

        f_star = (p_cal * payout - (1 - p_cal)) / payout
        if f_star <= 0:
            skip_reasons['neg_kelly'] = skip_reasons.get('neg_kelly', 0) + 1
            continue

        raw_stake = bankroll * FRACTIONAL_KELLY_COLD * f_star
        cap = bankroll * MAX_STAKE_PCT
        stake = round(min(raw_stake, cap), 2)

        won = (side == 'YES' and sig['yes_won'] == 1) or (side == 'NO' and sig['yes_won'] == 0)
        gross = (stake / cost) - stake if won else -stake
        fee = stake * (DEFAULT_FEE_PCT * 2 / 100 + DEFAULT_SPREAD_BY_CAT.get(sig['category'], 2.5) / 100)
        net = gross - fee

        trades.append({
            'category': sig['category'], 'side': side, 'cost': cost,
            'stake': stake, 'won': won, 'gross': gross, 'fee': fee, 'net': net,
        })

    print(f"\n  Skipped: {sum(skip_reasons.values()):,}")
    for r, c in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        print(f"    {r}: {c:,}")

    if not trades:
        return {'verdict': 'FAIL — no trades passed gates'}

    df_trades = pd.DataFrame(trades)
    n_total = len(df_trades)
    n_won = df_trades['won'].sum()
    total_stake = df_trades['stake'].sum()
    total_net = df_trades['net'].sum()
    n_days = BACKTEST_DAYS
    daily_pnl = total_net / n_days

    print(f"\n  Trades: {n_total:,}, WR: {n_won/n_total:.1%}")
    print(f"  Total stake: ${total_stake:,.2f}")
    print(f"  Net PnL: ${total_net:+,.2f}")
    print(f"  ROI: {total_net/total_stake*100:+.1f}%")
    print(f"  Daily raw: ${daily_pnl:+.2f}, realistic: ${daily_pnl/2:+.2f}")
    print(f"  Trades/day: {n_total/n_days:.2f}")

    print(f"\n  Per-side:")
    for side in ['YES', 'NO']:
        sub = df_trades[df_trades['side'] == side]
        if len(sub) == 0:
            continue
        s_stake = sub['stake'].sum()
        s_net = sub['net'].sum()
        roi = s_net / s_stake * 100 if s_stake > 0 else 0
        print(f"    {side}: n={len(sub):,}, WR={sub['won'].mean():.1%}, "
              f"net=${s_net:+.0f}, ROI={roi:+.1f}%")

    daily_5k = daily_pnl * 5
    print(f"\n  --- with bankroll $5000 ---")
    print(f"  Daily realistic: ${daily_5k/2:+.2f}, Annual: ${daily_5k/2*365:+.0f}")

    return {
        'n_trades': n_total, 'total_stake': total_stake, 'total_net': total_net,
        'daily_pnl': daily_pnl, 'daily_pnl_5k': daily_5k,
        'roi': total_net / total_stake if total_stake > 0 else 0, 'df': df_trades,
    }


def stage3_economics(sim):
    print("\n" + "=" * 70)
    print("STAGE 3: D10 Economics")
    print("=" * 70)
    if 'df' not in sim:
        return {}
    daily_5k_realistic = sim['daily_pnl_5k'] / 2
    print(f"  Daily realistic ($5K): ${daily_5k_realistic:+.2f}")
    print(f"  Annual realistic: ${daily_5k_realistic * 365:+.0f}")
    if daily_5k_realistic > 20:
        decision = '✅ PASS → запуск в shadow'
    elif daily_5k_realistic > 5:
        decision = '⚠️ MARGINAL'
    else:
        decision = '❌ FAIL (<$5) → KILL'
    print(f"\n  Decision: {decision}")
    return {'decision': decision}


def main():
    t0 = time.time()
    print("D10 ElectionLadderNotNegRisk — Canonical Test")
    print(f"Anchor: {ANCHOR_HOURS}h, event size {EVENT_SIZE_MIN}-{EVENT_SIZE_MAX}, "
          f"|sum-1.0| >= {DEVIATION_THRESHOLD}, exclude neg_risk")

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")

    ladder_events = find_ladder_events(con)
    if len(ladder_events) == 0:
        print("\n❌ No ladder events found.")
        return

    stage2 = stage2_backtest(con, ladder_events)
    sim = stage2_5_execution_sim(con, ladder_events)
    econ = stage3_economics(sim)

    out_dir = Path("/root/oracle/products/d10-election-ladder-not-negrisk")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# D10 ElectionLadderNotNegRisk — Canonical Test ({date.today().isoformat()})",
        "",
        f"**Hypothesis:** Multi-market mutex events (2-4 markets, ровно 1 winner) с sum(yes_price) ≠ 1.0 "
        f"показывают first-mover liquidity bias. SELL overpriced (sum>1) или BUY underpriced (sum<1).",
        "",
        f"**Excluded:** neg_risk events (D3 уже covers).",
        "",
        f"**Ladder events:** {len(ladder_events):,}",
        "",
        "## Stage 2 Per-direction",
        "",
        "| label | side | n | WR | raw_ROI | net_ROI | DSR |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in stage2:
        lines.append(f"| {r['label']} | {r['side']} | {r['n']:,} | {r['wr']:.1%} | "
                     f"{r['raw_roi']:+.1%} | {r['net_roi']:+.1%} | {r['dsr']:+.2f} |")
    lines.extend([
        "",
        "## Stage 2.5",
        f"- Trades: {sim.get('n_trades', 0):,}",
        f"- ROI: {sim.get('roi', 0)*100:+.1f}%",
        f"- Daily ($5K, realistic): ${sim.get('daily_pnl_5k', 0)/2:+.2f}",
        "",
        "## Decision",
        econ.get('decision', 'N/A'),
    ])
    out_path = out_dir / "canonical_test.md"
    out_path.write_text("\n".join(lines))
    print(f"\nReport: {out_path}")
    print(f"Total: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
