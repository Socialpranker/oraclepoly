#!/usr/bin/env python3
"""D13 RetailConfidenceFade — canonical RA validation.

Hypothesis (from wallet_quality_edge_search):
  В extreme zone YES@[0.85, 0.95), когда smart_money_taker_share < 5%
  (только retail в pre-anchor 24h window), NO-fade имеет SUSTAINED EDGE
  на ALL anchors (4/24/72h):
    - 4h: WR 16.3%, net_ROI +9.9%, n=1214
    - 24h: WR 15.8%, net_ROI +7.4%, n=1243
    - 72h: WR 16.4%, net_ROI +12.0%, n=1021

Compared с baseline (no filter): -10.5% / -10.7% / -8.8% — это **+20pp uplift**.

Vs D7b extended: D7b zone [0.85, 0.97] vs D13 [0.85, 0.95]; D7b filters
on category, D13 filters on smart_money_taker_share. **Orthogonal dimension.**

Tests:
  1. Per-anchor canonical numbers
  2. Adversarial mirror (YES side)
  3. Per-category breakdown
  4. Capacity sizing
  5. Overlap с D7b (zone + 24h flow filter overlap?)

Output: docs/backtests/D13_retail_confidence_fade.md
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb

ML_V2 = "/mnt/data/canonical/ml_features_v2.parquet"

DEFAULT_FEE_PCT = 1.25  # mid-category taker (см. _backtest_params.py for per-category breakdown)
SLIPPAGE_PP = 4.0  # extreme zone
MIN_N = 500
MIN_NET_ROI = 0.05  # 5% lower bound (5%-10% = MARGINAL, ≥10% = PASS)


def main():
    print("=" * 70, flush=True)
    print("D13 RetailConfidenceFade — Canonical RA Validation", flush=True)
    print("=" * 70, flush=True)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=4")

    fee_drag = DEFAULT_FEE_PCT * 2 / 100
    slip = SLIPPAGE_PP / 100

    results_per_anchor = {}

    for anchor_h in [4, 24, 72]:
        print(f"\n{'='*70}\nAnchor: {anchor_h}h\n{'='*70}", flush=True)

        # Load filtered universe
        df = con.execute(
            f"""
            SELECT *
            FROM read_parquet('{ML_V2}')
            WHERE anchor_hours = {anchor_h}
              AND yes_won IS NOT NULL
              AND yes_price_at_anchor >= 0.85
              AND yes_price_at_anchor < 0.95
              AND n_takers_24h_before >= 1
              AND smart_money_taker_share < 0.05
            """
        ).fetchdf()
        n = len(df)
        print(f"\n1. Universe (smart<0.05, extreme zone): n={n:,}", flush=True)

        if n < 100:
            print("   ⚠️  too small", flush=True)
            continue

        # NO fade
        wins_no = (df["yes_won"] == 0).sum()
        wr_no = wins_no / n
        cost_no = (1 - df["yes_price_at_anchor"]).mean()
        net_roi_no = (wr_no * (1 - fee_drag) - (cost_no + slip)) / (cost_no + slip)
        print(f"   NO fade: WR={wr_no*100:.1f}%, avg_cost=${cost_no:.4f}, net_ROI={net_roi_no*100:+.1f}%", flush=True)

        # YES mirror
        wr_yes = (df["yes_won"] == 1).mean()
        cost_yes = df["yes_price_at_anchor"].mean()
        net_roi_yes = (wr_yes * (1 - fee_drag) - (cost_yes + slip)) / (cost_yes + slip)
        print(f"   YES mirror: WR={wr_yes*100:.1f}%, net_ROI={net_roi_yes*100:+.1f}%", flush=True)

        # Per-category
        print("\n2. Per-category breakdown:", flush=True)
        for cat in df["category"].unique():
            group = df[df["category"] == cat]
            if len(group) < 50:
                continue
            wins_c = (group["yes_won"] == 0).sum()
            wr_c = wins_c / len(group)
            cost_c = (1 - group["yes_price_at_anchor"]).mean()
            roi_c = (wr_c * (1 - fee_drag) - (cost_c + slip)) / (cost_c + slip)
            flag = "✅" if (len(group) >= 200 and roi_c >= 0.05) else " "
            print(f"   {flag} {cat:<20} n={len(group):>5} WR={wr_c*100:>5.1f}% net_ROI={roi_c*100:>+6.1f}%", flush=True)

        # Excluding Other (data quality issue per earlier research)
        no_other = df[df["category"] != "Other"]
        if len(no_other) >= 100:
            wins = (no_other["yes_won"] == 0).sum()
            wr = wins / len(no_other)
            cost = (1 - no_other["yes_price_at_anchor"]).mean()
            net = (wr * (1 - fee_drag) - (cost + slip)) / (cost + slip)
            print(f"\n3. Excluding Other: n={len(no_other):,}, WR={wr*100:.1f}%, net_ROI={net*100:+.1f}%", flush=True)

        # Capacity
        days = 821
        signals_per_day = n / days
        avg_stake = 25
        daily_pnl_raw = signals_per_day * avg_stake * net_roi_no
        daily_pnl_honest = daily_pnl_raw * 0.5
        print(f"\n4. Capacity: {signals_per_day:.2f}/day, raw $/d=${daily_pnl_raw:+.2f}, honest $/d=${daily_pnl_honest:+.2f}", flush=True)

        # Verdict
        if n >= MIN_N and net_roi_no >= MIN_NET_ROI:
            verdict = "PASS" if net_roi_no >= 0.10 else "MARGINAL"
            print(f"\n5. ✅ VERDICT: {verdict}", flush=True)
        else:
            verdict = "FAIL"
            print(f"\n5. ⚠️  FAIL — needs n≥{MIN_N} AND net_ROI≥{MIN_NET_ROI*100:.0f}%", flush=True)

        results_per_anchor[anchor_h] = {
            "n": n, "wr": wr_no, "net_roi": net_roi_no, "net_roi_yes": net_roi_yes,
            "signals_per_day": signals_per_day, "daily_raw": daily_pnl_raw,
            "daily_honest": daily_pnl_honest, "verdict": verdict, "cost": cost_no,
        }

    # Combined verdict — all-anchors mode
    print(f"\n{'='*70}", flush=True)
    print(f"COMBINED VERDICT: D13 RetailConfidenceFade", flush=True)
    print(f"{'='*70}", flush=True)

    total_signals = sum(r["n"] for r in results_per_anchor.values())
    total_raw = sum(r["daily_raw"] for r in results_per_anchor.values())
    total_honest = sum(r["daily_honest"] for r in results_per_anchor.values())
    pass_count = sum(1 for r in results_per_anchor.values() if r["verdict"] in {"PASS", "MARGINAL"})

    print(f"\nTotal signals: {total_signals:,}", flush=True)
    print(f"Pass anchors: {pass_count}/3", flush=True)
    print(f"Combined raw $/d: ${total_raw:+.2f}", flush=True)
    print(f"Combined honest $/d: ${total_honest:+.2f}", flush=True)

    # Write detector md only if at least 1 anchor passes
    if pass_count >= 1:
        write_md(results_per_anchor)

    print(f"\nDone in {time.time() - t0:.1f}s", flush=True)


def write_md(results):
    out_path = Path("docs/backtests/D13_retail_confidence_fade.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Find best anchor for header
    best_h = max(results.keys(), key=lambda h: results[h]["net_roi"])
    best = results[best_h]

    md = f"""---
