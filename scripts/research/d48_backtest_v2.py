"""D48 v2 — refined filter: avoid resolution in hold window.

After v1 finding (WR 80% but mean PnL −0.028 due to fat-tail resolution losses):
    - MIN_DTE_HOURS = 24 (was 2)  ← hold window won't catch resolution
    - MAX_DTE_HOURS = 168 (1 week, was 72)
    - TTL_HOURS = 12 (was 24, tighter window)
    - Stricter entry band [0.42, 0.46) ∪ [0.54, 0.58) ← farther from 0.50 = stronger magnet pull
    - Volume gate increased (more conviction)

Output: docs/backtests/D48_round_number_magnet_v2.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

CANONICAL_BUCKETS = "/mnt/data/canonical/trade_buckets.parquet"
CANONICAL_MARKETS = "/mnt/data/canonical/markets_clean.parquet"

# Refined parameters
MIN_DTE_HOURS = 24.0
MAX_DTE_HOURS = 168.0
ENTRY_BANDS = [(0.42, 0.46), (0.54, 0.58)]
EXIT_TOUCH_BAND = (0.49, 0.51)
TTL_HOURS = 12
MIN_TRADES = 8
MIN_VOLUME_USD = 500.0

FEE_RATE = 0.02
SLIPPAGE_PCT = 0.02


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return float(max(0.0, center - margin))


def main(args) -> int:
    print(f"Loading {CANONICAL_BUCKETS}")
    buckets = pq.read_table(CANONICAL_BUCKETS).to_pandas()

    markets = pq.read_table(
        CANONICAL_MARKETS,
        columns=["condition_id", "category", "end_date", "yes_won"]
    ).to_pandas()
    markets = markets[markets["category"] == "Crypto"]
    markets = markets[markets["yes_won"].isin([0, 1])]
    markets["end_date"] = pd.to_datetime(markets["end_date"], utc=True)

    buckets = buckets.merge(markets, on="condition_id", how="inner")
    buckets["hour_bucket"] = pd.to_datetime(buckets["hour_bucket"], utc=True)
    buckets["dte_hours"] = (buckets["end_date"] - buckets["hour_bucket"]).dt.total_seconds() / 3600

    mask = (
        (buckets["dte_hours"] >= MIN_DTE_HOURS)
        & (buckets["dte_hours"] <= MAX_DTE_HOURS)
        & (buckets["n_trades"] >= MIN_TRADES)
        & (buckets["total_volume_usd"] >= MIN_VOLUME_USD)
        & buckets["vwap_yes"].notna()
    )
    candidates = buckets[mask].copy()
    print(f"  {len(candidates):,} candidates after filters")

    in_low = candidates["vwap_yes"].between(*ENTRY_BANDS[0], inclusive="left")
    in_high = candidates["vwap_yes"].between(*ENTRY_BANDS[1], inclusive="left")
    entries = candidates[in_low | in_high].copy()
    print(f"  {len(entries):,} candidates in entry bands {ENTRY_BANDS}")

    print("Building forward lookup")
    buckets_sorted = buckets.sort_values(["condition_id", "hour_bucket"]).reset_index(drop=True)
    market_lookup: dict[str, list[tuple]] = {}
    for cid, sub in buckets_sorted.groupby("condition_id"):
        market_lookup[cid] = list(
            zip(sub["hour_bucket"], sub["vwap_yes"], sub["min_yes_price"], sub["max_yes_price"])
        )

    print(f"\nSimulating {len(entries):,} entries")
    results = []
    for i, row in enumerate(entries.itertuples(index=False)):
        if i % 2000 == 0 and i > 0:
            print(f"  {i:,}/{len(entries):,}")

        cid = row.condition_id
        entry_ts = row.hour_bucket
        entry_vwap = float(row.vwap_yes)
        if entry_vwap < 0.50:
            side = "YES"
            entry_price = entry_vwap
        else:
            side = "NO"
            entry_price = 1.0 - entry_vwap

        ttl_end = entry_ts + pd.Timedelta(hours=TTL_HOURS)
        market_end = row.end_date

        forward = market_lookup.get(cid, [])
        future = [
            (ts, vw, mn, mx)
            for (ts, vw, mn, mx) in forward
            if ts > entry_ts and ts <= ttl_end and ts <= market_end
        ]

        exit_price = None
        exit_reason = None
        for ts, vw, mn, mx in future:
            if mn <= EXIT_TOUCH_BAND[1] and mx >= EXIT_TOUCH_BAND[0]:
                exit_price = 0.50
                exit_reason = "touch"
                break

        if exit_price is None:
            if market_end <= ttl_end:
                won = int(row.yes_won)
                if side == "YES":
                    exit_price = 1.0 if won == 1 else 0.0
                else:
                    exit_price = 1.0 if won == 0 else 0.0
                exit_reason = "resolution"
            elif future:
                last_ts, last_vw, _, _ = future[-1]
                if side == "YES":
                    exit_price = last_vw
                else:
                    exit_price = 1.0 - last_vw
                exit_reason = "ttl_timeout"
            else:
                continue

        raw_pnl = exit_price - entry_price
        cost = entry_price * (FEE_RATE + SLIPPAGE_PCT)
        net_pnl = raw_pnl - cost
        won_int = int(net_pnl > 0)

        results.append({
            "side": side,
            "entry_vwap": entry_vwap,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "raw_pnl": raw_pnl,
            "net_pnl": net_pnl,
            "won": won_int,
            "exit_reason": exit_reason,
            "dte_hours": row.dte_hours,
        })

    df = pd.DataFrame(results)
    n = len(df)
    if n == 0:
        print("No trades")
        return 1

    wins = int(df["won"].sum())
    wr = wins / n
    wl = wilson_lower(wins, n)
    mean_pnl = float(df["net_pnl"].mean())
    median_pnl = float(df["net_pnl"].median())
    total_pnl = float(df["net_pnl"].sum())

    print(f"\n=== OVERALL ===\n  n={n}  WR={wr:.3f}  Wilson={wl:.3f}")
    print(f"  Mean PnL = {mean_pnl:.4f}  Median = {median_pnl:.4f}  Total = {total_pnl:.2f}")

    print("\n=== Per exit reason ===")
    for reason, sub in df.groupby("exit_reason"):
        print(f"  {reason:20s}: n={len(sub):>6,}  WR={sub['won'].mean():.3f}  mean_pnl={sub['net_pnl'].mean():.4f}")

    print("\n=== Per side ===")
    for side, sub in df.groupby("side"):
        wl_s = wilson_lower(int(sub["won"].sum()), len(sub))
        print(f"  {side}: n={len(sub):>6,}  WR={sub['won'].mean():.3f}  Wilson={wl_s:.3f}  mean_pnl={sub['net_pnl'].mean():.4f}")

    print("\n=== Per dte band ===")
    bins = [(24, 48), (48, 72), (72, 120), (120, 168)]
    for lo, hi in bins:
        sub = df[(df["dte_hours"] >= lo) & (df["dte_hours"] < hi)]
        if len(sub) < 30:
            continue
        wl_b = wilson_lower(int(sub["won"].sum()), len(sub))
        print(f"  dte=[{lo:>3}-{hi:>3}h): n={len(sub):>5,}  WR={sub['won'].mean():.3f}  Wilson={wl_b:.3f}  mean_pnl={sub['net_pnl'].mean():.4f}")

    if n < 500:
        verdict = "FAIL_LOW_SAMPLE"
    elif wl < 0.52:
        verdict = "FAIL_WILSON"
    elif mean_pnl < 0.005:
        verdict = "FAIL_PNL"
    else:
        verdict = "PASS"

    print(f"\n=== VERDICT: {verdict} ===")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "detector: D48_round_number_magnet_v2",
        f"backtest_date: {date.today()}",
        f"verdict: {verdict}",
        f"n: {n}", f"wr: {wr:.4f}", f"wilson: {wl:.4f}",
        f"mean_pnl: {mean_pnl:.4f}", f"total_pnl: {total_pnl:.2f}",
        f"dte_window_hours: [{MIN_DTE_HOURS}, {MAX_DTE_HOURS}]",
        f"entry_bands: {ENTRY_BANDS}",
        f"ttl_hours: {TTL_HOURS}",
        "---",
        "",
        "# D48 v2 — Refined Round-Number Magnet Backtest",
        "",
        f"**Verdict:** {verdict}",
        "",
        f"## Overall: n={n}, WR={wr:.3f}, Wilson={wl:.3f}, mean_pnl={mean_pnl:.4f}",
        "",
        "## Per exit reason",
        "| Reason | n | WR | Mean PnL |",
        "|---|---|---|---|",
    ]
    for reason, sub in df.groupby("exit_reason"):
        lines.append(f"| {reason} | {len(sub):,} | {sub['won'].mean():.3f} | {sub['net_pnl'].mean():.4f} |")
    out.write_text("\n".join(lines) + "\n")
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="/app/docs/backtests/D48_round_number_magnet_v2.md")
    sys.exit(main(p.parse_args()))
