"""D48 Round-Number Magnet — backtest на canonical trade_buckets.

Hypothesis: на Crypto markets с low dte (≤ 3d) yes_price "магнитит" к 0.50
из-за uncertainty + retail tendency anchor at midpoint. Entry когда vwap
deviates moderately (но не sleepy) от 0.50, exit когда возвращается к 0.50
или TTL.

Entry conditions:
    - category == "Crypto"
    - n_trades >= 5 (real activity)
    - total_volume_usd >= 200
    - vwap_yes in [0.40, 0.46] OR [0.54, 0.60]  ← moderate deviation
    - dte ≤ 3 days (closer to resolution = stronger 50-magnet)
    - dte ≥ 2h (avoid D38 territory)
    - vwap NOT in extreme bins (0.40, 0.60 boundary excluded to avoid edge effects)

Trade rule:
    if vwap < 0.50:  side=YES (price will rise to 0.50)
    if vwap > 0.50:  side=NO   (price will fall to 0.50)

Exit:
    - Take profit when bucket has min_yes_price ≤ 0.51 ≤ max_yes_price (touched 0.50 from either side)
      OR final outcome if market resolves first
    - TTL 24h

Metrics:
    n_signals, wr, wilson_lower_95, mean PnL after fees+slippage,
    per dte_band breakdown
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

CANONICAL_BUCKETS = "/mnt/data/canonical/trade_buckets.parquet"
CANONICAL_MARKETS = "/mnt/data/canonical/markets_clean.parquet"

# Detector parameters
MIN_DTE_HOURS = 2.0
MAX_DTE_HOURS = 72.0  # 3 days
ENTRY_BANDS = [(0.40, 0.46), (0.54, 0.60)]
EXIT_TOUCH_BAND = (0.49, 0.51)
TTL_HOURS = 24
MIN_TRADES = 5
MIN_VOLUME_USD = 200.0

# Trade costs
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
    print(f"  {len(buckets):,} buckets total")

    print(f"Loading {CANONICAL_MARKETS}")
    markets = pq.read_table(
        CANONICAL_MARKETS,
        columns=["condition_id", "category", "end_date", "yes_won"]
    ).to_pandas()
    markets = markets[markets["category"] == "Crypto"]
    markets = markets[markets["yes_won"].isin([0, 1])]
    markets["end_date"] = pd.to_datetime(markets["end_date"], utc=True)
    print(f"  {len(markets):,} resolved Crypto markets")

    buckets = buckets.merge(markets, on="condition_id", how="inner")
    print(f"  {len(buckets):,} crypto buckets")

    # Compute dte at bucket start
    buckets["hour_bucket"] = pd.to_datetime(buckets["hour_bucket"], utc=True)
    buckets["dte_hours"] = (buckets["end_date"] - buckets["hour_bucket"]).dt.total_seconds() / 3600

    # Filter: dte window + liquidity
    mask = (
        (buckets["dte_hours"] >= MIN_DTE_HOURS)
        & (buckets["dte_hours"] <= MAX_DTE_HOURS)
        & (buckets["n_trades"] >= MIN_TRADES)
        & (buckets["total_volume_usd"] >= MIN_VOLUME_USD)
        & buckets["vwap_yes"].notna()
    )
    candidates = buckets[mask].copy()
    print(f"  {len(candidates):,} after dte+liquidity filter")

    # Entry bands
    in_low = candidates["vwap_yes"].between(*ENTRY_BANDS[0], inclusive="left")
    in_high = candidates["vwap_yes"].between(*ENTRY_BANDS[1], inclusive="left")
    entries = candidates[in_low | in_high].copy()
    print(f"  {len(entries):,} candidates in entry bands [0.40,0.46) ∪ [0.54,0.60)")

    # Sort buckets per market for forward lookup
    print("\nBuilding forward lookup")
    buckets_sorted = buckets.sort_values(["condition_id", "hour_bucket"]).reset_index(drop=True)
    # Per market: list of (hour_bucket, vwap, min, max) tuples
    market_lookup: dict[str, list[tuple]] = {}
    for cid, sub in buckets_sorted.groupby("condition_id"):
        market_lookup[cid] = list(
            zip(sub["hour_bucket"], sub["vwap_yes"], sub["min_yes_price"], sub["max_yes_price"])
        )

    print(f"\nSimulating {len(entries):,} entries")
    results = []
    for i, row in enumerate(entries.itertuples(index=False)):
        if i % 5000 == 0 and i > 0:
            print(f"  {i:,}/{len(entries):,}")

        cid = row.condition_id
        entry_ts = row.hour_bucket
        entry_vwap = float(row.vwap_yes)
        # Side: bet on bounce to 0.50
        if entry_vwap < 0.50:
            side = "YES"
            entry_price = entry_vwap
            target = 0.50
        else:
            side = "NO"
            entry_price = 1.0 - entry_vwap
            target = 0.50

        ttl_end = entry_ts + pd.Timedelta(hours=TTL_HOURS)
        market_end = row.end_date

        forward = market_lookup.get(cid, [])
        # Skip past entry bucket itself
        future = [
            (ts, vw, mn, mx)
            for (ts, vw, mn, mx) in forward
            if ts > entry_ts and ts <= ttl_end and ts <= market_end
        ]

        # Check for touch of [0.49, 0.51]
        exit_price = None
        exit_reason = None
        for ts, vw, mn, mx in future:
            if mn <= EXIT_TOUCH_BAND[1] and mx >= EXIT_TOUCH_BAND[0]:
                # Touched the magnet — take profit at 0.50
                if side == "YES":
                    exit_price = 0.50
                else:
                    exit_price = 0.50  # NO at 1-0.50 = 0.50 same
                exit_reason = "touch"
                break

        if exit_price is None:
            # Timeout or resolution
            if market_end <= ttl_end:
                # Market resolved during hold window
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
                # No forward data
                continue

        raw_pnl = exit_price - entry_price
        cost = entry_price * (FEE_RATE + SLIPPAGE_PCT)
        net_pnl = raw_pnl - cost
        won_int = int(net_pnl > 0)

        results.append({
            "condition_id": cid,
            "entry_ts": entry_ts,
            "side": side,
            "entry_vwap": entry_vwap,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "raw_pnl": raw_pnl,
            "net_pnl": net_pnl,
            "won": won_int,
            "exit_reason": exit_reason,
            "dte_hours": row.dte_hours,
            "yes_won": int(row.yes_won),
        })

    df = pd.DataFrame(results)
    print(f"\n{len(df):,} completed trades")

    if len(df) == 0:
        print("No trades. FAIL.")
        return 1

    # === Overall ===
    n = len(df)
    wins = int(df["won"].sum())
    wr = wins / n
    wl = wilson_lower(wins, n)
    mean_pnl = float(df["net_pnl"].mean())
    median_pnl = float(df["net_pnl"].median())
    total_pnl = float(df["net_pnl"].sum())

    print("\n=== OVERALL ===")
    print(f"  n = {n}")
    print(f"  wins = {wins}")
    print(f"  WR = {wr:.3f}")
    print(f"  Wilson lower 95% = {wl:.3f}")
    print(f"  Mean net PnL = {mean_pnl:.4f}")
    print(f"  Median net PnL = {median_pnl:.4f}")
    print(f"  Total net PnL = {total_pnl:.2f}")

    # === Per exit reason ===
    print("\n=== Per exit reason ===")
    for reason, sub in df.groupby("exit_reason"):
        n_r = len(sub)
        wr_r = sub["won"].mean()
        mp = sub["net_pnl"].mean()
        print(f"  {reason:20s}: n={n_r:>6,}  WR={wr_r:.3f}  mean_pnl={mp:.4f}")

    # === Per side ===
    print("\n=== Per side ===")
    for side, sub in df.groupby("side"):
        n_s = len(sub)
        wr_s = sub["won"].mean()
        wl_s = wilson_lower(int(sub["won"].sum()), n_s)
        mp = sub["net_pnl"].mean()
        print(f"  {side}: n={n_s:>6,}  WR={wr_s:.3f}  Wilson={wl_s:.3f}  mean_pnl={mp:.4f}")

    # === Per dte band ===
    print("\n=== Per dte band ===")
    bins = [(2, 8), (8, 24), (24, 48), (48, 72)]
    for lo, hi in bins:
        sub = df[(df["dte_hours"] >= lo) & (df["dte_hours"] < hi)]
        if len(sub) < 30:
            continue
        n_b = len(sub)
        wr_b = sub["won"].mean()
        wl_b = wilson_lower(int(sub["won"].sum()), n_b)
        mp = sub["net_pnl"].mean()
        print(f"  dte=[{lo:>2}-{hi:>2}h): n={n_b:>6,}  WR={wr_b:.3f}  Wilson={wl_b:.3f}  mean_pnl={mp:.4f}")

    # Verdict
    if n < 500:
        verdict = "FAIL_LOW_SAMPLE"
    elif wl < 0.52:
        verdict = "FAIL_WILSON"
    elif mean_pnl < 0.005:
        verdict = "FAIL_PNL"
    else:
        verdict = "PASS"

    print(f"\n=== VERDICT: {verdict} ===")
    print("Pass criteria: n_signals ≥ 500, Wilson lower > 0.52, mean net PnL > 0.005")

    # Write report
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "detector: D48_round_number_magnet",
        f"backtest_date: {date.today()}",
        f"verdict: {verdict}",
        f"n_signals: {n}",
        f"wr: {wr:.4f}",
        f"wilson_lower_95: {wl:.4f}",
        f"mean_net_pnl: {mean_pnl:.4f}",
        f"total_net_pnl: {total_pnl:.2f}",
        f"dte_min_hours: {MIN_DTE_HOURS}",
        f"dte_max_hours: {MAX_DTE_HOURS}",
        f"entry_bands: {ENTRY_BANDS}",
        f"exit_touch_band: {EXIT_TOUCH_BAND}",
        f"ttl_hours: {TTL_HOURS}",
        f"fee_pct: {FEE_RATE}",
        f"slippage_pct: {SLIPPAGE_PCT}",
        "---",
        "",
        "# D48 Round-Number Magnet — Canonical Backtest",
        "",
        f"**Verdict:** {verdict}",
        "",
        "## Overall",
        f"- n = {n}",
        f"- WR = {wr:.3f}",
        f"- Wilson lower = {wl:.3f}",
        f"- Mean net PnL = {mean_pnl:.4f}",
        f"- Median net PnL = {median_pnl:.4f}",
        f"- Total net PnL = {total_pnl:.2f}",
        "",
        "## Per exit reason",
        "",
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
    p.add_argument("--out", default="/app/docs/backtests/D48_round_number_magnet.md")
    sys.exit(main(p.parse_args()))