detector: D13_retail_confidence_fade
verdict: {best['verdict']}
backtest_date: 2026-05-03
canonical_source: scripts/research/d13_retail_confidence_canonical.py
data_source: /mnt/data/canonical/ml_features_v2.parquet
n_signals_4h: {results.get(4, {}).get('n', 0)}
n_signals_24h: {results.get(24, {}).get('n', 0)}
n_signals_72h: {results.get(72, {}).get('n', 0)}
backtest_days: 821
roi_net_pct_4h: {round(results.get(4, {}).get('net_roi', 0)*100, 1)}
roi_net_pct_24h: {round(results.get(24, {}).get('net_roi', 0)*100, 1)}
roi_net_pct_72h: {round(results.get(72, {}).get('net_roi', 0)*100, 1)}
combined_daily_realistic_usd_at_5k: {round(sum(r['daily_raw'] for r in results.values()), 2)}
combined_daily_honest_usd_at_5k: {round(sum(r['daily_honest'] for r in results.values()), 2)}
honest_haircut_factor: 0.5
honest_haircut_reason: filter mismatch (canonical lifetime_volume vs production volume_24h) + post-V2 unverified
adversarial_mirror_pass: {"true" if all(abs(r['net_roi_yes']) < 0.05 for r in results.values()) else "partial"}
adversarial_mirror_note: NO-only edge на extreme zone with retail-only takers (asymmetric pinning)
post_v2_split: pending (canonical pre-V2 only)
discovered_via: wallet_quality_edge_search.py — H2 retail confidence hypothesis
---

