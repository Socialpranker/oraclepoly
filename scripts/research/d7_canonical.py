#!/usr/bin/env python3
"""D7 PreResolutionConvergenceFade — Stage 2/2.5/3 на canonical HF.

Гипотеза: trade за 24-72h до резолва с YES price ∈ [0.85,0.95] ∪ [0.05,0.15]
и market volume ≥ $5K — фейдим экстремальную сторону (pinning effect).

Источники (только canonical, leak-free):
  - markets_clean.parquet   (target: yes_won)
  - price_snapshots.parquet (yes_price на anchor)

Workflow:
  Stage 2: per-bucket backtest (n, WR, ROI, mirror)
  Stage 2.5: execution simulation (real $ PnL)
  Stage 3: economics (capacity, daily PnL, drawdown)

Запуск:
    cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/d7_canonical.py
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import duckdb

# ── Constants ────────────────────────────────────────────────────────────────

VOL_FLOOR_USD = 5_000
NO_ZONE = (0.85, 0.95)
YES_ZONE = (0.05, 0.15)

# Anchor points to test
ANCHORS_HOURS = [4, 8, 12, 24, 48, 72]

# Price buckets
PRICE_BUCKETS = {
    'YES_0.05-0.10': (0.05, 0.10, 'YES'),
    'YES_0.10-0.15': (0.10, 0.15, 'YES'),
    'NO_0.85-0.90':  (0.85, 0.90, 'NO'),
    'NO_0.90-0.95':  (0.90, 0.95, 'NO'),
}

# Fees by zone (from execution simulation)
FEES = {
    '0.05-0.10': 0.07,
    '0.10-0.15': 0.05,
    '0.85-0.90': 0.05,
    '0.90-0.95': 0.07,
}

# Stage 2 thresholds
ROI_THRESHOLD = 0.30
DSR_THRESHOLD = 1.0
N_THRESHOLD = 200
MIRROR_MAX_ASYMM = 0.50

# Execution simulation params (mirror app/calibration/ev_sizer.py)
SHADOW_BANKROLL = 1000.0
FRACTIONAL_KELLY_COLD = 0.10
MIN_EDGE_PCT = 5.0
EDGE_BUFFER_PCT = 2.0
TOTAL_EDGE_THRESHOLD = MIN_EDGE_PCT + EDGE_BUFFER_PCT
ENTRY_PRICE_CEILING = 0.95
MAX_STAKE_PCT = 0.005

DEFAULT_FEE_PCT = 1.5
DEFAULT_SPREAD_BY_CAT = {
    'Crypto': 1.0, 'Politics': 1.5, 'Sports': 1.0, 'Tech & AI': 2.0,
    'Weather': 3.0, 'Geopolitics': 2.5, 'Other': 2.0, 'Culture': 2.0,
    'Finance': 1.5, 'Minute Markets': 4.0, None: 2.5,
}

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"


# ── Stage 2: per-bucket backtest ─────────────────────────────────────────────

def stage2_backtest(con) -> dict:
    """Per-bucket backtest на canonical."""
    print("\n" + "=" * 70)
    print("STAGE 2: Per-bucket backtest")
    print("=" * 70)

    rows = []
    for anchor in ANCHORS_HOURS:
        for bucket, (lo, hi, side) in PRICE_BUCKETS.items():
            r = con.execute(f"""
                WITH signals AS (
                    SELECT
                        m.condition_id, m.category, m.lifetime_volume_usd,
                        m.yes_won, s.yes_price
                    FROM read_parquet('{MARKETS}') m
                    JOIN read_parquet('{SNAPS}') s
                      ON s.condition_id = m.condition_id
                    WHERE s.hours_to_resolve = {anchor}
                      AND s.yes_price BETWEEN {lo} AND {hi}
                      AND m.yes_won IS NOT NULL
                      AND m.lifetime_volume_usd >= {VOL_FLOOR_USD}
                ),
                with_outcome AS (
                    SELECT *,
                        CASE WHEN '{side}' = 'YES' THEN
                            CASE WHEN yes_won = 1 THEN (1.0 / NULLIF(yes_price, 0)) - 1 ELSE -1.0 END
                        ELSE
                            CASE WHEN yes_won = 0 THEN (1.0 / NULLIF(1.0 - yes_price, 0)) - 1 ELSE -1.0 END
                        END AS roi,
                        CASE WHEN '{side}' = 'YES' THEN yes_won
                             ELSE 1 - yes_won END AS win
                    FROM signals
                )
                SELECT
                    COUNT(*) AS n,
                    AVG(win::DOUBLE) AS wr,
                    AVG(roi) AS avg_roi,
                    STDDEV(roi) AS std_roi
                FROM with_outcome
            """).fetchone()

            n, wr, avg_roi, std_roi = r
            if n is None or n == 0:
                continue

            # Halved Sharpe (Harvey-Liu shortcut)
            std_roi = std_roi or 0
            dsr = (avg_roi / std_roi) * (max(n, 50) ** 0.5) / 2.0 if std_roi > 0 else 0

            # Net ROI after fee
            zone_key = bucket.split('_')[1]  # extract '0.05-0.10' etc.
            fee = FEES.get(zone_key, 0.05)
            net_roi = avg_roi - fee

            rows.append({
                'anchor_h': anchor, 'bucket': bucket, 'side': side, 'zone': zone_key,
                'n': n, 'wr': wr or 0, 'raw_roi': avg_roi, 'fee': fee,
                'net_roi': net_roi, 'dsr': dsr,
            })

    # Print table
    print(f"\n{'anchor':>7} {'bucket':14} {'side':4} {'n':>6} {'WR':>6} "
          f"{'raw_ROI':>8} {'net_ROI':>8} {'DSR':>6} {'thresholds':12}")
    print("-" * 90)
    for r in rows:
        thr = []
        if r['net_roi'] > ROI_THRESHOLD: thr.append('ROI✅')
        if r['dsr'] > DSR_THRESHOLD: thr.append('DSR✅')
        if r['n'] >= N_THRESHOLD: thr.append('N✅')
        thr_str = ' '.join(thr) or '—'
        print(f"{r['anchor_h']:>5}h {r['bucket']:14} {r['side']:4} {r['n']:>6,} "
              f"{r['wr']:>5.1%} {r['raw_roi']:>+7.1%} {r['net_roi']:>+7.1%} "
              f"{r['dsr']:>+5.2f} {thr_str:12}")

    return {'buckets': rows}


# ── Adversarial mirror check ─────────────────────────────────────────────────

def adversarial_mirror(buckets) -> dict:
    """NO vs YES symmetry check."""
    print("\n" + "=" * 70)
    print("ADVERSARIAL MIRROR CHECK")
    print("=" * 70)

    # Aggregate per side across all buckets/anchors
    no_buckets = [b for b in buckets if b['side'] == 'NO']
    yes_buckets = [b for b in buckets if b['side'] == 'YES']

    if not no_buckets or not yes_buckets:
        print("Insufficient data for mirror check.")
        return {'pass': False, 'no_roi': 0, 'yes_roi': 0, 'asymmetry': 1.0}

    no_total_n = sum(b['n'] for b in no_buckets)
    no_avg_net = sum(b['net_roi'] * b['n'] for b in no_buckets) / no_total_n
    yes_total_n = sum(b['n'] for b in yes_buckets)
    yes_avg_net = sum(b['net_roi'] * b['n'] for b in yes_buckets) / yes_total_n

    print(f"  NO  side: n={no_total_n:,}, weighted net_ROI={no_avg_net:+.1%}")
    print(f"  YES side: n={yes_total_n:,}, weighted net_ROI={yes_avg_net:+.1%}")

    both_pos = no_avg_net > 0 and yes_avg_net > 0
    if both_pos:
        asymm = abs(no_avg_net - yes_avg_net) / max(no_avg_net, yes_avg_net)
        passed = asymm < MIRROR_MAX_ASYMM
        print(f"  Both positive: ✅")
        print(f"  Asymmetry: {asymm:.0%} (threshold < {MIRROR_MAX_ASYMM:.0%})")
        print(f"  Verdict: {'✅ PASS — symmetric pinning' if passed else '❌ FAIL — favorite-longshot bias, не pinning'}")
    else:
        passed = False
        asymm = 1.0
        print(f"  Both positive: ❌")
        print(f"  Verdict: ❌ FAIL — at least one side негативная, не pinning effect")

    return {'pass': passed, 'no_roi': no_avg_net, 'yes_roi': yes_avg_net, 'asymmetry': asymm}


# ── Stage 2.5: Execution simulation ──────────────────────────────────────────

def stage2_5_execution_sim(con) -> dict:
    """Execution simulation на canonical."""
    print("\n" + "=" * 70)
    print("STAGE 2.5: Execution Simulation (real $ via ev_sizer)")
    print("=" * 70)

    # Pull all D7 signals: anchor=24h is primary trigger window
    # Multiple anchors → один сигнал per market (first valid)
    signals = con.execute(f"""
        WITH d7_candidates AS (
            SELECT
                m.condition_id, m.category, m.lifetime_volume_usd,
                m.yes_won, s.yes_price, s.hours_to_resolve,
                ROW_NUMBER() OVER (PARTITION BY m.condition_id ORDER BY s.hours_to_resolve) AS rn
            FROM read_parquet('{MARKETS}') m
            JOIN read_parquet('{SNAPS}') s
              ON s.condition_id = m.condition_id
            WHERE s.hours_to_resolve IN ({','.join(str(h) for h in ANCHORS_HOURS)})
              AND ((s.yes_price BETWEEN {YES_ZONE[0]} AND {YES_ZONE[1]})
                   OR (s.yes_price BETWEEN {NO_ZONE[0]} AND {NO_ZONE[1]}))
              AND m.yes_won IS NOT NULL
              AND m.lifetime_volume_usd >= {VOL_FLOOR_USD}
        )
        SELECT condition_id, category, lifetime_volume_usd,
               yes_won, yes_price, hours_to_resolve
        FROM d7_candidates
        WHERE rn = 1
    """).df()

    print(f"  Candidates (one per market): {len(signals):,}")

    # Empirical WR per bucket (cold-start confidence)
    wr_lookup = {}
    for bucket, (lo, hi, side) in PRICE_BUCKETS.items():
        mask = (signals['yes_price'] >= lo) & (signals['yes_price'] <= hi)
        sub = signals[mask]
        if len(sub) == 0:
            continue
        if side == 'YES':
            wr_lookup[(side, lo, hi)] = sub['yes_won'].mean()
        else:
            wr_lookup[(side, lo, hi)] = (1 - sub['yes_won']).mean()

    # Simulate each signal through ev_sizer
    bankroll = SHADOW_BANKROLL
    trades = []
    skip_reasons = {}

    for _, sig in signals.iterrows():
        price = sig['yes_price']

        # Determine side
        if YES_ZONE[0] <= price <= YES_ZONE[1]:
            side = 'YES'
            cost = price
        elif NO_ZONE[0] <= price <= NO_ZONE[1]:
            side = 'NO'
            cost = 1 - price
        else:
            continue

        # Cold-start confidence = empirical WR in price bucket
        p_cal = None
        for (s, lo, hi), wr in wr_lookup.items():
            if s == side and lo <= price <= hi:
                p_cal = wr
                break
        if p_cal is None or p_cal <= 0 or p_cal >= 1:
            skip_reasons['no_confidence'] = skip_reasons.get('no_confidence', 0) + 1
            continue

        # Hard ceiling
        if cost >= ENTRY_PRICE_CEILING:
            skip_reasons['cost_ceiling'] = skip_reasons.get('cost_ceiling', 0) + 1
            continue

        # EV check
        payout = 1.0 / cost
        gross_ev = (p_cal * payout - (1 - p_cal)) * 100
        fee_drag = DEFAULT_FEE_PCT * 2
        spread_drag = DEFAULT_SPREAD_BY_CAT.get(sig['category'], 2.5)
        net_ev = gross_ev - fee_drag - spread_drag

        if net_ev < TOTAL_EDGE_THRESHOLD:
            skip_reasons['ev_below_threshold'] = skip_reasons.get('ev_below_threshold', 0) + 1
            continue

        # Kelly fraction
        f_star = (p_cal * payout - (1 - p_cal)) / payout
        if f_star <= 0:
            skip_reasons['neg_kelly'] = skip_reasons.get('neg_kelly', 0) + 1
            continue

        raw_stake = bankroll * FRACTIONAL_KELLY_COLD * f_star
        cap = bankroll * MAX_STAKE_PCT
        stake = round(min(raw_stake, cap), 2)

        # Outcome → PnL
        won = (side == 'YES' and sig['yes_won'] == 1) or (side == 'NO' and sig['yes_won'] == 0)
        gross_pnl = (stake / cost) - stake if won else -stake
        cost_total = stake * (DEFAULT_FEE_PCT * 2 / 100 + DEFAULT_SPREAD_BY_CAT.get(sig['category'], 2.5) / 100)
        net_pnl = gross_pnl - cost_total

        trades.append({
            'condition_id': sig['condition_id'],
            'category': sig['category'],
            'side': side,
            'price': price,
            'cost': cost,
            'stake': stake,
            'won': won,
            'gross_pnl': gross_pnl,
            'fee': cost_total,
            'net_pnl': net_pnl,
        })

    print(f"\n  Skipped: {sum(skip_reasons.values()):,}")
    for r, c in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        print(f"    {r}: {c:,}")

    if not trades:
        print("\n  ❌ No trades passed gates — D7 KILL")
        return {'verdict': 'FAIL', 'daily_pnl': 0}

    import pandas as pd
    trades_df = pd.DataFrame(trades)

    n_total = len(trades_df)
    n_won = trades_df['won'].sum()
    total_stake = trades_df['stake'].sum()
    total_gross = trades_df['gross_pnl'].sum()
    total_fee = trades_df['fee'].sum()
    total_net = trades_df['net_pnl'].sum()

    # Days = 821 (canonical period 2024-01-01 → 2026-03-31)
    n_days = 821
    daily_pnl = total_net / n_days

    print(f"\n  Trades: {n_total:,}, WR: {n_won/n_total:.1%}")
    print(f"  Total stake: ${total_stake:,.2f}")
    print(f"  Gross PnL: ${total_gross:+,.2f}")
    print(f"  Fee+spread: ${total_fee:,.2f}")
    print(f"  Net PnL: ${total_net:+,.2f}")
    print(f"  ROI: {total_net/total_stake*100:+.1f}%")
    print(f"  Daily PnL: ${daily_pnl:+.2f} (over {n_days} days)")

    # Per-side breakdown
    print(f"\n  Per-side:")
    for side in ['YES', 'NO']:
        sub = trades_df[trades_df['side'] == side]
        if len(sub) == 0:
            continue
        s_stake = sub['stake'].sum()
        s_net = sub['net_pnl'].sum()
        s_roi = s_net / s_stake * 100 if s_stake > 0 else 0
        print(f"    {side}: n={len(sub):,}, WR={sub['won'].mean():.1%}, "
              f"net_PnL=${s_net:+.0f}, ROI={s_roi:+.1f}%")

    # Per-category
    print(f"\n  Per-category × side:")
    cats = trades_df.groupby(['category', 'side']).agg(
        n=('won', 'count'),
        wr=('won', 'mean'),
        stake=('stake', 'sum'),
        net=('net_pnl', 'sum'),
    ).reset_index()
    cats['roi'] = cats['net'] / cats['stake'] * 100
    cats = cats.sort_values('net', ascending=False)
    for _, r in cats.iterrows():
        marker = '✅' if r['net'] > 0 else '❌'
        print(f"    {marker} {r['category']:15s} {r['side']:4} n={int(r['n']):>4,} "
              f"WR={r['wr']:.1%}, net=${r['net']:+.0f}, ROI={r['roi']:+.1f}%")

    verdict = '✅ PASS' if daily_pnl > 20 else '⚠️ MARGINAL' if daily_pnl > 5 else '❌ FAIL'
    return {
        'verdict': verdict,
        'daily_pnl': daily_pnl,
        'total_net': total_net,
        'n_trades': n_total,
        'n_days': n_days,
        'roi': total_net / total_stake if total_stake > 0 else 0,
        'trades_df': trades_df,
    }


# ── Stage 3: Economics ───────────────────────────────────────────────────────

def stage3_economics(sim_result, mirror_result) -> dict:
    """Realistic EV + capacity + drawdown."""
    print("\n" + "=" * 70)
    print("STAGE 3: Economics")
    print("=" * 70)

    if 'trades_df' not in sim_result or len(sim_result['trades_df']) == 0:
        print("  No trades — skip economics")
        return {}

    df = sim_result['trades_df']

    # Realistic EV (Harvey-Liu × 0.5 haircut)
    raw_roi = sim_result['roi']
    realistic_roi = raw_roi * 0.5
    print(f"\n  Raw ROI:        {raw_roi*100:+.1f}%")
    print(f"  Realistic ROI:  {realistic_roi*100:+.1f}% (Harvey-Liu × 0.5)")

    # Daily PnL realistic
    daily_pnl = sim_result['daily_pnl']
    realistic_daily = daily_pnl * 0.5
    print(f"  Daily PnL raw:        ${daily_pnl:+.2f}")
    print(f"  Daily PnL realistic:  ${realistic_daily:+.2f}")

    # Capacity
    avg_stake = df['stake'].mean()
    trades_per_day = len(df) / sim_result['n_days']
    print(f"\n  Trades/day: {trades_per_day:.1f}")
    print(f"  Avg stake: ${avg_stake:.2f}")
    print(f"  Daily turnover: ${avg_stake * trades_per_day:.0f}")

    # Worst-case drawdown (3-sigma)
    pnl_std = df['net_pnl'].std()
    worst_3sigma = 3 * pnl_std
    print(f"\n  Per-trade std: ${pnl_std:.2f}")
    print(f"  Worst 3σ trade: ${-worst_3sigma:.2f}")

    # Streak: 10 consecutive losses (worst-case)
    base_wr = df['won'].mean()
    streak_prob = (1 - base_wr) ** 10
    streak_loss = avg_stake * 10
    print(f"  10-streak loss: ${-streak_loss:.0f} (prob={streak_prob:.4f})")

    # Decision
    print(f"\n  Decision:")
    if realistic_daily > 20:
        decision = '✅ PASS → запуск в shadow с stake $5'
    elif realistic_daily > 5:
        decision = '⚠️ MARGINAL ($5-20) → продолжать backtest, не shadow'
    else:
        decision = '❌ FAIL (<$5/day realistic) → kill'

    if not mirror_result['pass']:
        decision += '\n    ⚠️ Adversarial mirror FAIL — не классический pinning, скорее longshot bias'

    print(f"  {decision}")

    return {
        'realistic_daily_pnl': realistic_daily,
        'realistic_roi': realistic_roi,
        'avg_stake': avg_stake,
        'trades_per_day': trades_per_day,
        'worst_3sigma': -worst_3sigma,
        'streak_loss': -streak_loss,
        'decision': decision,
    }


# ── Save report ──────────────────────────────────────────────────────────────

def save_report(stage2, mirror, sim, econ):
    out_dir = Path("/root/oracle/products/d7-pre-resolution-convergence-fade")
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# D7 PreResolutionConvergenceFade — Canonical Test ({date.today().isoformat()})",
        "",
        "**Источники:** `/mnt/data/canonical/markets_clean.parquet` + `price_snapshots.parquet`  ",
        f"**Период:** 2024-01-01 → 2026-03-31 (821 days)  ",
        f"**Filter:** vol≥${VOL_FLOOR_USD:,}, yes_won IS NOT NULL  ",
        "",
        "## Stage 2 — Per-bucket backtest",
        "",
        "| anchor | bucket | side | n | WR | raw_ROI | net_ROI | DSR |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in stage2['buckets']:
        lines.append(f"| {r['anchor_h']}h | {r['bucket']} | {r['side']} | {r['n']:,} | "
                     f"{r['wr']:.1%} | {r['raw_roi']:+.1%} | {r['net_roi']:+.1%} | {r['dsr']:+.2f} |")

    lines.extend([
        "",
        "## Adversarial Mirror Check",
        "",
        f"- NO  net_ROI: **{mirror['no_roi']:+.1%}**",
        f"- YES net_ROI: **{mirror['yes_roi']:+.1%}**",
        f"- Asymmetry: **{mirror['asymmetry']:.0%}** (threshold < 50%)",
        f"- **Verdict:** {'✅ PASS — symmetric pinning' if mirror['pass'] else '❌ FAIL — asymmetric (longshot bias, не pinning)'}",
        "",
        "## Stage 2.5 — Execution Simulation",
        "",
        f"- Bankroll: $1000 (shadow), Kelly cold f=0.10, max 0.5%",
        f"- Trades: {sim.get('n_trades', 0):,}",
        f"- Net PnL: ${sim.get('total_net', 0):+,.2f}",
        f"- ROI: {sim.get('roi', 0)*100:+.1f}%",
        f"- Daily PnL (raw): ${sim.get('daily_pnl', 0):+.2f}",
        f"- **Verdict:** {sim.get('verdict', 'N/A')}",
        "",
        "## Stage 3 — Economics",
        "",
        f"- Realistic Daily PnL (Harvey-Liu): **${econ.get('realistic_daily_pnl', 0):+.2f}**",
        f"- Realistic ROI: {econ.get('realistic_roi', 0)*100:+.1f}%",
        f"- Trades/day: {econ.get('trades_per_day', 0):.1f}",
        f"- Avg stake: ${econ.get('avg_stake', 0):.2f}",
        f"- Worst 3σ trade: ${econ.get('worst_3sigma', 0):.2f}",
        f"- 10-streak loss: ${econ.get('streak_loss', 0):.0f}",
        "",
        f"**Final decision:**  ",
        f"{econ.get('decision', 'N/A')}",
    ])

    out_path = out_dir / "canonical_test.md"
    out_path.write_text("\n".join(lines))
    print(f"\nReport saved: {out_path}")


def main():
    t0 = time.time()
    print("D7 PreResolutionConvergenceFade — Canonical Test")
    print(f"Source: {MARKETS}")
    print(f"Source: {SNAPS}")

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    stage2 = stage2_backtest(con)
    mirror = adversarial_mirror(stage2['buckets'])
    sim = stage2_5_execution_sim(con)
    econ = stage3_economics(sim, mirror)

    save_report(stage2, mirror, sim, econ)
    print(f"\nTotal: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
