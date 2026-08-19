#!/usr/bin/env python3
"""D8 ClusterSpilloverPostFOMC — Canonical test.

Гипотеза: 30-180 мин после FOMC announcement, correlated macro markets (S&P/Fed
rate/recession/CPI) лагают за headline. Edge: после большого price move в окне
2-3h, fade reversal или continue trend.

Симплифицированная версия гипотезы для backtest:
  Триггер: market с macro-keyword (Fed/rate/S&P/CPI/recession/unemployment/GDP),
           hour bucket с **VPIN-style imbalance** (buy_ratio > 70% или < 30%) и
           объём > $5K за час.
  Сигнал: на следующих 4-24h, мы фейдим направление imbalance.
  Outcome: yes_won (final resolve)

Filters:
  - Macro markets ONLY (keyword match)
  - Hour with imbalance > 70% buy OR < 30% buy (signal)
  - Volume in that hour >= $5K
  - At least 2h before resolve (don't trade in last 2h)

Workflow:
  Stage 2: per-bucket backtest (по imbalance direction × time-window)
  Stage 2.5: execution sim
  Stage 3: economics

Запуск: cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/d8_canonical.py
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import duckdb

# ── Constants ────────────────────────────────────────────────────────────────

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
BUCKETS = "/mnt/data/canonical/trade_buckets.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"

# Macro keyword filter (мы хотим только FOMC/macro markets)
MACRO_KEYWORDS = ['fed', 'rate cut', 'rate hike', 'fomc', 'recession', 'S&P',
                  'unemployment', 'inflation', 'CPI', 'GDP', 'jerome powell',
                  'interest rate']

VOL_FLOOR_HOUR_USD = 5_000  # min volume in trigger hour

# Imbalance thresholds
BUY_HIGH = 0.70  # > 70% buy → strong YES sentiment
BUY_LOW = 0.30   # < 30% buy → strong NO sentiment

# Time windows after trigger (hours to resolve)
MIN_HOURS_TO_RESOLVE = 2  # do NOT trade in last 2h
MAX_HOURS_TO_RESOLVE = 24 * 7  # extend to 7 days

# Stage 2 thresholds
ROI_THRESHOLD = 0.30
DSR_THRESHOLD = 1.0
N_THRESHOLD = 200

# Execution sim params
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


def macro_keyword_filter():
    """SQL фрагмент для фильтрации macro markets."""
    conds = []
    for kw in MACRO_KEYWORDS:
        kw_safe = kw.replace("'", "''")
        conds.append(f"m.question ILIKE '%{kw_safe}%'")
    return ' OR '.join(conds)


def stage2_backtest(con):
    """D8 per-bucket: trigger imbalance × time_to_resolve_window × side."""
    print("\n" + "=" * 70)
    print("STAGE 2: D8 backtest — macro markets, imbalance triggers")
    print("=" * 70)

    macro_filter = macro_keyword_filter()

    # Find triggers: hours с (vol >= $5K) и (buy_ratio extreme)
    # Then check outcome based on yes_won
    rows = []
    for trigger_side, lo, hi, fade_side in [
        ('NO_signal_fade_YES', BUY_LOW, 1.01, 'YES'),  # taker selling heavy → fade with YES
        ('YES_signal_fade_NO', -0.01, BUY_LOW, 'NO'),   # taker selling heavy hour → unconfirmed
    ]:
        pass  # placeholder

    # Simpler design: fade direction of imbalance
    # buy_ratio > 0.70 → fade with NO (sell side)
    # buy_ratio < 0.30 → fade with YES (buy side)

    for trigger_label, bf_lo, bf_hi, fade_side, expected_p_factor in [
        ('strong_buy>70%', BUY_HIGH, 1.01, 'NO', 0.30),  # buyers heavy → NO win
        ('strong_sell<30%', -0.01, BUY_LOW, 'YES', 0.30),  # sellers heavy → YES win
    ]:
        for hours_window_min, hours_window_max, label in [
            (2, 4, '2-4h'), (4, 12, '4-12h'),
            (12, 24, '12-24h'), (24, 72, '1-3d'),
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
                        b.total_volume_usd AS hour_volume,
                        b.vwap_yes AS hour_vwap,
                        b.buy_volume / NULLIF(b.buy_volume + b.sell_volume, 0) AS buy_ratio,
                        EXTRACT(EPOCH FROM (m.end_date - b.hour_bucket)) / 3600.0 AS hours_to_resolve
                    FROM read_parquet('{BUCKETS}') b
                    JOIN read_parquet('{MARKETS}') m ON m.condition_id = b.condition_id
                    WHERE ({macro_filter})
                      AND m.yes_won IS NOT NULL
                      AND b.total_volume_usd >= {VOL_FLOOR_HOUR_USD}
                      AND b.buy_volume + b.sell_volume > 0
                      AND m.lifetime_volume_usd >= 5000
                ),
                filtered AS (
                    SELECT *,
                        -- Cost = entry price for our side
                        CASE WHEN '{fade_side}' = 'YES' THEN hour_vwap ELSE 1 - hour_vwap END AS cost,
                        -- Win if our side wins
                        CASE WHEN '{fade_side}' = 'YES' THEN yes_won ELSE 1 - yes_won END AS win
                    FROM triggers
                    WHERE buy_ratio BETWEEN {bf_lo} AND {bf_hi}
                      AND hours_to_resolve BETWEEN {hours_window_min} AND {hours_window_max}
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
            # Approximate fee: 3% (entry + exit) + 2% spread
            fee_estimate = 0.05
            net_roi = avg_roi - fee_estimate

            rows.append({
                'trigger': trigger_label,
                'window': label,
                'fade_side': fade_side,
                'n': n,
                'wr': wr or 0,
                'avg_cost': avg_cost or 0,
                'raw_roi': avg_roi,
                'net_roi': net_roi,
                'dsr': dsr,
            })

    print(f"\n{'trigger':18} {'window':8} {'side':4} {'n':>6} {'avg_cost':>9} "
          f"{'WR':>6} {'raw_ROI':>8} {'net_ROI':>8} {'DSR':>6}")
    print("-" * 90)
    for r in rows:
        thr = []
        if r['net_roi'] > ROI_THRESHOLD: thr.append('ROI✅')
        if r['dsr'] > DSR_THRESHOLD: thr.append('DSR✅')
        if r['n'] >= N_THRESHOLD: thr.append('N✅')
        print(f"{r['trigger']:18} {r['window']:8} {r['fade_side']:4} {r['n']:>6,} "
              f"{r['avg_cost']:>9.3f} {r['wr']:>5.1%} {r['raw_roi']:>+7.1%} "
              f"{r['net_roi']:>+7.1%} {r['dsr']:>+5.2f} {' '.join(thr)}")

    return rows


def stage2_5_execution_sim(con):
    print("\n" + "=" * 70)
    print("STAGE 2.5: D8 Execution Simulation")
    print("=" * 70)

    macro_filter = macro_keyword_filter()

    # Pull all D8 trigger candidates
    # ОДИН сигнал per (condition_id, day) — берём первый качественный hour
    signals = con.execute(f"""
        WITH triggers AS (
            SELECT
                b.condition_id, b.hour_bucket, m.end_date, m.category,
                m.lifetime_volume_usd, m.yes_won,
                b.total_volume_usd AS hour_vol,
                b.vwap_yes AS hour_vwap,
                b.buy_volume / NULLIF(b.buy_volume + b.sell_volume, 0) AS buy_ratio,
                EXTRACT(EPOCH FROM (m.end_date - b.hour_bucket)) / 3600.0 AS hrs_to_resolve
            FROM read_parquet('{BUCKETS}') b
            JOIN read_parquet('{MARKETS}') m ON m.condition_id = b.condition_id
            WHERE ({macro_filter})
              AND m.yes_won IS NOT NULL
              AND b.total_volume_usd >= {VOL_FLOOR_HOUR_USD}
              AND b.buy_volume + b.sell_volume > 0
              AND m.lifetime_volume_usd >= 5000
        ),
        ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (PARTITION BY condition_id ORDER BY hour_bucket) AS rn
            FROM triggers
            WHERE (buy_ratio >= 0.70 OR buy_ratio <= 0.30)
              AND hrs_to_resolve BETWEEN 2 AND 168
              AND hour_vwap > 0.01 AND hour_vwap < 0.99
        )
        SELECT * FROM ranked WHERE rn = 1
    """).df()
    print(f"  Candidates (one per market): {len(signals):,}")

    if len(signals) == 0:
        return {'verdict': 'FAIL — no triggers'}

    # Empirical WR per (trigger_direction, fade_side)
    sigs_buyhi = signals[signals['buy_ratio'] >= 0.70]
    sigs_buylo = signals[signals['buy_ratio'] <= 0.30]
    wr_no_after_buyhi = (1 - sigs_buyhi['yes_won']).mean()  # NO wins after buy-heavy hour
    wr_yes_after_buylo = sigs_buylo['yes_won'].mean()  # YES wins after sell-heavy hour
    print(f"  Empirical WR after buy>70% (fade NO): {wr_no_after_buyhi:.1%}")
    print(f"  Empirical WR after sell<30% (fade YES): {wr_yes_after_buylo:.1%}")

    bankroll = SHADOW_BANKROLL  # use shadow for first sim
    trades = []
    skip_reasons = {}

    for _, sig in signals.iterrows():
        if sig['buy_ratio'] >= 0.70:
            fade_side = 'NO'
            cost = 1 - sig['hour_vwap']
            p_cal = wr_no_after_buyhi
        elif sig['buy_ratio'] <= 0.30:
            fade_side = 'YES'
            cost = sig['hour_vwap']
            p_cal = wr_yes_after_buylo
        else:
            continue

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
            'category': sig['category'],
            'fade_side': fade_side,
            'cost': cost,
            'stake': stake,
            'won': won,
            'gross': gross,
            'fee': fee,
            'net': net,
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
    print(f"  Daily PnL (raw): ${daily_pnl:+.2f}, realistic: ${daily_pnl/2:+.2f}")
    print(f"  Trades/day: {n_total/n_days:.2f}")

    # Per-side
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

    # Per-category
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

    # Try with $5K bankroll for live-ready
    print(f"\n  --- with bankroll $5000 (live-ready) ---")
    df['stake_5k'] = df['stake'] * 5  # linear scaling
    df['net_5k'] = df['net'] * 5
    daily_5k = df['net_5k'].sum() / n_days
    print(f"  Daily raw: ${daily_5k:+.2f}, realistic: ${daily_5k/2:+.2f}, "
          f"Annual realistic: ${daily_5k/2*365:+.0f}")

    return {
        'n_trades': n_total,
        'n_won': n_won,
        'total_stake': total_stake,
        'total_net': total_net,
        'daily_pnl': daily_pnl,
        'daily_pnl_5k': daily_5k,
        'roi': total_net / total_stake if total_stake > 0 else 0,
        'df': df,
    }


def stage3_economics(sim):
    print("\n" + "=" * 70)
    print("STAGE 3: D8 Economics")
    print("=" * 70)

    if 'df' not in sim:
        print(f"  No trades")
        return {}

    daily_5k_realistic = sim['daily_pnl_5k'] / 2
    print(f"  Daily realistic ($5K bankroll): ${daily_5k_realistic:+.2f}")
    print(f"  Annual realistic: ${daily_5k_realistic * 365:+.0f}")

    if daily_5k_realistic > 20:
        decision = '✅ PASS → запуск в shadow с stake $5'
    elif daily_5k_realistic > 5:
        decision = '⚠️ MARGINAL ($5-20) → продолжать backtest'
    else:
        decision = '❌ FAIL (<$5) → KILL'
    print(f"\n  Decision: {decision}")
    return {'decision': decision}


def main():
    t0 = time.time()
    print("D8 ClusterSpilloverPostFOMC — Canonical Test")
    print(f"Filter: macro keywords ({len(MACRO_KEYWORDS)} terms)")
    print(f"Trigger: hour with vol>${VOL_FLOOR_HOUR_USD} and (buy>={BUY_HIGH} or buy<={BUY_LOW})")

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    stage2 = stage2_backtest(con)
    sim = stage2_5_execution_sim(con)
    econ = stage3_economics(sim)

    out_dir = Path("/root/oracle/products/d8-cluster-spillover-fomc")
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# D8 ClusterSpilloverPostFOMC — Canonical Test ({date.today().isoformat()})",
        "",
        "**Hypothesis:** В macro markets (Fed/rate/S&P/CPI/recession), час с экстремальным "
        "buy-imbalance (>70% или <30%) предсказывает следующее direction reversal.",
        "",
        "## Stage 2 Per-bucket",
        "",
        "| trigger | window | side | n | WR | raw_ROI | net_ROI | DSR |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in stage2:
        lines.append(f"| {r['trigger']} | {r['window']} | {r['fade_side']} | {r['n']:,} | "
                     f"{r['wr']:.1%} | {r['raw_roi']:+.1%} | {r['net_roi']:+.1%} | {r['dsr']:+.2f} |")

    lines.extend([
        "",
        "## Stage 2.5",
        "",
        f"- Trades: {sim.get('n_trades', 0):,}",
        f"- ROI: {sim.get('roi', 0)*100:+.1f}%",
        f"- Daily PnL ($1K bankroll, raw): ${sim.get('daily_pnl', 0):+.2f}",
        f"- Daily PnL ($5K bankroll, realistic): ${sim.get('daily_pnl_5k', 0)/2:+.2f}",
        "",
        "## Decision",
        "",
        econ.get('decision', 'N/A'),
    ])

    out_path = out_dir / "canonical_test.md"
    out_path.write_text("\n".join(lines))
    print(f"\nReport: {out_path}")
    print(f"Total: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
