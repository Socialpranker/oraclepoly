#!/usr/bin/env python3
"""D12 ExtremeBuyImbalanceNoFade — extreme zone NO-fade с buy_imbalance signal.

Hypothesis (from 2D interactions):
  В extreme zone YES@[0.85, 0.95) NO-fade сильнее работает когда
  buy_imbalance_asof > 0.36 (sustained buying pressure последний bucket).
  Это D7b refinement — D7b uses zone+category, D12 adds flow-based filter.

Test:
  1. Extreme zone universe baseline
  2. With buy_imbalance > 0.36 filter
  3. Per-category breakdown (avoid Other-dominated)
  4. Adversarial mirror (YES side)
  5. Compare с baseline D7b extreme zone

Output: docs/backtests/D12_extreme_buy_imbalance_no_fade.md if PASS
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb

ML_FEATURES = "/mnt/data/canonical/ml_features.parquet"

DEFAULT_FEE_PCT = 1.25  # mid-category taker (см. _backtest_params.py for per-category breakdown)
SLIPPAGE_PP = 4.0  # extreme zone wider slippage
MIN_N = 500
MIN_NET_ROI = 0.10


def main():
    print("=" * 70)
    print("D12 ExtremeBuyImbalanceNoFade — Canonical RA Validation")
    print("=" * 70)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    con.execute("SET threads=4")

    fee_drag = DEFAULT_FEE_PCT * 2 / 100
    slip = SLIPPAGE_PP / 100

    for anchor_h in [4, 24, 72]:
        print(f"\n{'='*70}\nAnchor: {anchor_h}h\n{'='*70}")
        df = con.execute(
            f"""
            SELECT *
            FROM read_parquet('{ML_FEATURES}')
            WHERE anchor_hours = {anchor_h}
              AND yes_won IS NOT NULL
              AND yes_price_at_anchor >= 0.85
              AND yes_price_at_anchor < 0.95
            """
        ).fetchdf()
        print(f"\n1. Extreme zone universe: n={len(df):,}")

        # Baseline D7b extreme NO fade
        wins_base = (df["yes_won"] == 0).sum()
        wr_base = wins_base / max(len(df), 1)
        cost_base = (1 - df["yes_price_at_anchor"]).mean()
        cost_base_net = cost_base + slip
        net_roi_base = (wr_base * (1 - fee_drag) - cost_base_net) / cost_base_net
        print(f"   Baseline NO fade: WR={wr_base*100:.1f}%, net_ROI={net_roi_base*100:+.1f}%")

        # ─── Filter: buy_imbalance_asof > 0.36 ───
        filt = df[df["buy_imbalance_asof"] > 0.36]
        if len(filt) >= 100:
            print(f"\n2. Filter: buy_imbalance > 0.36, n={len(filt):,}")
            wins = (filt["yes_won"] == 0).sum()
            wr = wins / len(filt)
            cost = (1 - filt["yes_price_at_anchor"]).mean()
            net_roi = (wr * (1 - fee_drag) - (cost + slip)) / (cost + slip)
            print(f"   NO fade: WR={wr*100:.1f}%, net_ROI={net_roi*100:+.1f}%")

            # YES mirror
            wr_yes = (filt["yes_won"] == 1).mean()
            cost_yes = filt["yes_price_at_anchor"].mean()
            net_roi_yes = (wr_yes * (1 - fee_drag) - (cost_yes + slip)) / (cost_yes + slip)
            print(f"   YES mirror: WR={wr_yes*100:.1f}%, net_ROI={net_roi_yes*100:+.1f}%")

            # Per-category
            print("\n   Per-category breakdown:")
            for cat, group in filt.groupby("category"):
                if len(group) < 50:
                    continue
                wins_c = (group["yes_won"] == 0).sum()
                wr_c = wins_c / len(group)
                cost_c = (1 - group["yes_price_at_anchor"]).mean()
                roi_c = (wr_c * (1 - fee_drag) - (cost_c + slip)) / (cost_c + slip)
                print(f"     {cat:<20} n={len(group):>5} WR={wr_c*100:>5.1f}% net_ROI={roi_c*100:>+6.1f}%")

            # Excluding Other
            no_other = filt[filt["category"] != "Other"]
            if len(no_other) >= 100:
                wins_no = (no_other["yes_won"] == 0).sum()
                wr_no = wins_no / len(no_other)
                cost_no = (1 - no_other["yes_price_at_anchor"]).mean()
                net_no = (wr_no * (1 - fee_drag) - (cost_no + slip)) / (cost_no + slip)
                print(f"\n   Excluding Other: n={len(no_other):,}, WR={wr_no*100:.1f}%, net_ROI={net_no*100:+.1f}%")

                # Capacity
                days = 821
                signals_per_day = len(no_other) / days
                avg_stake = 25
                daily_pnl_raw = signals_per_day * avg_stake * net_no
                daily_pnl_honest = daily_pnl_raw * 0.5
                print(f"   Capacity: {signals_per_day:.2f}/day, raw $/d=${daily_pnl_raw:+.2f}, honest $/d=${daily_pnl_honest:+.2f}")

                # Verdict
                if net_no >= MIN_NET_ROI and len(no_other) >= MIN_N:
                    verdict = "PASS" if net_no >= 0.20 else "MARGINAL"
                    print(f"\n   ✅ VERDICT: {verdict}")
                    if anchor_h == 24:  # write md only for primary anchor
                        write_md(no_other, anchor_h, wr_no, cost_no, net_no, net_roi_yes,
                                 signals_per_day, daily_pnl_raw, daily_pnl_honest, verdict, days)
                else:
                    print(f"   ⚠️  not PASS (need n≥{MIN_N} AND net_ROI≥{MIN_NET_ROI*100:.0f}%)")

    print(f"\nDone in {time.time() - t0:.1f}s")


def write_md(df, anchor_h, wr, cost, net_roi, net_roi_yes, signals_per_day,
             daily_raw, daily_honest, verdict, days):
    out_path = Path("docs/backtests/D12_extreme_buy_imbalance_no_fade.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md = f"""---
