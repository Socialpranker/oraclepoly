#!/usr/bin/env python3
"""D-MLCalibratorTopDecile — Stage 2 backtest на canonical HF.

Гипотеза:
  ml_features.parquet (LGB AUC 0.94 на anchor=24h) даёт `predicted_p` per market.
  Top-decile signals по EV (predicted_p far enough from market_yes_price) на mid-price
  [0.20, 0.80] дают ROI ≥ 30% net of fee+slippage.
  Это новый детектор — D7b работает только в [0.85, 0.95]; на mid-price никакой
  detector сейчас не активен.

Differentiation от D7b/D10b:
  - Mid-price [0.20, 0.80] — D7b НЕ touched
  - ML signal ortogonal to longshot bias
  - No new infrastructure — переиспользует ml_features.parquet

Pipeline:
  1. Train LightGBM на anchor=24h, walk-forward split (train ≤ end_date - 7d, test = последняя неделя)
  2. Predict p_yes для test set
  3. Compute EV для NO entry: ev_no = (1 - predicted_p) * (1/(1-yes_price) - 1) - predicted_p
                       YES entry: ev_yes = predicted_p * (1/yes_price - 1) - (1 - predicted_p)
  4. Top-decile по EV (после fee+slippage haircut) = signals
  5. Adversarial: bottom-decile EV должен быть НЕ profitable

Stage 2 thresholds (per docs/detector_lifecycle.md):
  - ROI ≥ 30% net
  - n ≥ 200
  - DSR > 1.0 (Harvey-Liu × 0.5 haircut)
  - Adversarial purity: bottom-decile ROI < 5%

Запуск на VPS:
  cd ~/oracle && /root/oracle/.venv-research/bin/python scripts/research/d_ml_calibrator_topdecile.py
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

# ── Constants ────────────────────────────────────────────────────────────────

ML_FEATURES = "/mnt/data/canonical/ml_features.parquet"
MARKETS = "/mnt/data/canonical/markets_clean.parquet"

ANCHOR_HOURS = 24

# Mid-price zone — explicitly NOT D7b territory
MID_PRICE_MIN = 0.20
MID_PRICE_MAX = 0.80

# Out-of-sample window
OUT_OF_SAMPLE_DAYS = 7

# Stage 2 thresholds
ROI_THRESHOLD = 0.30
DSR_THRESHOLD = 1.0
N_THRESHOLD = 200
ADVERSARIAL_BOTTOM_DECILE_MAX_ROI = 0.05

# Execution params (mirror app/calibration/ev_sizer.py)
SHADOW_BANKROLL = 5000.0
FRACTIONAL_KELLY_COLD = 0.10
MAX_STAKE_PCT = 0.005
MIN_NET_EV_PCT = 7.0  # после fee+slippage

DEFAULT_FEE_PCT = 1.5  # per leg, ×2 = 3% round trip
DEFAULT_SPREAD_BY_CAT = {
    'Crypto': 1.0, 'Politics': 1.5, 'Sports': 1.0, 'Tech & AI': 2.0,
    'Weather': 3.0, 'Geopolitics': 2.5, 'Other': 2.0, 'Culture': 2.0,
    'Finance': 1.5, 'Minute Markets': 4.0, None: 2.5,
}

# Volume floor — exclude совсем тонкие книги
VOL_FLOOR_USD = 5_000


def load_features(con):
    """Load anchor=24h slice, mid-price filter, attach end_date."""
    print(f"\n[1/6] Loading ml_features anchor={ANCHOR_HOURS}h...")
    t0 = time.time()
    df = con.execute(f"""
        SELECT
            f.*,
            m.end_date,
            m.lifetime_volume_usd
        FROM read_parquet('{ML_FEATURES}') f
        JOIN read_parquet('{MARKETS}') m ON m.condition_id = f.condition_id
        WHERE f.anchor_hours = {ANCHOR_HOURS}
          AND f.yes_price_at_anchor BETWEEN {MID_PRICE_MIN} AND {MID_PRICE_MAX}
          AND m.lifetime_volume_usd >= {VOL_FLOOR_USD}
          AND m.yes_won IS NOT NULL
    """).df()
    print(f"  Loaded {len(df):,} rows in {time.time()-t0:.1f}s")
    print(f"  Date range: {df['end_date'].min()} → {df['end_date'].max()}")
    print(f"  Cols: {len(df.columns)}")
    return df


def walk_forward_split(df, days):
    """Train = all but last `days`. Test = last `days` window."""
    cutoff = df['end_date'].max() - pd.Timedelta(days=days)
    train = df[df['end_date'] < cutoff].copy()
    test = df[df['end_date'] >= cutoff].copy()
    print(f"\n[2/6] Walk-forward split (cutoff = {cutoff.date()}):")
    print(f"  Train: {len(train):,} rows, base YES rate = {train['yes_won'].mean():.3f}")
    print(f"  Test : {len(test):,} rows, base YES rate = {test['yes_won'].mean():.3f}")
    return train, test


def fit_predict(train, test):
    """LightGBM fit на train, predict для test. Returns test with `predicted_p`."""
    print("\n[3/6] LightGBM fit + predict...")
    try:
        import lightgbm as lgb
    except ImportError:
        print("  ERROR: lightgbm not installed. Install: pip install lightgbm")
        sys.exit(1)

    # Drop non-feature cols
    drop_cols = ['condition_id', 'event_id', 'yes_won', 'category',
                 'end_date', 'lifetime_volume_usd', 'anchor_hours']
    feat_cols = [c for c in train.columns if c not in drop_cols]
    print(f"  Features: {len(feat_cols)}")

    X_train = train[feat_cols].fillna(0)
    y_train = train['yes_won'].astype(int)
    X_test = test[feat_cols].fillna(0)

    t0 = time.time()
    model = lgb.LGBMClassifier(
        n_estimators=300, max_depth=6, learning_rate=0.05,
        num_leaves=31, min_child_samples=50, verbose=-1,
    )
    model.fit(X_train, y_train)
    train_auc = lgb.plot_metric  # placeholder for hint
    test['predicted_p'] = model.predict_proba(X_test)[:, 1]
    print(f"  Fit + predict: {time.time()-t0:.1f}s")

    # Quick sanity: train AUC vs test AUC
    from sklearn.metrics import roc_auc_score, brier_score_loss
    p_train = model.predict_proba(X_train)[:, 1]
    train_auc = roc_auc_score(y_train, p_train)
    test_auc = roc_auc_score(test['yes_won'].astype(int), test['predicted_p'])
    train_brier = brier_score_loss(y_train, p_train)
    test_brier = brier_score_loss(test['yes_won'].astype(int), test['predicted_p'])
    print(f"  Train AUC={train_auc:.4f} Brier={train_brier:.4f}")
    print(f"  Test  AUC={test_auc:.4f} Brier={test_brier:.4f}")
    if test_auc < 0.55:
        print("  ⚠️ Test AUC < 0.55 — model degraded out-of-sample (concept drift)")
    if abs(train_auc - test_auc) > 0.10:
        print(f"  ⚠️ AUC degradation {train_auc-test_auc:+.3f} — может быть overfit или concept drift")
    return test, {'train_auc': train_auc, 'test_auc': test_auc,
                  'train_brier': train_brier, 'test_brier': test_brier}


def compute_signals(test):
    """For each test row compute EV for both NO and YES entries.
    Pick best side per row (whichever has higher EV).
    Returns df with ev_pct, side, raw_roi, won."""
    print("\n[4/6] Computing signals (EV per row, both sides)...")
    df = test.copy()

    fee_drag = DEFAULT_FEE_PCT * 2
    df['spread_drag'] = df['category'].map(
        lambda c: DEFAULT_SPREAD_BY_CAT.get(c, 2.5)
    )
    cost_drag = fee_drag + df['spread_drag']  # %, total round-trip

    # NO entry: cost = 1 - yes_price, payout = 1/cost
    cost_no = 1 - df['yes_price_at_anchor']
    payout_no = 1 / cost_no
    p_no_win = 1 - df['predicted_p']  # NO wins iff YES loses
    ev_no_pct = (p_no_win * payout_no - (1 - p_no_win)) * 100 - cost_drag

    # YES entry: cost = yes_price, payout = 1/cost
    cost_yes = df['yes_price_at_anchor']
    payout_yes = 1 / cost_yes
    p_yes_win = df['predicted_p']
    ev_yes_pct = (p_yes_win * payout_yes - (1 - p_yes_win)) * 100 - cost_drag

    # Pick best side
    df['ev_no_pct'] = ev_no_pct
    df['ev_yes_pct'] = ev_yes_pct
    df['side'] = np.where(ev_no_pct > ev_yes_pct, 'NO', 'YES')
    df['ev_pct'] = np.where(df['side'] == 'NO', ev_no_pct, ev_yes_pct)
    df['cost'] = np.where(df['side'] == 'NO', cost_no, cost_yes)
    df['payout'] = 1 / df['cost']
    df['p_calibrated'] = np.where(df['side'] == 'NO', p_no_win, p_yes_win)

    # Outcome
    df['won'] = np.where(
        df['side'] == 'NO',
        df['yes_won'] == 0,
        df['yes_won'] == 1
    ).astype(int)
    df['raw_roi'] = np.where(
        df['won'] == 1,
        df['payout'] - 1,
        -1.0
    )
    return df


def stage2_decile_analysis(sig):
    """Bin by EV decile, show ROI per decile.
    Top decile = top 10% by ev_pct.
    Adversarial check: bottom decile should have ROI close to 0 (purity check).
    """
    print("\n[5/6] Stage 2 — Decile analysis...")
    sig = sig.copy()
    sig['ev_decile'] = pd.qcut(
        sig['ev_pct'], q=10, labels=False, duplicates='drop'
    )

    rows = []
    for dec in sorted(sig['ev_decile'].dropna().unique()):
        sub = sig[sig['ev_decile'] == dec]
        if len(sub) == 0:
            continue
        rows.append({
            'decile': int(dec),
            'n': len(sub),
            'wr': sub['won'].mean(),
            'ev_pct_avg': sub['ev_pct'].mean(),
            'raw_roi': sub['raw_roi'].mean(),
            'std_roi': sub['raw_roi'].std(),
            'no_pct': (sub['side'] == 'NO').mean() * 100,
        })

    print(f"\n{'decile':>6} {'n':>6} {'WR':>6} {'EV%':>7} {'rawROI':>8} "
          f"{'stdROI':>8} {'NO%':>5}")
    print("-" * 60)
    for r in rows:
        print(f"{r['decile']:>6} {r['n']:>6,} {r['wr']:>5.1%} {r['ev_pct_avg']:>+6.1f} "
              f"{r['raw_roi']:>+7.1%} {r['std_roi']:>7.2f} {r['no_pct']:>4.0f}%")

    # Adversarial purity: top decile vs bottom decile
    if len(rows) >= 2:
        top = rows[-1]
        bot = rows[0]
        print(f"\n  Top decile  ROI: {top['raw_roi']*100:+.1f}%")
        print(f"  Bot decile  ROI: {bot['raw_roi']*100:+.1f}%")
        purity_pp = (top['raw_roi'] - bot['raw_roi']) * 100
        print(f"  Purity gap: {purity_pp:+.1f}pp")
        if bot['raw_roi'] > ADVERSARIAL_BOTTOM_DECILE_MAX_ROI:
            print(f"  ⚠️ Bottom decile ROI {bot['raw_roi']*100:+.1f}% > "
                  f"{ADVERSARIAL_BOTTOM_DECILE_MAX_ROI*100:+.1f}% — "
                  f"adversarial purity FAIL")
        else:
            print(f"  ✅ Adversarial purity PASS (bot decile ROI < threshold)")

    return rows


def stage2_5_execution_sim(sig):
    """Top-decile = signals → execution sim with EV-aware sizing."""
    print("\n[6/6] Stage 2.5 — Execution sim (top-decile only)...")
    sig = sig.copy()
    sig['ev_decile'] = pd.qcut(
        sig['ev_pct'], q=10, labels=False, duplicates='drop'
    )
    top = sig[sig['ev_decile'] == sig['ev_decile'].max()].copy()
    print(f"  Top-decile signals: {len(top):,}")

    bankroll = SHADOW_BANKROLL
    trades = []
    skip_reasons = {}

    for _, row in top.iterrows():
        cost = row['cost']
        if cost <= 0.01 or cost >= 0.95:
            skip_reasons['cost_oob'] = skip_reasons.get('cost_oob', 0) + 1
            continue

        p_cal = row['p_calibrated']
        if not (0 < p_cal < 1):
            continue

        net_ev_pct = row['ev_pct']
        if net_ev_pct < MIN_NET_EV_PCT:
            skip_reasons['ev_below_threshold'] = skip_reasons.get('ev_below_threshold', 0) + 1
            continue

        payout = row['payout']
        f_star = (p_cal * payout - (1 - p_cal)) / payout
        if f_star <= 0:
            skip_reasons['neg_kelly'] = skip_reasons.get('neg_kelly', 0) + 1
            continue

        raw_stake = bankroll * FRACTIONAL_KELLY_COLD * f_star
        cap = bankroll * MAX_STAKE_PCT
        stake = round(min(raw_stake, cap), 2)

        won = row['won'] == 1
        gross_pnl = (stake / cost) - stake if won else -stake
        spread_pct = DEFAULT_SPREAD_BY_CAT.get(row['category'], 2.5)
        cost_total = stake * (DEFAULT_FEE_PCT * 2 / 100 + spread_pct / 100)
        net_pnl = gross_pnl - cost_total

        trades.append({
            'condition_id': row['condition_id'],
            'category': row['category'],
            'side': row['side'],
            'price': row['yes_price_at_anchor'],
            'cost': cost,
            'predicted_p': row['predicted_p'],
            'p_calibrated': p_cal,
            'ev_pct': net_ev_pct,
            'stake': stake,
            'won': won,
            'gross_pnl': gross_pnl,
            'fee': cost_total,
            'net_pnl': net_pnl,
        })

    print(f"  Skipped: {sum(skip_reasons.values()):,}")
    for r, c in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        print(f"    {r}: {c:,}")

    if not trades:
        print("  No trades — top-decile too thin")
        return None

    df = pd.DataFrame(trades)
    n_total = len(df)
    n_won = df['won'].sum()
    total_stake = df['stake'].sum()
    total_net = df['net_pnl'].sum()
    test_days = OUT_OF_SAMPLE_DAYS
    daily_pnl = total_net / test_days

    print(f"\n  Trades: {n_total:,}, WR: {n_won/n_total:.1%}")
    print(f"  Total stake: ${total_stake:,.2f}")
    print(f"  Net PnL: ${total_net:+,.2f}")
    print(f"  ROI: {total_net/total_stake*100:+.1f}%")
    print(f"  Daily PnL (raw, on test {test_days}d): ${daily_pnl:+.2f}")

    # Per-side
    print(f"\n  Per-side:")
    for s in ['NO', 'YES']:
        sub = df[df['side'] == s]
        if len(sub) == 0:
            continue
        s_st = sub['stake'].sum()
        s_n = sub['net_pnl'].sum()
        print(f"    {s:3} n={len(sub):,} WR={sub['won'].mean():.1%}, "
              f"net=${s_n:+.0f}, ROI={s_n/s_st*100:+.1f}%")

    # Per-category
    print(f"\n  Per-category × side:")
    cats = df.groupby(['category', 'side']).agg(
        n=('won', 'count'), wr=('won', 'mean'),
        stake=('stake', 'sum'), net=('net_pnl', 'sum'),
    ).reset_index()
    cats['roi'] = cats['net'] / cats['stake'] * 100
    cats = cats.sort_values('net', ascending=False)
    for _, r in cats.iterrows():
        marker = '✅' if r['net'] > 0 else '❌'
        print(f"    {marker} {r['category']:15s} {r['side']:4} n={int(r['n']):>3} "
              f"WR={r['wr']:.1%}, net=${r['net']:+.0f}, ROI={r['roi']:+.1f}%")

    return {
        'n_trades': n_total,
        'n_won': n_won,
        'total_stake': total_stake,
        'total_net': total_net,
        'daily_pnl': daily_pnl,
        'roi': total_net / total_stake if total_stake > 0 else 0,
        'df': df,
    }


def stage3_economics(sim, decile_rows, model_metrics):
    print("\n" + "=" * 70)
    print("STAGE 3: Economics + Decision")
    print("=" * 70)

    if sim is None or 'df' not in sim:
        print("  No trades")
        return {'decision': '❌ FAIL — no trades'}

    df = sim['df']
    raw_roi = sim['roi']
    realistic_roi = raw_roi * 0.5  # Harvey-Liu
    realistic_daily = sim['daily_pnl'] * 0.5

    print(f"\n  Model quality:")
    print(f"    Train AUC: {model_metrics['train_auc']:.4f}")
    print(f"    Test  AUC: {model_metrics['test_auc']:.4f}")
    print(f"    Train Brier: {model_metrics['train_brier']:.4f}")
    print(f"    Test  Brier: {model_metrics['test_brier']:.4f}")

    print(f"\n  Returns:")
    print(f"    Raw ROI:        {raw_roi*100:+.1f}%")
    print(f"    Realistic ROI:  {realistic_roi*100:+.1f}%")
    print(f"    Daily raw:        ${sim['daily_pnl']:+.2f}")
    print(f"    Daily realistic:  ${realistic_daily:+.2f}")

    avg_stake = df['stake'].mean()
    n_test_days = OUT_OF_SAMPLE_DAYS
    trades_per_day = len(df) / n_test_days
    print(f"\n  Activity:")
    print(f"    Trades/day: {trades_per_day:.1f}")
    print(f"    Avg stake:  ${avg_stake:.2f}")
    print(f"    Daily turnover: ${avg_stake * trades_per_day:.0f}")

    pnl_std = df['net_pnl'].std()
    base_wr = df['won'].mean()
    streak_prob = (1 - base_wr) ** 10 if base_wr > 0 else 1
    print(f"\n  Risk:")
    print(f"    Per-trade std: ${pnl_std:.2f}")
    print(f"    Worst 3σ trade: ${-3*pnl_std:.2f}")
    print(f"    10-streak loss: ${-avg_stake*10:.0f} (prob={streak_prob:.4f})")

    # Decision
    print(f"\n  Decision tree:")
    bot_dec = decile_rows[0] if decile_rows else None
    adversarial_pass = bot_dec and bot_dec['raw_roi'] <= ADVERSARIAL_BOTTOM_DECILE_MAX_ROI

    decision = None
    if not adversarial_pass:
        decision = '❌ FAIL — adversarial purity check failed (bottom decile profitable)'
    elif sim['n_trades'] < N_THRESHOLD:
        decision = f'⚠️ INCONCLUSIVE (n={sim["n_trades"]} < {N_THRESHOLD}) — extend test window'
    elif realistic_daily > 20 and realistic_roi > ROI_THRESHOLD * 0.5:
        decision = '✅ PASS → запуск в shadow с stake $5'
    elif realistic_daily > 5:
        decision = '⚠️ MARGINAL ($5-20/day) — borderline, рассмотреть после D-NegRisk/D-Pinnacle'
    else:
        decision = '❌ FAIL (<$5/day realistic)'
    print(f"  {decision}")

    return {
        'realistic_daily_pnl': realistic_daily,
        'realistic_roi': realistic_roi,
        'decision': decision,
        'adversarial_pass': adversarial_pass,
    }


def main():
    t0 = time.time()
    print("D-MLCalibratorTopDecile — Canonical Stage 2 Backtest")
    print(f"Anchor: {ANCHOR_HOURS}h | Mid-price: [{MID_PRICE_MIN}, {MID_PRICE_MAX}] | "
          f"Vol floor: ${VOL_FLOOR_USD:,}")

    if not Path(ML_FEATURES).exists():
        print(f"ERROR: {ML_FEATURES} not found")
        sys.exit(1)

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    df = load_features(con)
    if len(df) < 1000:
        print(f"ERROR: too few rows ({len(df):,}) for backtest")
        sys.exit(1)

    train, test = walk_forward_split(df, OUT_OF_SAMPLE_DAYS)
    if len(test) < N_THRESHOLD:
        print(f"WARN: test slice has {len(test):,} < {N_THRESHOLD} — "
              f"увеличить OUT_OF_SAMPLE_DAYS")

    test, model_metrics = fit_predict(train, test)
    sig = compute_signals(test)
    decile_rows = stage2_decile_analysis(sig)
    sim = stage2_5_execution_sim(sig)
    econ = stage3_economics(sim, decile_rows, model_metrics)

    # Save report
    out_dir = Path("/root/oracle/products/d-ml-calibrator-topdecile")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# D-MLCalibratorTopDecile — Stage 2 Backtest ({date.today().isoformat()})",
        "",
        f"**Anchor:** {ANCHOR_HOURS}h · **Mid-price:** [{MID_PRICE_MIN}, {MID_PRICE_MAX}] · "
        f"**Vol floor:** ${VOL_FLOOR_USD:,}",
        f"**Out-of-sample:** {OUT_OF_SAMPLE_DAYS}d · **Bankroll:** ${SHADOW_BANKROLL:,.0f}",
        "",
        "## Model quality",
        "",
        f"- Train AUC: {model_metrics['train_auc']:.4f} · Brier: {model_metrics['train_brier']:.4f}",
        f"- Test  AUC: {model_metrics['test_auc']:.4f} · Brier: {model_metrics['test_brier']:.4f}",
        "",
        "## Decile analysis",
        "",
        "| decile | n | WR | EV% avg | raw ROI | std ROI | NO% |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in decile_rows:
        lines.append(
            f"| {r['decile']} | {r['n']:,} | {r['wr']:.1%} | "
            f"{r['ev_pct_avg']:+.1f} | {r['raw_roi']:+.1%} | "
            f"{r['std_roi']:.2f} | {r['no_pct']:.0f}% |"
        )

    lines.extend([
        "",
        "## Stage 2.5 Execution (top-decile)",
        "",
        f"- Trades: {sim['n_trades'] if sim else 0:,}",
        f"- WR: {sim['n_won']/max(sim['n_trades'], 1):.1%}" if sim else "- WR: n/a",
        f"- Net PnL: ${sim['total_net'] if sim else 0:+,.2f}",
        f"- ROI: {sim['roi']*100 if sim else 0:+.1f}%",
        f"- Daily raw: ${sim['daily_pnl'] if sim else 0:+.2f}",
        "",
        "## Stage 3 Economics",
        "",
        f"- Realistic Daily: **${econ.get('realistic_daily_pnl', 0):+.2f}**",
        f"- Realistic ROI: {econ.get('realistic_roi', 0)*100:+.1f}%",
        f"- Adversarial purity: {'✅ PASS' if econ.get('adversarial_pass') else '❌ FAIL'}",
        f"- **Decision:** {econ.get('decision', 'N/A')}",
    ])

    out_path = out_dir / "stage2_backtest.md"
    out_path.write_text("\n".join(lines))
    print(f"\nReport saved: {out_path}")
    print(f"Total: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
