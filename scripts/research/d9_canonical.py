#!/usr/bin/env python3
"""D9 OffHoursThinBookFade — Canonical test.

Гипотеза: в off-hours UTC (00:00-12:00) markets с большим price-deviation от
prev-24h VWAP мean-reverts когда US prime-time (14:00-23:00) возвращается.

Trigger:
  - off-hours bucket (hour_utc ∈ [0, 12])
  - market vol_24h $5K-$50K (mid-cap, не longshot и не whale)
  - bucket vwap отклонился от prev-24h-VWAP на 5%+
  - entry_price ∈ [0.20, 0.80] (не extreme)
  - NOT Sports (sports calendars active US weekend, не off-hours)

Signal:
  - Side = mean-revert direction (если price выше VWAP → fade YES; ниже → fade NO)
  - Hold 4-12h (до US prime-time)

Выход:
  - Per-bucket attribution
  - Per-side execution simulation
  - Stage 3 economics
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import duckdb

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
BUCKETS = "/mnt/data/canonical/trade_buckets.parquet"

# Trigger filters
OFF_HOURS_START = 0
OFF_HOURS_END = 12  # exclusive
DEVIATION_THRESHOLD = 0.05  # 5% от VWAP
PRICE_FLOOR = 0.20
PRICE_CEILING = 0.80
MIN_BUCKET_VOL = 1000  # USD in trigger hour

# Hold window (looking ahead — но НЕ leakage потому что мы knows только prev VWAP)
HOLD_HOURS_MIN = 4
HOLD_HOURS_MAX = 12

EXCLUDED_CATS = ['Sports']  # active weekend → not off-hours mechanism

# Stage thresholds
ROI_THRESHOLD = 0.30
DSR_THRESHOLD = 1.0
N_THRESHOLD = 200

# Execution sim
SHADOW_BANKROLL = 1000.0
LIVE_BANKROLL = 5000.0
FRACTIONAL_KELLY_COLD = 0.10
TOTAL_EDGE_THRESHOLD = 7.0
ENTRY_PRICE_CEILING = 0.95
MAX_STAKE_PCT = 0.005
DEFAULT_FEE_PCT = 1.5
DEFAULT_SPREAD_BY_CAT = {
    'Crypto': 1.0, 'Politics': 1.5, 'Sports': 1.0, 'Tech & AI': 2.0,
    'Weather': 3.0, 'Geopolitics': 2.5, 'Other': 2.0, 'Culture': 2.0,
    'Finance': 1.5, 'Minute Markets': 4.0, None: 2.5,
}


def stage2_backtest(con):
    """Per-deviation × side bucket backtest."""
    print("\n" + "=" * 70)
    print("STAGE 2: D9 backtest — off-hours mean-reversion")
    print("=" * 70)

    excl_str = "', '".join(EXCLUDED_CATS)

    rows = []
    for fade_side, dev_lo, dev_hi, label in [
        # YES too high → fade with NO (price decreases mean-revert)
        ('NO', DEVIATION_THRESHOLD, 0.20, '5-20%_above'),
        ('NO', 0.20, 1.0, '20%+_above'),
        # YES too low → fade with YES (price increases mean-revert)
        ('YES', -0.20, -DEVIATION_THRESHOLD, '5-20%_below'),
        ('YES', -1.0, -0.20, '20%+_below'),
    ]:
        r = con.execute(f"""
            WITH triggers AS (
                SELECT
                    b.condition_id,
                    b.hour_bucket,
                    m.end_date,
                    m.category,
                    m.lifetime_volume_usd,
                    m.yes_won,
                    b.vwap_yes,
                    b.total_volume_usd AS hour_vol,
                    -- Prev 24h VWAP (excluding current hour)
                    AVG(b.vwap_yes) OVER (
                        PARTITION BY b.condition_id
                        ORDER BY b.hour_bucket
                        ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
                    ) AS prev_24h_vwap,
                    EXTRACT(HOUR FROM b.hour_bucket) AS hour_utc
                FROM read_parquet('{BUCKETS}') b
                JOIN read_parquet('{MARKETS}') m ON m.condition_id = b.condition_id
                WHERE m.yes_won IS NOT NULL
                  AND b.total_volume_usd >= {MIN_BUCKET_VOL}
                  AND b.vwap_yes BETWEEN {PRICE_FLOOR} AND {PRICE_CEILING}
                  AND m.category NOT IN ('{excl_str}')
                  AND m.lifetime_volume_usd BETWEEN 5000 AND 50000
            ),
            with_deviation AS (
                SELECT *,
                    (vwap_yes - prev_24h_vwap) / NULLIF(prev_24h_vwap, 0) AS deviation,
                    EXTRACT(EPOCH FROM (end_date - hour_bucket)) / 3600.0 AS hours_to_resolve
                FROM triggers
                WHERE prev_24h_vwap IS NOT NULL
                  AND hour_utc BETWEEN {OFF_HOURS_START} AND {OFF_HOURS_END - 1}
            ),
            filtered AS (
                SELECT *,
                    CASE WHEN '{fade_side}' = 'YES' THEN vwap_yes
                         ELSE 1 - vwap_yes END AS cost,
                    CASE WHEN '{fade_side}' = 'YES' THEN yes_won
                         ELSE 1 - yes_won END AS win
                FROM with_deviation
                WHERE deviation BETWEEN {dev_lo} AND {dev_hi}
                  AND hours_to_resolve BETWEEN {HOLD_HOURS_MIN} AND {HOLD_HOURS_MAX}
            ),
            with_roi AS (
                SELECT *,
                    CASE WHEN win = 1 THEN (1.0 / NULLIF(cost, 0)) - 1 ELSE -1.0 END AS roi
                FROM filtered
                WHERE cost > 0.01 AND cost < 0.99
            )
            SELECT
                COUNT(*) AS n,
                AVG(win::DOUBLE) AS wr,
                AVG(roi) AS avg_roi,
                STDDEV(roi) AS std_roi,
                AVG(cost) AS avg_cost
            FROM with_roi
        """).fetchone()

        n, wr, avg_roi, std_roi, avg_cost = r
        if n is None or n == 0:
            continue
        std_roi = std_roi or 0
        dsr = (avg_roi / std_roi) * (max(n, 50) ** 0.5) / 2.0 if std_roi > 0 else 0
        fee = 0.05
        net_roi = avg_roi - fee
        rows.append({
            'deviation': label, 'side': fade_side, 'n': n, 'wr': wr or 0,
            'avg_cost': avg_cost or 0, 'raw_roi': avg_roi, 'net_roi': net_roi, 'dsr': dsr,
        })

    print(f"\n{'deviation':14} {'side':4} {'n':>6} {'avg_cost':>9} {'WR':>6} "
          f"{'raw_ROI':>8} {'net_ROI':>8} {'DSR':>6}")
    print("-" * 80)
    for r in rows:
        thr = []
        if r['net_roi'] > ROI_THRESHOLD: thr.append('ROI✅')
        if r['dsr'] > DSR_THRESHOLD: thr.append('DSR✅')
        if r['n'] >= N_THRESHOLD: thr.append('N✅')
        print(f"{r['deviation']:14} {r['side']:4} {r['n']:>6,} {r['avg_cost']:>9.3f} "
              f"{r['wr']:>5.1%} {r['raw_roi']:>+7.1%} {r['net_roi']:>+7.1%} "
              f"{r['dsr']:>+5.2f} {' '.join(thr)}")

    return rows


def stage2_5_execution_sim(con):
    print("\n" + "=" * 70)
    print("STAGE 2.5: D9 Execution Simulation")
    print("=" * 70)

    excl_str = "', '".join(EXCLUDED_CATS)
    signals = con.execute(f"""
        WITH triggers AS (
            SELECT
                b.condition_id, b.hour_bucket, m.end_date, m.category,
                m.lifetime_volume_usd, m.yes_won,
                b.vwap_yes, b.total_volume_usd AS hour_vol,
                AVG(b.vwap_yes) OVER (
                    PARTITION BY b.condition_id
                    ORDER BY b.hour_bucket
                    ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
                ) AS prev_24h_vwap,
                EXTRACT(HOUR FROM b.hour_bucket) AS hour_utc
            FROM read_parquet('{BUCKETS}') b
            JOIN read_parquet('{MARKETS}') m ON m.condition_id = b.condition_id
            WHERE m.yes_won IS NOT NULL
              AND b.total_volume_usd >= {MIN_BUCKET_VOL}
              AND b.vwap_yes BETWEEN {PRICE_FLOOR} AND {PRICE_CEILING}
              AND m.category NOT IN ('{excl_str}')
              AND m.lifetime_volume_usd BETWEEN 5000 AND 50000
        ),
        ranked AS (
            SELECT *,
                (vwap_yes - prev_24h_vwap) / NULLIF(prev_24h_vwap, 0) AS deviation,
                EXTRACT(EPOCH FROM (end_date - hour_bucket)) / 3600.0 AS hrs,
                ROW_NUMBER() OVER (PARTITION BY condition_id ORDER BY hour_bucket) AS rn
            FROM triggers
            WHERE prev_24h_vwap IS NOT NULL
              AND hour_utc BETWEEN {OFF_HOURS_START} AND {OFF_HOURS_END - 1}
        )
        SELECT * FROM ranked
        WHERE rn = 1
          AND ABS(deviation) >= {DEVIATION_THRESHOLD}
          AND hrs BETWEEN {HOLD_HOURS_MIN} AND {HOLD_HOURS_MAX}
    """).df()
    print(f"  Candidates: {len(signals):,}")

    if len(signals) == 0:
        return {'verdict': 'FAIL — no triggers'}

    # Empirical WR per direction
    sigs_above = signals[signals['deviation'] > 0]  # price above VWAP → fade NO
    sigs_below = signals[signals['deviation'] < 0]  # price below VWAP → fade YES
    wr_no = (1 - sigs_above['yes_won']).mean() if len(sigs_above) else 0
    wr_yes = sigs_below['yes_won'].mean() if len(sigs_below) else 0
    print(f"  Empirical WR fade NO (price above VWAP): {wr_no:.1%}")
    print(f"  Empirical WR fade YES (price below VWAP): {wr_yes:.1%}")

    bankroll = SHADOW_BANKROLL
    trades = []
    skip_reasons = {}

    for _, sig in signals.iterrows():
        if sig['deviation'] > 0:
            fade_side = 'NO'
            cost = 1 - sig['vwap_yes']
            p_cal = wr_no
        else:
            fade_side = 'YES'
            cost = sig['vwap_yes']
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

        won = (fade_side == 'YES' and sig['yes_won'] == 1) or (fade_side == 'NO' and sig['yes_won'] == 0)
        gross = (stake / cost) - stake if won else -stake
        fee = stake * (DEFAULT_FEE_PCT * 2 / 100 + DEFAULT_SPREAD_BY_CAT.get(sig['category'], 2.5) / 100)
        net = gross - fee

        trades.append({
            'category': sig['category'], 'fade_side': fade_side,
            'cost': cost, 'stake': stake, 'won': won,
            'gross': gross, 'fee': fee, 'net': net,
        })

    print(f"\n  Skipped: {sum(skip_reasons.values()):,}")
    for r, c in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        print(f"    {r}: {c:,}")

    if not trades:
        return {'verdict': 'FAIL — no trades passed gates'}

    import pandas as pd
    df = pd.DataFrame(trades)
    n_total = len(df)
    n_won = df['won'].sum()
    total_stake = df['stake'].sum()
    total_net = df['net'].sum()
    n_days = 821
    daily_pnl = total_net / n_days

    print(f"\n  Trades: {n_total:,}, WR: {n_won/n_total:.1%}")
    print(f"  Total stake: ${total_stake:,.2f}")
    print(f"  Net PnL: ${total_net:+,.2f}")
    print(f"  ROI: {total_net/total_stake*100:+.1f}%")
    print(f"  Daily raw: ${daily_pnl:+.2f}, realistic: ${daily_pnl/2:+.2f}")
    print(f"  Trades/day: {n_total/n_days:.2f}")

    print(f"\n  Per-side:")
    for side in ['YES', 'NO']:
        sub = df[df['fade_side'] == side]
        if len(sub) == 0:
            continue
        s_stake = sub['stake'].sum()
        s_net = sub['net'].sum()
        roi = s_net / s_stake * 100 if s_stake > 0 else 0
        print(f"    {side}: n={len(sub):,}, WR={sub['won'].mean():.1%}, "
              f"net=${s_net:+.0f}, ROI={roi:+.1f}%")

    print(f"\n  Per-category × side:")
    cats = df.groupby(['category', 'fade_side']).agg(
        n=('won', 'count'), wr=('won', 'mean'),
        stake=('stake', 'sum'), net=('net', 'sum'),
    ).reset_index()
    cats['roi'] = cats['net'] / cats['stake'] * 100
    for _, r in cats.sort_values('net', ascending=False).iterrows():
        marker = '✅' if r['net'] > 0 else '❌'
        print(f"    {marker} {r['category']:15s} {r['fade_side']:4} n={int(r['n']):>4,} "
              f"WR={r['wr']:.1%}, net=${r['net']:+.0f}, ROI={r['roi']:+.1f}%")

    # $5K bankroll scaling
    daily_5k = daily_pnl * 5
    print(f"\n  --- with bankroll $5000 ---")
    print(f"  Daily raw: ${daily_5k:+.2f}, realistic: ${daily_5k/2:+.2f}, "
          f"Annual: ${daily_5k/2*365:+.0f}")

    return {
        'n_trades': n_total, 'total_stake': total_stake, 'total_net': total_net,
        'daily_pnl': daily_pnl, 'daily_pnl_5k': daily_5k,
        'roi': total_net / total_stake if total_stake > 0 else 0, 'df': df,
    }


def stage3_economics(sim):
    print("\n" + "=" * 70)
    print("STAGE 3: D9 Economics")
    print("=" * 70)

    if 'df' not in sim:
        return {}

    daily_5k_realistic = sim['daily_pnl_5k'] / 2
    print(f"  Daily realistic ($5K bankroll): ${daily_5k_realistic:+.2f}")
    print(f"  Annual realistic: ${daily_5k_realistic * 365:+.0f}")

    if daily_5k_realistic > 20:
        decision = '✅ PASS → запуск в shadow'
    elif daily_5k_realistic > 5:
        decision = '⚠️ MARGINAL → продолжать backtest'
    else:
        decision = '❌ FAIL (<$5) → KILL'
    print(f"\n  Decision: {decision}")
    return {'decision': decision}


def main():
    t0 = time.time()
    print("D9 OffHoursThinBookFade — Canonical Test")
    print(f"Trigger: off-hours UTC[{OFF_HOURS_START},{OFF_HOURS_END}], "
          f"|deviation| >= {DEVIATION_THRESHOLD}, mid-cap $5K-50K")

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')
    con.execute("SET temp_directory='/mnt/data/duckdb_tmp'")

    stage2 = stage2_backtest(con)
    sim = stage2_5_execution_sim(con)
    econ = stage3_economics(sim)

    out_dir = Path("/root/oracle/products/d9-off-hours-thin-book-fade")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# D9 OffHoursThinBookFade — Canonical Test ({date.today().isoformat()})",
        "",
        "**Hypothesis:** В off-hours UTC mid-cap markets с price-deviation от prev-24h-VWAP "
        "≥5% mean-reverts когда US prime-time возвращается.",
        "",
        "## Stage 2 Per-bucket",
        "",
        "| deviation | side | n | WR | raw_ROI | net_ROI | DSR |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in stage2:
        lines.append(f"| {r['deviation']} | {r['side']} | {r['n']:,} | {r['wr']:.1%} | "
                     f"{r['raw_roi']:+.1%} | {r['net_roi']:+.1%} | {r['dsr']:+.2f} |")
    lines.extend([
        "",
        "## Stage 2.5",
        f"- Trades: {sim.get('n_trades', 0):,}",
        f"- ROI: {sim.get('roi', 0)*100:+.1f}%",
        f"- Daily PnL ($1K, raw): ${sim.get('daily_pnl', 0):+.2f}",
        f"- Daily PnL ($5K, realistic): ${sim.get('daily_pnl_5k', 0)/2:+.2f}",
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