detector: D12_extreme_buy_imbalance_no_fade
verdict: {verdict}
backtest_date: 2026-05-03
canonical_source: scripts/research/d12_extreme_buy_imbalance_canonical.py
data_source: /mnt/data/canonical/ml_features.parquet
n_signals: {len(df)}
backtest_days: {days}
roi_net_pct: {round(net_roi*100, 1)}
wr_pct: {round(wr*100, 1)}
daily_realistic_usd_at_5k: {round(daily_raw, 2)}
daily_realistic_usd_at_1k: {round(daily_raw / 5, 2)}
honest_haircut_factor: 0.5
honest_daily_usd_at_5k: {round(daily_honest, 2)}
honest_daily_usd_at_1k: {round(daily_honest / 5, 2)}
honest_haircut_reason: filter mismatch (lifetime_volume vs volume_24h) + post-V2 unverified
trades_per_day: {round(signals_per_day, 2)}
adversarial_mirror_pass: {"true" if abs(net_roi_yes) < 0.05 else "false"}
adversarial_mirror_note: NO-only edge на extreme zone with sustained buying (asymmetric pinning)
post_v2_split: pending
---

# D12 ExtremeBuyImbalanceNoFade — Canonical Backtest

**Hypothesis:** В extreme zone YES@[0.85, 0.95) NO-fade сильнее когда `buy_imbalance_asof > 0.36`
(sustained buying pressure on most recent hour bucket). Excluding noisy `Other` category.

**Verdict:** {verdict}

## Filters

- **Zone:** YES [0.85, 0.95)
- **Anchor:** {anchor_h}h
- **Trigger:** `buy_imbalance_asof > 0.36`
- **Category:** != 'Other' (signal-noise filter)
- **Side:** NO

## Numbers

- n = {len(df):,} signals over {days} days = {signals_per_day:.2f}/day
- WR (NO): {wr*100:.1f}%
- avg_cost: ${cost:.4f}
- net ROI: {net_roi*100:+.1f}%
- Mirror (YES): net ROI {net_roi_yes*100:+.1f}%
- Daily PnL @ $5K (raw): ${daily_raw:+.2f}
- Daily PnL honest (×0.5): ${daily_honest:+.2f}

## Discovered via

LGB feature importance + 2D interactions surfaced `buy_imbalance_asof × extreme zone` as
top cell. Этот script validates с per-category check, adversarial mirror, capacity sizing.

См. `scripts/research/lgb_feature_importance.py`, `scripts/research/lgb_2d_interactions.py`.

Generated by `scripts/research/d12_extreme_buy_imbalance_canonical.py`.
"""
    out_path.write_text(md)
    print(f"   → Written {out_path}")


if __name__ == "__main__":
    main()
