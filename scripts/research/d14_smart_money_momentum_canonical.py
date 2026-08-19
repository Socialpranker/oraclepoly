#!/usr/bin/env python3
"""D14 SmartMoneyMomentum — inverse D13 hypothesis.

D13 показал: retail-only takers (smart_money_taker_share < 0.05) в extreme
zone → fade NO works (+9-12% net ROI).

Inverse: что если **smart money concentrated buying** в неэкстремальных zones
указывает на informed conviction?

Hypotheses:
  H1: smart_share > 0.5 в mid zone (0.30-0.70) AND price was rising
      (current > anchor) → mirror smart direction (BUY same side they bought)
  H2: smart_share > 0.3 в low zone (0.10-0.30) → BUY YES (they see undervalued)
  H3: top_decile_share > 0.3 anywhere → mirror their volume-weighted side

Approach:
  - На canonical ml_features_v2 (включает wallet quality 24h pre-anchor)
  - Need taker direction signal (BUY/SELL ratio для smart wallets specifically)
  - В canonical wallet_score_at_anchor у нас есть smart_money_taker_volume
    но не split by buy/sell

Сейчас testing с aggregated direction proxy:
  - buy_imbalance_asof — общий buy ratio (proxy для market direction)
  - taker_pnl_weighted_usd_24h — weighted PnL track record
  - smart_money_taker_share — что доля trades от smart wallets

Strict gate: n ≥ 500, net_ROI ≥ 5%.
"""

from __future__ import annotations

import sys
import time

import duckdb

ML_V2 = "/mnt/data/canonical/ml_features_v2.parquet"

DEFAULT_FEE_PCT = 1.25  # mid-category taker (см. _backtest_params.py for per-category breakdown)
SLIPPAGE_BY_ZONE = {
    "longshot": 4.0, "low": 2.5, "mid": 2.0, "high": 2.5, "extreme": 4.0,
}
MIN_N = 500
MIN_NET_ROI = 0.05


def zone_for(p):
    if p < 0.30: return "longshot", 0.10, 0.30
    if p < 0.50: return "low", 0.30, 0.50
    if p < 0.70: return "mid", 0.50, 0.70
    if p < 0.85: return "high", 0.70, 0.85
    return "extreme", 0.85, 0.95


def compute_metrics(df, side, slip_pp, fee_drag):
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
    cost_net = cost + slip_pp / 100
    net_roi = (wr * (1 - fee_drag) - cost_net) / cost_net
    return {"n": n, "wr": wr, "avg_price": avg_price, "net_roi": net_roi}


