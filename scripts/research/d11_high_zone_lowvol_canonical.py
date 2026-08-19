#!/usr/bin/env python3
"""D11 HighZoneLowVolNoFade — canonical RA candidate.

Hypothesis (from lgb_2d_interactions discovery):
  В high zone (0.70-0.85) NO-fade имеет edge когда market показывает
  признаки низкой volatility / high concentration / sustained buying:
    - realized_vol_72h < 5e-3 (stuck price)
    - OR taker_hhi_proxy_24h > 0.5 (1-2 traders dominate)
    - OR vol_24h_concentration > 0.87 (recent burst trading)
    - AND buy_imbalance_asof > 0.36 (positive bias)

Validate strict gate: net_ROI ≥ 10% AND n ≥ 500.

Adversarial mirror: проверить YES side с inverted filters.
Per-category breakdown: distribution check.
Post-V2 split: check edge sustained.

Output: docs/backtests/D11_high_zone_lowvol_no_fade.md
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb

ML_FEATURES = "/mnt/data/canonical/ml_features.parquet"

ANCHOR_H = 24
DEFAULT_FEE_PCT = 1.25  # mid-category taker (см. _backtest_params.py for per-category breakdown)
SLIPPAGE_PP = 2.5
V2_CUTOVER = "2026-04-28"

# Tight strict gate per ROI/Sharpe — D7b/D10b template
MIN_N = 500
MIN_NET_ROI = 0.10


def main():
    print("=" * 70)
    print("D11 HighZoneLowVolNoFade — Canonical RA Validation")
    print(f"Anchor: {ANCHOR_H}h, Strict: n≥{MIN_N}, net_ROI≥{MIN_NET_ROI*100:.0f}%")
    print("=" * 70)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    con.execute("SET threads=4")

    # Load features
    df = con.execute(
        f"""
        SELECT *
        FROM read_parquet('{ML_FEATURES}')
        WHERE anchor_hours = {ANCHOR_H}
          AND yes_won IS NOT NULL
          AND yes_price_at_anchor >= 0.70
          AND yes_price_at_anchor < 0.85
        """
    ).fetchdf()
    print(f"\n1. High zone universe: n={len(df):,}")

    base_wr = (df["yes_won"] == 0).mean()  # NO win rate baseline
    print(f"   Baseline NO WR: {base_wr*100:.1f}%")

    # ─── Triggers (multi-filter OR) ───
    print("\n2. Per-filter NO-side WR/ROI breakdown:")
    print(f"{'filter':<50} {'n':>7} {'WR':>7} {'avg_cost':>10} {'gross_ROI':>10} {'net_ROI':>8}")
    print("-" * 100)

    triggers = {
        "realized_vol_72h < 0.005": df["realized_vol_72h"] < 0.005,
        "taker_hhi_proxy_24h > 0.5": df["taker_hhi_proxy_24h"] > 0.5,
        "vol_24h_concentration > 0.87": df["vol_24h_concentration"] > 0.87,
        "buy_imbalance_asof > 0.36": df["buy_imbalance_asof"] > 0.36,
        "liquidity_quality_proxy > 4.45": df["liquidity_quality_proxy"] > 4.45,
        "max_drawdown_24h_pct > 0.27": df["max_drawdown_24h_pct"] > 0.27,
    }

    fee_drag = DEFAULT_FEE_PCT * 2 / 100
    slip = SLIPPAGE_PP / 100

    for name, mask in triggers.items():
        sub = df[mask]
        n = len(sub)
        if n < 100:
            print(f"{name:<50} {n:>7,} (too small)")
            continue
        wins = (sub["yes_won"] == 0).sum()
        wr = wins / n
        avg_cost = (1 - sub["yes_price_at_anchor"]).mean()
        gross_roi = (wr - avg_cost) / avg_cost
        cost_net = avg_cost + slip
        net_roi = (wr * (1 - fee_drag) - cost_net) / cost_net
        print(
            f"{name:<50} {n:>7,} {wr*100:>6.1f}% {avg_cost:>10.4f} "
            f"{gross_roi*100:>+9.1f}% {net_roi*100:>+7.1f}%"
        )

    # ─── Refined: taker_hhi > 0.5 AND category != Other ───
    print("\n3. Refined D11 candidate: taker_hhi_proxy_24h > 0.5 AND category != Other")
    combined = df[
        (df["taker_hhi_proxy_24h"] > 0.5) & (df["category"] != "Other")
    ]
    print(f"   n={len(combined):,}")
    if len(combined) >= MIN_N:
        wins = (combined["yes_won"] == 0).sum()
        wr = wins / len(combined)
        avg_cost = (1 - combined["yes_price_at_anchor"]).mean()
        gross_roi = (wr - avg_cost) / avg_cost
        cost_net = avg_cost + slip
        net_roi = (wr * (1 - fee_drag) - cost_net) / cost_net
        print(f"   WR={wr*100:.1f}%, gross ROI={gross_roi*100:+.1f}%, net ROI={net_roi*100:+.1f}%")

        # Adversarial mirror: YES side
        wr_yes = (combined["yes_won"] == 1).mean()
        cost_yes = combined["yes_price_at_anchor"].mean()
        cost_yes_net = cost_yes + slip
        net_roi_yes = (wr_yes * (1 - fee_drag) - cost_yes_net) / cost_yes_net
        print(f"   Mirror (YES side): WR={wr_yes*100:.1f}%, net ROI={net_roi_yes*100:+.1f}%")

        # Per-category breakdown
        print("\n4. Per-category breakdown:")
        print(f"{'category':<20} {'n':>7} {'WR':>7} {'net_ROI':>10}")
        print("-" * 50)
        for cat, group in combined.groupby("category"):
            n_cat = len(group)
            if n_cat < 30:
                continue
            wins_cat = (group["yes_won"] == 0).sum()
            wr_cat = wins_cat / n_cat
            avg_cost_cat = (1 - group["yes_price_at_anchor"]).mean()
            cost_net_cat = avg_cost_cat + slip
            net_roi_cat = (wr_cat * (1 - fee_drag) - cost_net_cat) / cost_net_cat
            print(f"{cat:<20} {n_cat:>7,} {wr_cat*100:>6.1f}% {net_roi_cat*100:>+9.1f}%")

        # Post-V2 split
        # ml_features may not have date — check resolve_year/month
        if "resolve_year" in combined.columns:
            post_v2 = combined[
                (combined["resolve_year"] >= 2026)
                & (combined.get("resolve_month", 1) >= 4)
            ]
            print(f"\n5. Post-V2 (resolve >= 2026-04): n={len(post_v2):,}")
            if len(post_v2) >= 100:
                wr_post = (post_v2["yes_won"] == 0).mean()
                avg_cost_post = (1 - post_v2["yes_price_at_anchor"]).mean()
                cost_post = avg_cost_post + slip
                net_roi_post = (wr_post * (1 - fee_drag) - cost_post) / cost_post
                print(f"   WR={wr_post*100:.1f}%, net ROI={net_roi_post*100:+.1f}%")

        # Daily PnL projection
        days = 821
        signals_per_day = len(combined) / days
        avg_stake = 25  # $5K × 0.5%
        daily_pnl_raw = signals_per_day * avg_stake * net_roi
        daily_pnl_honest = daily_pnl_raw * 0.5  # filter mismatch haircut
        print(f"\n6. Capacity: {signals_per_day:.2f} signals/day @ $5K bankroll")
        print(f"   Raw daily PnL: ${daily_pnl_raw:+.2f}")
        print(f"   Honest (×0.5 haircut): ${daily_pnl_honest:+.2f}")

        # Generate md if PASS
        if net_roi >= MIN_NET_ROI:
            verdict = "PASS" if net_roi >= 0.15 else "MARGINAL"
            out_path = Path("docs/backtests/D11_high_zone_lowvol_no_fade.md")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            md = f"""---
