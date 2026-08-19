#!/usr/bin/env python3
"""Search for tradeable edge using wallet quality features в ml_features_v2.

Tests 4 specific hypotheses derived from wallet quality dimension:

H1: Smart money fade signal
    Where smart_money_taker_share > 0.5 AND extreme zone (≥0.85) →
    NO fade should work LESS (sharps know what they're doing).

H2: Retail confidence boost
    Where smart_money_taker_share < 0.05 AND extreme zone →
    NO fade should work MORE (pure retail chasing favorites).

H3: Top decile contrarian
    top_decile_taker_share > 0.3 → mirror their position
    (top wallets are usually right).

H4: Whale conviction filter
    taker_pnl_weighted_usd_24h > 0 (positive PnL takers entered) →
    reduce fade confidence.

Strict gate: n ≥ 500, net_ROI ≥ 10%.
Compare с baseline D7b extreme zone NO-fade.
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb

ML_V2 = "/mnt/data/canonical/ml_features_v2.parquet"

DEFAULT_FEE_PCT = 1.5
SLIPPAGE_BY_ZONE = {
    "longshot": 4.0, "low": 2.5, "mid": 2.0, "high": 2.5, "extreme": 4.0,
}
MIN_N = 500
MIN_NET_ROI = 0.10


def zone_for_price(p: float) -> str:
    if p < 0.30:
        return "longshot"
    if p < 0.50:
        return "low"
    if p < 0.70:
        return "mid"
    if p < 0.85:
        return "high"
    return "extreme"


def compute_metrics(df, side: str, slip: float, fee_drag: float):
    n = len(df)
    if n == 0:
        return None
    avg_price = df["yes_price_at_anchor"].mean()
    if side == "NO":
        wins = (df["yes_won"] == 0).sum()
        cost = 1 - avg_price
    else:
        wins = (df["yes_won"] == 1).sum()
        cost = avg_price
    wr = wins / n
    cost_net = cost + slip
    net_roi = (wr * (1 - fee_drag) - cost_net) / cost_net
    return {"n": n, "wr": wr, "avg_price": avg_price, "net_roi": net_roi}


def main():
    print("=" * 70)
    print("Wallet Quality Edge Search")
    print(f"Strict: n≥{MIN_N}, net_ROI≥{MIN_NET_ROI*100:.0f}%")
    print("=" * 70)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    con.execute("SET threads=4")

    fee_drag = DEFAULT_FEE_PCT * 2 / 100

    for anchor_h in [4, 24, 72]:
        print(f"\n{'='*70}\nAnchor: {anchor_h}h\n{'='*70}")

        # Load with wallet quality features non-null
        df = con.execute(
            f"""
            SELECT *
            FROM read_parquet('{ML_V2}')
            WHERE anchor_hours = {anchor_h}
              AND yes_won IS NOT NULL
              AND yes_price_at_anchor >= 0.10
              AND yes_price_at_anchor < 0.95
              AND n_takers_24h_before >= 1  -- has wallet quality data
            """
        ).fetchdf()
        print(f"\nUniverse: n={len(df):,}")

        # H1: Smart money fade — extreme zone, smart share > 0.5
        print("\n— H1: Smart money fade (smart_share > 0.5, extreme zone)")
        slip = SLIPPAGE_BY_ZONE["extreme"] / 100
        for thr in [0.3, 0.5, 0.7]:
            for ext_lo, ext_hi in [(0.85, 0.95)]:
                sub = df[
                    (df["yes_price_at_anchor"] >= ext_lo)
                    & (df["yes_price_at_anchor"] < ext_hi)
                    & (df["smart_money_taker_share"] > thr)
                ]
                m = compute_metrics(sub, "NO", slip, fee_drag)
                if m and m["n"] >= 100:
                    flag = "✅" if m["n"] >= MIN_N and m["net_roi"] >= MIN_NET_ROI else " "
                    print(f"  {flag} smart>{thr:.1f} ext[{ext_lo},{ext_hi}): n={m['n']:>5} WR={m['wr']*100:>5.1f}% net_ROI={m['net_roi']*100:>+6.1f}%")

        # H2: Retail confidence boost — extreme zone, smart share < 0.05
        print("\n— H2: Retail confidence (smart_share < 0.05, extreme zone)")
        for thr in [0.05, 0.10, 0.20]:
            for ext_lo, ext_hi in [(0.85, 0.95)]:
                sub = df[
                    (df["yes_price_at_anchor"] >= ext_lo)
                    & (df["yes_price_at_anchor"] < ext_hi)
                    & (df["smart_money_taker_share"] < thr)
                ]
                m = compute_metrics(sub, "NO", slip, fee_drag)
                if m and m["n"] >= 100:
                    flag = "✅" if m["n"] >= MIN_N and m["net_roi"] >= MIN_NET_ROI else " "
                    print(f"  {flag} smart<{thr:.2f} ext[{ext_lo},{ext_hi}): n={m['n']:>5} WR={m['wr']*100:>5.1f}% net_ROI={m['net_roi']*100:>+6.1f}%")

        # H3: Top decile contrarian
        print("\n— H3: Top decile contrarian (top_decile_share > 0.3)")
        for zone_name, zlo, zhi in [
            ("longshot", 0.10, 0.30),
            ("low", 0.30, 0.50),
            ("mid", 0.50, 0.70),
            ("high", 0.70, 0.85),
            ("extreme", 0.85, 0.95),
        ]:
            slip_zone = SLIPPAGE_BY_ZONE[zone_name] / 100
            sub = df[
                (df["yes_price_at_anchor"] >= zlo)
                & (df["yes_price_at_anchor"] < zhi)
                & (df["top_decile_taker_share"] > 0.3)
            ]
            for side in ["NO", "YES"]:
                m = compute_metrics(sub, side, slip_zone, fee_drag)
                if m and m["n"] >= 100:
                    flag = "✅" if m["n"] >= MIN_N and m["net_roi"] >= MIN_NET_ROI else " "
                    print(f"  {flag} {zone_name}/{side} top>0.3: n={m['n']:>5} WR={m['wr']*100:>5.1f}% net_ROI={m['net_roi']*100:>+6.1f}%")

        # H4: Whale conviction filter
        print("\n— H4: Whale conviction filter (taker_pnl_weighted_usd_24h sign)")
        for zone_name, zlo, zhi in [
            ("high", 0.70, 0.85),
            ("extreme", 0.85, 0.95),
        ]:
            slip_zone = SLIPPAGE_BY_ZONE[zone_name] / 100
            for sign_filter, label in [
                (df["taker_pnl_weighted_usd_24h"] > 0, "winning_takers"),
                (df["taker_pnl_weighted_usd_24h"] < 0, "losing_takers"),
            ]:
                sub = df[
                    (df["yes_price_at_anchor"] >= zlo)
                    & (df["yes_price_at_anchor"] < zhi)
                    & sign_filter
                ]
                m = compute_metrics(sub, "NO", slip_zone, fee_drag)
                if m and m["n"] >= 100:
                    flag = "✅" if m["n"] >= MIN_N and m["net_roi"] >= MIN_NET_ROI else " "
                    print(f"  {flag} {zone_name}/{label}: n={m['n']:>5} WR={m['wr']*100:>5.1f}% net_ROI={m['net_roi']*100:>+6.1f}%")

        # Baseline для reference
        print("\n— Baseline: extreme zone NO-fade (no wallet filter)")
        for ext_lo, ext_hi in [(0.85, 0.95)]:
            sub = df[
                (df["yes_price_at_anchor"] >= ext_lo)
                & (df["yes_price_at_anchor"] < ext_hi)
            ]
            slip = SLIPPAGE_BY_ZONE["extreme"] / 100
            m = compute_metrics(sub, "NO", slip, fee_drag)
            if m:
                print(f"    baseline ext[{ext_lo},{ext_hi}): n={m['n']:>5} WR={m['wr']*100:>5.1f}% net_ROI={m['net_roi']*100:>+6.1f}%")

    print(f"\nDone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