def main():
    print("=" * 70, flush=True)
    print("D14 SmartMoneyMomentum — Canonical Research", flush=True)
    print("=" * 70, flush=True)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=4")

    fee_drag = DEFAULT_FEE_PCT * 2 / 100

    # Universe: any zone, any anchor, has wallet quality data
    print("\n→ Loading universe...", flush=True)
    df_all = con.execute(
        f"""
        SELECT yes_price_at_anchor, yes_won, anchor_hours,
               smart_money_taker_share, top_decile_taker_share,
               taker_roi_weighted_24h, taker_pnl_weighted_usd_24h,
               n_takers_24h_before, taker_volume_usd_total_24h,
               buy_imbalance_asof, drift_72h_to_anchor, category
        FROM read_parquet('{ML_V2}')
        WHERE yes_won IS NOT NULL
          AND yes_price_at_anchor >= 0.10
          AND yes_price_at_anchor < 0.95
          AND n_takers_24h_before >= 3
        """
    ).fetchdf()
    print(f"   total rows with wallet data: {len(df_all):,}", flush=True)

    # ─── H1: Smart concentration в mid/low zones — FOLLOW direction ───
    print("\n— H1: smart_share > X в low/mid zones — direction follow", flush=True)
    print(f"{'filter':<55} {'side':<5} {'n':>6} {'WR':>7} {'cost':>7} {'netROI':>8}", flush=True)
    print("-" * 90, flush=True)

    for anchor_h in [4, 24, 72]:
        df_a = df_all[df_all["anchor_hours"] == anchor_h]

        for zone_name, zlo, zhi in [
            ("longshot", 0.10, 0.30),
            ("low", 0.30, 0.50),
            ("mid", 0.50, 0.70),
            ("high", 0.70, 0.85),
        ]:
            slip = SLIPPAGE_BY_ZONE[zone_name]

            for smart_thr in [0.3, 0.5, 0.7]:
                sub = df_a[
                    (df_a["yes_price_at_anchor"] >= zlo)
                    & (df_a["yes_price_at_anchor"] < zhi)
                    & (df_a["smart_money_taker_share"] > smart_thr)
                ]
                if len(sub) < 100:
                    continue

                # Direction proxy: buy_imbalance_asof — last hour buy ratio
                # If smart in market AND positive buy momentum → BUY YES (mirror)
                # If smart in market AND negative buy momentum → BUY NO (smart dumping)
                buy_pos = sub[sub["buy_imbalance_asof"] > 0.6]  # strong buy
                sell_pos = sub[sub["buy_imbalance_asof"] < 0.4]  # strong sell

                if len(buy_pos) >= 100:
                    m = compute_metrics(buy_pos, "YES", slip, fee_drag)
                    if m and m["net_roi"] >= MIN_NET_ROI:
                        flag = "✅" if m["n"] >= MIN_N else " "
                        print(
                            f"{flag} a{anchor_h}h {zone_name} smart>{smart_thr:.1f} buy>0.6 → YES "
                            f"{'':<10} n={m['n']:>5} WR={m['wr']*100:>5.1f}% cost={m['avg_price']:.3f} "
                            f"ROI={m['net_roi']*100:>+6.1f}%",
                            flush=True,
                        )

                if len(sell_pos) >= 100:
                    m = compute_metrics(sell_pos, "NO", slip, fee_drag)
                    if m and m["net_roi"] >= MIN_NET_ROI:
                        flag = "✅" if m["n"] >= MIN_N else " "
                        print(
                            f"{flag} a{anchor_h}h {zone_name} smart>{smart_thr:.1f} buy<0.4 → NO  "
                            f"{'':<10} n={m['n']:>5} WR={m['wr']*100:>5.1f}% cost={m['avg_price']:.3f} "
                            f"ROI={m['net_roi']*100:>+6.1f}%",
                            flush=True,
                        )

    # ─── H2: Top decile concentration → mirror ───
    print("\n— H2: top_decile_share > X — mirror direction", flush=True)
    print("-" * 90, flush=True)

    for anchor_h in [4, 24, 72]:
        df_a = df_all[df_all["anchor_hours"] == anchor_h]

        for zone_name, zlo, zhi in [
            ("longshot", 0.10, 0.30),
            ("low", 0.30, 0.50),
            ("mid", 0.50, 0.70),
            ("high", 0.70, 0.85),
        ]:
            slip = SLIPPAGE_BY_ZONE[zone_name]
            for top_thr in [0.2, 0.3, 0.5]:
                sub = df_a[
                    (df_a["yes_price_at_anchor"] >= zlo)
                    & (df_a["yes_price_at_anchor"] < zhi)
                    & (df_a["top_decile_taker_share"] > top_thr)
                ]
                if len(sub) < 100:
                    continue

                # Mirror logic: top decile entered → assume they're right → mirror
                # buy_imbalance > 0.5 means majority buy → likely bullish → BUY YES
                buy_pos = sub[sub["buy_imbalance_asof"] > 0.55]
                sell_pos = sub[sub["buy_imbalance_asof"] < 0.45]

                for direction_df, side, label in [
                    (buy_pos, "YES", "buy>0.55"),
                    (sell_pos, "NO", "buy<0.45"),
                ]:
                    if len(direction_df) < 100:
                        continue
                    m = compute_metrics(direction_df, side, slip, fee_drag)
                    if m and m["net_roi"] >= MIN_NET_ROI:
                        flag = "✅" if m["n"] >= MIN_N else " "
                        print(
                            f"{flag} a{anchor_h}h {zone_name} top>{top_thr:.1f} {label} → {side} "
                            f"n={m['n']:>5} WR={m['wr']*100:>5.1f}% cost={m['avg_price']:.3f} "
                            f"ROI={m['net_roi']*100:>+6.1f}%",
                            flush=True,
                        )

    # ─── H3: Winning takers (positive PnL) momentum ───
    print("\n— H3: taker_pnl_weighted > 0 (winning takers in market)", flush=True)
    print("-" * 90, flush=True)

    for anchor_h in [4, 24, 72]:
        df_a = df_all[df_all["anchor_hours"] == anchor_h]
        for zone_name, zlo, zhi in [
            ("longshot", 0.10, 0.30),
            ("low", 0.30, 0.50),
            ("mid", 0.50, 0.70),
            ("high", 0.70, 0.85),
        ]:
            slip = SLIPPAGE_BY_ZONE[zone_name]
            sub = df_a[
                (df_a["yes_price_at_anchor"] >= zlo)
                & (df_a["yes_price_at_anchor"] < zhi)
                & (df_a["taker_pnl_weighted_usd_24h"] > 0)
                & (df_a["taker_roi_weighted_24h"] > 10)  # winners with track record
            ]
            if len(sub) < 100:
                continue
            buy_pos = sub[sub["buy_imbalance_asof"] > 0.55]
            sell_pos = sub[sub["buy_imbalance_asof"] < 0.45]

            for direction_df, side, label in [
                (buy_pos, "YES", "buy>0.55"),
                (sell_pos, "NO", "buy<0.45"),
            ]:
                if len(direction_df) < 100:
                    continue
                m = compute_metrics(direction_df, side, slip, fee_drag)
                if m and m["net_roi"] >= MIN_NET_ROI:
                    flag = "✅" if m["n"] >= MIN_N else " "
                    print(
                        f"{flag} a{anchor_h}h {zone_name} winning_takers {label} → {side} "
                        f"n={m['n']:>5} WR={m['wr']*100:>5.1f}% cost={m['avg_price']:.3f} "
                        f"ROI={m['net_roi']*100:>+6.1f}%",
                        flush=True,
                    )

    # ─── H4 (NEW): Smart momentum + drift confirmation ───
    print("\n— H4: smart > 0.3 AND drift_72h confirms (sustained move)", flush=True)
    print("-" * 90, flush=True)

    for anchor_h in [4, 24, 72]:
        df_a = df_all[df_all["anchor_hours"] == anchor_h]
        for zone_name, zlo, zhi in [
            ("low", 0.30, 0.50),
            ("mid", 0.50, 0.70),
            ("high", 0.70, 0.85),
        ]:
            slip = SLIPPAGE_BY_ZONE[zone_name]
            # Up trend (positive drift = price rising) + smart buying → BUY YES
            up_trend = df_a[
                (df_a["yes_price_at_anchor"] >= zlo)
                & (df_a["yes_price_at_anchor"] < zhi)
                & (df_a["smart_money_taker_share"] > 0.3)
                & (df_a["drift_72h_to_anchor"] > 0.05)
            ]
            if len(up_trend) >= 100:
                m = compute_metrics(up_trend, "YES", slip, fee_drag)
                if m and m["net_roi"] >= MIN_NET_ROI:
                    flag = "✅" if m["n"] >= MIN_N else " "
                    print(
                        f"{flag} a{anchor_h}h {zone_name} smart>0.3 drift>+5% → YES "
                        f"n={m['n']:>5} WR={m['wr']*100:>5.1f}% cost={m['avg_price']:.3f} "
                        f"ROI={m['net_roi']*100:>+6.1f}%",
                        flush=True,
                    )

            # Down trend (negative drift) + smart selling → BUY NO
            down_trend = df_a[
                (df_a["yes_price_at_anchor"] >= zlo)
                & (df_a["yes_price_at_anchor"] < zhi)
                & (df_a["smart_money_taker_share"] > 0.3)
                & (df_a["drift_72h_to_anchor"] < -0.05)
            ]
            if len(down_trend) >= 100:
                m = compute_metrics(down_trend, "NO", slip, fee_drag)
                if m and m["net_roi"] >= MIN_NET_ROI:
                    flag = "✅" if m["n"] >= MIN_N else " "
                    print(
                        f"{flag} a{anchor_h}h {zone_name} smart>0.3 drift<-5% → NO  "
                        f"n={m['n']:>5} WR={m['wr']*100:>5.1f}% cost={m['avg_price']:.3f} "
                        f"ROI={m['net_roi']*100:>+6.1f}%",
                        flush=True,
                    )

    print(f"\nDone in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