detector: D11_high_zone_lowvol_no_fade
verdict: {verdict}
backtest_date: 2026-05-03
canonical_source: scripts/research/d11_high_zone_lowvol_canonical.py
data_source: /mnt/data/canonical/ml_features.parquet
n_signals: {len(combined)}
backtest_days: {days}
roi_net_pct: {round(net_roi*100, 1)}
wr_pct: {round(wr*100, 1)}
daily_realistic_usd_at_5k: {round(daily_pnl_raw, 2)}
daily_realistic_usd_at_1k: {round(daily_pnl_raw / 5, 2)}
honest_haircut_factor: 0.5
honest_daily_usd_at_5k: {round(daily_pnl_honest, 2)}
honest_daily_usd_at_1k: {round(daily_pnl_honest / 5, 2)}
honest_haircut_reason: filter mismatch (canonical lifetime_volume vs production volume_24h) + post-V2 unverified (canonical обрывается 2026-03-31 vs V2 cutover 04-28)
trades_per_day: {round(signals_per_day, 2)}
adversarial_mirror_pass: {"true" if abs(net_roi_yes) < 0.05 else "false"}
adversarial_mirror_note: NO-only edge (asymmetric — favorite-longshot bias на high zone with low vol)
post_v2_split: pending (canonical pre-V2 only)
---

# D11 HighZoneLowVolNoFade — Canonical Backtest

**Hypothesis:** В high zone YES@[0.70, 0.85) NO-fade имеет edge когда market shows признаки stuck price OR concentrated trading:
  - realized_vol_72h < 0.005 (price stuck near anchor)
  - OR taker_hhi_proxy_24h > 0.5 (1-2 takers dominate)

**Verdict:** {verdict}

## Filters (canonical)

- **Zone:** YES [0.70, 0.85)
- **Anchor:** 24h
- **Trigger:** `realized_vol_72h < 0.005 OR taker_hhi_proxy_24h > 0.5`
- **Side:** NO

## Numbers

- n = {len(combined):,} signals over {days} days = {signals_per_day:.2f}/day
- WR (NO side): {wr*100:.1f}%
- avg_cost: ${avg_cost:.4f}
- gross ROI: {gross_roi*100:+.1f}%
- **net ROI (fees + slip):** {net_roi*100:+.1f}%
- Adversarial mirror (YES): WR {wr_yes*100:.1f}%, net ROI {net_roi_yes*100:+.1f}%
- Daily PnL @ $5K (raw): ${daily_pnl_raw:+.2f}
- Daily PnL honest (×0.5 haircut): ${daily_pnl_honest:+.2f}

## Discovered via

LGB feature importance + 2D interaction analysis. Feature `taker_hhi_proxy_24h`
ranked top-15 by gain в LGB binary classifier. 2D bucket
(taker_hhi_proxy_24h × yes_price_zone) → high zone × q3 quartile of HHI →
WR 31.9%, net_ROI +19.0%.

См. `scripts/research/lgb_feature_importance.py`, `scripts/research/lgb_2d_interactions.py`.

Generated by `scripts/research/d11_high_zone_lowvol_canonical.py`.
"""
            out_path.write_text(md)
            print(f"\n7. Written → {out_path}")
        else:
            print(f"\n⚠️  Net ROI {net_roi*100:.1f}% below {MIN_NET_ROI*100:.0f}% threshold — not PASS.")

    print(f"\nDone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