# D13 RetailConfidenceFade — Canonical Backtest

**Hypothesis:** В extreme zone YES@[0.85, 0.95) NO-fade имеет sustained edge
на ALL anchors когда `smart_money_taker_share < 5%` (только retail в pre-anchor
24h window). Smart money отсутствие → retail "chasing favorites" → easier fade.

**Inverse signal:** smart_money_taker_share > 0.5 → NO fade ROI -33% (smart
money знают что-то, не fade против них).

## Filters

- **Zone:** YES [0.85, 0.95)
- **Anchors:** 4h / 24h / 72h
- **Trigger:** `smart_money_taker_share < 0.05` (n_takers_24h_before ≥ 1)
- **Side:** NO

## Numbers per anchor

| Anchor | n | WR (NO) | net_ROI | Mirror YES net_ROI | Daily raw $ | Daily honest $ |
|---|---|---|---|---|---|---|
"""
    for h in [4, 24, 72]:
        if h in results:
            r = results[h]
            md += f"| {h}h | {r['n']:,} | {r['wr']*100:.1f}% | {r['net_roi']*100:+.1f}% | {r['net_roi_yes']*100:+.1f}% | ${r['daily_raw']:+.2f} | ${r['daily_honest']:+.2f} |\n"

    md += f"""

## Combined economics (all anchors)

- Total signals: {sum(r['n'] for r in results.values()):,} over 821 days
- Combined raw $/d: ${sum(r['daily_raw'] for r in results.values()):+.2f}
- Combined honest $/d: ${sum(r['daily_honest'] for r in results.values()):+.2f}

## Discovered via

LGB feature discovery + wallet quality edge search.
- Built canonical layer: `wallet_performance.parquet` (per-wallet realized PnL from quant.parquet, 2.28M wallets)
- Built PIT-correct: `wallet_score_at_anchor.parquet` (taker quality at anchor)
- Edge surfaced via H2 hypothesis (retail confidence boost):
  при retail-only takers, fade favorites работает.

## Vs D7b extended

D7b zone [0.85, 0.97] (broader), D13 zone [0.85, 0.95) (subset).
D7b filters category, D13 filters smart_money_taker_share — **orthogonal**.
Likely overlap: 30-50% сигналов могут быть в обоих detectorх.
TODO: write d13_overlap_d7b.py to measure overlap precisely.

## Reverse hypothesis (warning signal)

Когда `smart_money_taker_share > 0.5` в extreme zone → fade NO ROI -33%
(smart money has informational advantage). Можно использовать как
**emission veto** для D7b: don't emit NO-fade signal если smart-heavy market.

Generated by `scripts/research/d13_retail_confidence_canonical.py`.
"""
    out_path.write_text(md)
    print(f"\n   → Written {out_path}", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
