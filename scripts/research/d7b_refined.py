#!/usr/bin/env python3
"""D7b PreResolutionConvergenceFade — REFINED после D7 canonical results.

Гипотеза refined (отличие от D7):
  Когда trade в окне 4-72h до резолва на market vol≥$5K с YES price ∈ [0.85, 0.95],
  открываем NO — фейдим NO-side underpricing (рынок прайсит favorite слишком уверенно).
  ЭТО НЕ pinning, ЭТО favorite-longshot bias на NO-side в high YES price zones.

Filters (на основе D7 canonical results 2026-05-02):
  - ✅ ТОЛЬКО NO side (YES слабый, ROI +4.4%)
  - ❌ Exclude category Culture (both sides убыточны)
  - ❌ Exclude Crypto YES, Politics YES (но мы и так делаем NO-only)
  - ✅ All anchors 4h..72h
  - ✅ Volume floor $5K

Expected daily realistic PnL: $20-40 (D7 was $12.93 realistic из-за YES + Culture noise)

Source: /mnt/data/canonical/markets_clean.parquet + price_snapshots.parquet
Запуск: cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/d7b_refined.py
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import duckdb

from _backtest_params import DEFAULT_FEE_PCT, FEE_ROUND_TRIP_PCT, SLIPPAGE_PP as _SLIP, BACKTEST_DAYS, STAKE_USD_AT_5K

# ── Constants ────────────────────────────────────────────────────────────────

VOL_FLOOR_USD = 5_000
NO_ZONE = (0.85, 0.95)  # YES price range — мы шортим этот side

ANCHORS_HOURS = [4, 8, 12, 24, 48, 72]

# Excluded categories (from D7 results)
EXCLUDED_CATS = ['Culture']

# Stage 2 thresholds
ROI_THRESHOLD = 0.30
DSR_THRESHOLD = 1.0
N_THRESHOLD = 200

# Execution simulation params (mirror app/calibration/ev_sizer.py)
SHADOW_BANKROLL = 1000.0
FRACTIONAL_KELLY_COLD = 0.10
MIN_EDGE_PCT = 5.0
EDGE_BUFFER_PCT = 2.0
TOTAL_EDGE_THRESHOLD = MIN_EDGE_PCT + EDGE_BUFFER_PCT
ENTRY_PRICE_CEILING = 0.95
MAX_STAKE_PCT = 0.005

DEFAULT_SPREAD_BY_CAT = {
    'Crypto': 1.0, 'Politics': 1.5, 'Sports': 1.0, 'Tech & AI': 2.0,
    'Weather': 3.0, 'Geopolitics': 2.5, 'Other': 2.0, 'Culture': 2.0,
    'Finance': 1.5, 'Minute Markets': 4.0, None: 2.5,
}

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"


def stage2_backtest(con):
    """Per-anchor backtest для NO@[0.85,0.90) и NO@[0.90,0.95]."""
    print("\n" + "=" * 70)
    print("STAGE 2: Per-bucket backtest (NO-only, exclude Culture)")
    print("=" * 70)

    excl_str = "', '".join(EXCLUDED_CATS)
    rows = []
    for anchor in ANCHORS_HOURS:
        for lo, hi, label in [(0.85, 0.90, 'NO_0.85-0.90'), (0.90, 0.95, 'NO_0.90-0.95')]:
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
                      AND m.category NOT IN ('{excl_str}')
                ),
                with_outcome AS (
                    SELECT *,
                        CASE WHEN yes_won = 0 THEN (1.0 / NULLIF(1.0 - yes_price, 0)) - 1 ELSE -1.0 END AS roi,
                        1 - yes_won AS win
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
            std_roi = std_roi or 0
            dsr = (avg_roi / std_roi) * (max(n, 50) ** 0.5) / 2.0 if std_roi > 0 else 0
            # fee per-share: FEE_ROUND_TRIP_PCT/100 + slippage_pp/100/avg_no_cost
            # extreme zone (NO_0.90): avg_no_cost≈0.07, normal: avg_no_cost≈0.12
            avg_no_cost = 0.07 if 'NO_0.90' in label else 0.12
            fee = FEE_ROUND_TRIP_PCT / 100 + _SLIP["D7b"] / 100 / avg_no_cost
            net_roi = avg_roi - fee
            rows.append({
                'anchor_h': anchor, 'bucket': label,
                'n': n, 'wr': wr or 0, 'raw_roi': avg_roi,
                'fee': fee, 'net_roi': net_roi, 'dsr': dsr,
            })

    print(f"\n{'anchor':>7} {'bucket':14} {'n':>6} {'WR':>6} {'raw_ROI':>8} "
          f"{'net_ROI':>8} {'DSR':>6}")
    print("-" * 70)
    for r in rows:
        thr = []
        if r['net_roi'] > ROI_THRESHOLD: thr.append('ROI✅')
        if r['dsr'] > DSR_THRESHOLD: thr.append('DSR✅')
        if r['n'] >= N_THRESHOLD: thr.append('N✅')
        thr_str = ' '.join(thr)
        print(f"{r['anchor_h']:>5}h {r['bucket']:14} {r['n']:>6,} {r['wr']:>5.1%} "
              f"{r['raw_roi']:>+7.1%} {r['net_roi']:>+7.1%} {r['dsr']:>+5.2f} {thr_str}")
    return rows


def stage2_5_execution_sim(con):
    print("\n" + "=" * 70)
    print("STAGE 2.5: Execution Simulation (NO-only, exclude Culture)")
    print("=" * 70)

    excl_str = "', '".join(EXCLUDED_CATS)
    signals = con.execute(f"""
        WITH d7b_candidates AS (
            SELECT
                m.condition_id, m.category, m.lifetime_volume_usd,
                m.yes_won, s.yes_price, s.hours_to_resolve,
                ROW_NUMBER() OVER (PARTITION BY m.condition_id ORDER BY s.hours_to_resolve) AS rn
            FROM read_parquet('{MARKETS}') m
            JOIN read_parquet('{SNAPS}') s
              ON s.condition_id = m.condition_id
            WHERE s.hours_to_resolve IN ({','.join(str(h) for h in ANCHORS_HOURS)})
              AND s.yes_price BETWEEN {NO_ZONE[0]} AND {NO_ZONE[1]}
              AND m.yes_won IS NOT NULL
              AND m.lifetime_volume_usd >= {VOL_FLOOR_USD}
              AND m.category NOT IN ('{excl_str}')
        )
        SELECT condition_id, category, lifetime_volume_usd,
               yes_won, yes_price, hours_to_resolve
        FROM d7b_candidates
        WHERE rn = 1
    """).df()
    print(f"  Candidates: {len(signals):,}")

    # Empirical WR per zone (NO win rate)
    wr_lookup = {}
    for lo, hi in [(0.85, 0.90), (0.90, 0.95)]:
        mask = (signals['yes_price'] >= lo) & (signals['yes_price'] <= hi)
        sub = signals[mask]
        if len(sub) > 0:
            wr_lookup[(lo, hi)] = (1 - sub['yes_won']).mean()
    print(f"  Empirical NO WR per zone:")
    for k, v in wr_lookup.items():
        print(f"    {k[0]}-{k[1]}: {v:.1%}")

    bankroll = SHADOW_BANKROLL
    trades = []
    skip_reasons = {}

    for _, sig in signals.iterrows():
        price = sig['yes_price']
        cost = 1 - price  # NO cost

        p_cal = None
        for (lo, hi), wr in wr_lookup.items():
            if lo <= price <= hi:
                p_cal = wr
                break
        if p_cal is None or p_cal <= 0 or p_cal >= 1:
            skip_reasons['no_confidence'] = skip_reasons.get('no_confidence', 0) + 1
            continue

        if cost >= ENTRY_PRICE_CEILING:
            skip_reasons['cost_ceiling'] = skip_reasons.get('cost_ceiling', 0) + 1
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

        won = (sig['yes_won'] == 0)
        gross_pnl = (stake / cost) - stake if won else -stake
        cost_total = stake * (DEFAULT_FEE_PCT * 2 / 100 + DEFAULT_SPREAD_BY_CAT.get(sig['category'], 2.5) / 100)
        net_pnl = gross_pnl - cost_total

        trades.append({
            'category': sig['category'],
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
        return {'verdict': 'FAIL'}

    import pandas as pd
    df = pd.DataFrame(trades)
    n_total = len(df)
    n_won = df['won'].sum()
    total_stake = df['stake'].sum()
    total_net = df['net_pnl'].sum()
    n_days = BACKTEST_DAYS
    daily_pnl = total_net / n_days

    print(f"\n  Trades: {n_total:,}, WR: {n_won/n_total:.1%}")
    print(f"  Total stake: ${total_stake:,.2f}")
    print(f"  Net PnL: ${total_net:+,.2f}")
    print(f"  ROI: {total_net/total_stake*100:+.1f}%")
    print(f"  Daily PnL (raw): ${daily_pnl:+.2f}")

    # Per-category
    print(f"\n  Per-category:")
    cats = df.groupby('category').agg(
        n=('won', 'count'),
        wr=('won', 'mean'),
        stake=('stake', 'sum'),
        net=('net_pnl', 'sum'),
    ).reset_index()
    cats['roi'] = cats['net'] / cats['stake'] * 100
    cats = cats.sort_values('net', ascending=False)
    for _, r in cats.iterrows():
        marker = '✅' if r['net'] > 0 else '❌'
        print(f"    {marker} {r['category']:15s} n={int(r['n']):>4,} WR={r['wr']:.1%}, "
              f"net=${r['net']:+.0f}, ROI={r['roi']:+.1f}%")

    return {
        'n_trades': n_total,
        'n_won': n_won,
        'total_stake': total_stake,
        'total_net': total_net,
        'daily_pnl': daily_pnl,
        'roi': total_net / total_stake if total_stake > 0 else 0,
        'df': df,
    }


def stage3_economics(sim):
    print("\n" + "=" * 70)
    print("STAGE 3: Economics")
    print("=" * 70)

    if 'df' not in sim:
        print("  No trades")
        return {}

    df = sim['df']
    raw_roi = sim['roi']
    realistic_roi = raw_roi * 0.5
    realistic_daily = sim['daily_pnl'] * 0.5

    print(f"  Raw ROI:        {raw_roi*100:+.1f}%")
    print(f"  Realistic ROI:  {realistic_roi*100:+.1f}%")
    print(f"  Daily PnL raw:        ${sim['daily_pnl']:+.2f}")
    print(f"  Daily PnL realistic:  ${realistic_daily:+.2f}")

    avg_stake = df['stake'].mean()
    trades_per_day = len(df) / 821
    print(f"\n  Trades/day: {trades_per_day:.1f}")
    print(f"  Avg stake: ${avg_stake:.2f}")
    print(f"  Daily turnover: ${avg_stake * trades_per_day:.0f}")

    pnl_std = df['net_pnl'].std()
    base_wr = df['won'].mean()
    streak_prob = (1 - base_wr) ** 10
    print(f"\n  Per-trade std: ${pnl_std:.2f}")
    print(f"  Worst 3σ trade: ${-3*pnl_std:.2f}")
    print(f"  10-streak loss: ${-avg_stake*10:.0f} (prob={streak_prob:.4f})")

    # Decision
    print(f"\n  Decision:")
    if realistic_daily > 20:
        decision = '✅ PASS → запуск в shadow с stake $5'
    elif realistic_daily > 5:
        decision = '⚠️ MARGINAL ($5-20) → продолжать backtest'
    else:
        decision = '❌ FAIL (<$5)'
    print(f"  {decision}")

    return {
        'realistic_daily_pnl': realistic_daily,
        'realistic_roi': realistic_roi,
        'decision': decision,
    }


def main():
    t0 = time.time()
    print("D7b PreResolutionConvergenceFade REFINED — Canonical Test")
    print(f"Filters: NO-only, exclude {EXCLUDED_CATS}, vol>=${VOL_FLOOR_USD}")

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    stage2 = stage2_backtest(con)
    sim = stage2_5_execution_sim(con)
    econ = stage3_economics(sim)

    # Save report
    out_dir = Path("/root/oracle/products/d7-pre-resolution-convergence-fade")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# D7b Refined — Canonical Test ({date.today().isoformat()})",
        "",
        f"**Filter:** NO-only, exclude {EXCLUDED_CATS}, vol>=${VOL_FLOOR_USD}",
        "",
        "## Stage 2 Per-bucket",
        "",
        "| anchor | bucket | n | WR | raw_ROI | net_ROI | DSR |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in stage2:
        lines.append(f"| {r['anchor_h']}h | {r['bucket']} | {r['n']:,} | {r['wr']:.1%} | "
                     f"{r['raw_roi']:+.1%} | {r['net_roi']:+.1%} | {r['dsr']:+.2f} |")

    lines.extend([
        "",
        "## Stage 2.5 Execution",
        "",
        f"- Trades: {sim.get('n_trades', 0):,}",
        f"- WR: {sim.get('n_won', 0)/max(sim.get('n_trades', 1), 1):.1%}",
        f"- Net PnL: ${sim.get('total_net', 0):+,.2f}",
        f"- ROI: {sim.get('roi', 0)*100:+.1f}%",
        f"- Daily PnL raw: ${sim.get('daily_pnl', 0):+.2f}",
        "",
        "## Stage 3 Economics",
        "",
        f"- Realistic Daily: **${econ.get('realistic_daily_pnl', 0):+.2f}**",
        f"- Realistic ROI: {econ.get('realistic_roi', 0)*100:+.1f}%",
        f"- **Decision:** {econ.get('decision', 'N/A')}",
    ])
    out_path = out_dir / "d7b_refined.md"
    out_path.write_text("\n".join(lines))
    print(f"\nReport saved: {out_path}")
    print(f"Total: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
