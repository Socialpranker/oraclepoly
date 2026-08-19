"""D47 MeanReversion 20d/5d — backtest на canonical.

Hypothesis: на long-horizon markets (dte ≥ 30d), когда current_price падает до
20-day rolling min, толпа over-extrapolates downward move. Через 5 days price
mean-reverts. Entry YES at trough, exit fixed +5d.

Source: canonical price_snapshots.parquet (snapshot points {4,8,12,24,48,72,168,720}h
до end_date). Converting к calendar dates через `trade_ts → date`, last snapshot of
the day per market.

Filter:
    - dte_days ≥ 30 (mean-reversion edge на distant horizons)
    - yes_price ∈ [0.05, 0.40] (avoid extreme tails)
    - lifetime_volume_usd ≥ $5K
    - n_trades ≥ 50
    - category ∈ {Politics, World & Economy, Tech & AI, Crypto} — slow narratives

Metrics:
    - n_signals, wr, wilson_lower_95, mean_net_pnl_pct
    - per category breakdown
    - per year breakdown
    - per asset (если crypto strike)

Pass criteria:
    - n_signals ≥ 500
    - wilson_lower_95(wr) > 0.52
    - Wilson lower > 0.50 в каждой whitelisted category

Usage on VPS:
    docker exec oraclepoly-backend python /app/scripts/research/d47_backtest.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


CANONICAL_MARKETS = "/mnt/data/canonical/markets_clean.parquet"
CANONICAL_SNAPS = "/mnt/data/canonical/price_snapshots.parquet"

# Detector parameters
LOOKBACK_DAYS = 20
HOLD_DAYS = 5
MIN_DTE_DAYS = 30
PRICE_RANGE = (0.05, 0.40)
MIN_VOLUME_USD = 5000
MIN_TRADES = 50
CATEGORY_WHITELIST = frozenset({"Politics", "World & Economy", "Tech & AI", "Crypto"})

# Trade costs
FEE_RATE = 0.02
SLIPPAGE_PCT = 0.025


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return float(max(0.0, center - margin))


def build_daily_series(snaps: pd.DataFrame) -> pd.DataFrame:
    """price_snapshots → daily series (last snapshot of the day per market).

    Output columns: condition_id, snapshot_date, yes_price, end_date_proxy.
    """
    snaps = snaps.copy()
    snaps["ts"] = pd.to_datetime(snaps["trade_ts"], unit="s", utc=True)
    snaps["snapshot_date"] = snaps["ts"].dt.date
    # estimated end_date = snapshot_ts + hours_to_resolve
    snaps["est_end_ts"] = snaps["ts"] + pd.to_timedelta(snaps["hours_to_resolve"], unit="h")
    snaps = snaps.sort_values("ts")
    daily = snaps.drop_duplicates(subset=["condition_id", "snapshot_date"], keep="last")
    return daily[["condition_id", "snapshot_date", "yes_price", "est_end_ts"]]


def find_entry_points(daily: pd.DataFrame, markets_meta: pd.DataFrame) -> pd.DataFrame:
    """Find moments where yes_price ≤ rolling 20d min AND dte ≥ 30 AND in price range."""
    daily = daily.merge(
        markets_meta[["condition_id", "category", "yes_won", "end_date"]],
        on="condition_id",
        how="inner",
    )
    daily["end_date"] = pd.to_datetime(daily["end_date"], utc=True)
    daily["snapshot_date_ts"] = pd.to_datetime(daily["snapshot_date"]).dt.tz_localize("UTC")
    daily["dte_days"] = (daily["end_date"] - daily["snapshot_date_ts"]).dt.total_seconds() / 86400

    # Sort и rolling min per market
    daily = daily.sort_values(["condition_id", "snapshot_date"]).reset_index(drop=True)
    daily["rolling_min_20d"] = daily.groupby("condition_id")["yes_price"].transform(
        lambda s: s.rolling(window=LOOKBACK_DAYS, min_periods=10).min()
    )

    # Mask: entry conditions
    mask = (
        (daily["yes_price"] <= daily["rolling_min_20d"])
        & (daily["yes_price"] >= PRICE_RANGE[0])
        & (daily["yes_price"] <= PRICE_RANGE[1])
        & (daily["dte_days"] >= MIN_DTE_DAYS)
        & (daily["category"].isin(CATEGORY_WHITELIST))
        & (daily["rolling_min_20d"].notna())
    )
    entries = daily[mask].copy()
    return entries


def compute_exit_pnl(entries: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """For each entry, find exit price at +HOLD_DAYS (or resolution if sooner)."""
    # Build lookup: (condition_id, snapshot_date) → yes_price
    daily_lookup = daily.set_index(["condition_id", "snapshot_date"])["yes_price"].to_dict()

    exit_prices = []
    for _, row in entries.iterrows():
        cid = row["condition_id"]
        entry_dt = row["snapshot_date"]
        target_dt = entry_dt + pd.Timedelta(days=HOLD_DAYS)
        end_dt = row["end_date"].date()

        # Try exact target date, else closest earlier within HOLD_DAYS
        exit_price = None
        for delta in range(0, HOLD_DAYS + 1):
            check_dt = (target_dt - pd.Timedelta(days=delta)).date()
            if check_dt < entry_dt:
                break
            if check_dt > end_dt:
                # Market resolved → final outcome
                exit_price = 1.0 if row["yes_won"] == 1 else 0.0
                break
            exit_price = daily_lookup.get((cid, check_dt))
            if exit_price is not None:
                break

        if exit_price is None:
            # Fallback: market ended without intermediate snapshot in 5d window
            exit_price = 1.0 if row["yes_won"] == 1 else 0.0

        exit_prices.append(exit_price)

    entries = entries.copy()
    entries["exit_price"] = exit_prices
    entries["raw_pnl"] = entries["exit_price"] - entries["yes_price"]
    # Costs: fee + slippage on entry only (we hold to exit, mostly resolution)
    entries["cost"] = entries["yes_price"] * (FEE_RATE + SLIPPAGE_PCT)
    entries["net_pnl"] = entries["raw_pnl"] - entries["cost"]
    entries["won"] = (entries["net_pnl"] > 0).astype(int)
    return entries


def print_summary(entries: pd.DataFrame, title: str = "Overall") -> dict:
    n = len(entries)
    if n == 0:
        print(f"\n{title}: n=0")
        return {}
    wins = int(entries["won"].sum())
    wr = wins / n
    wl = wilson_lower(wins, n)
    mean_pnl_pct = entries["net_pnl"].mean() / entries["yes_price"].mean() * 100
    median_pnl = entries["net_pnl"].median()
    print(f"\n{title}:")
    print(f"  n_signals = {n}")
    print(f"  wins = {wins}")
    print(f"  WR = {wr:.3f}")
    print(f"  Wilson lower 95% = {wl:.3f}")
    print(f"  Mean net PnL = {entries['net_pnl'].mean():.4f}")
    print(f"  Mean net PnL % entry = {mean_pnl_pct:.2f}%")
    print(f"  Median net PnL = {median_pnl:.4f}")
    return {"n": n, "wins": wins, "wr": wr, "wilson": wl, "mean_pnl": float(entries["net_pnl"].mean())}


def render_report(entries: pd.DataFrame, out_path: Path, args) -> None:
    overall = print_summary(entries, "OVERALL")

    per_cat = {}
    print("\n=== Per category ===")
    for cat in sorted(entries["category"].unique()):
        sub = entries[entries["category"] == cat]
        per_cat[cat] = print_summary(sub, f"Category={cat}")

    per_year = {}
    print("\n=== Per year ===")
    entries["year"] = pd.to_datetime(entries["snapshot_date"]).dt.year
    for year in sorted(entries["year"].unique()):
        sub = entries[entries["year"] == year]
        per_year[year] = print_summary(sub, f"Year={year}")

    # Verdict
    wl_overall = overall.get("wilson", 0.0)
    n_total = overall.get("n", 0)

    all_cat_pass = all(c.get("wilson", 0.0) > 0.50 for c in per_cat.values() if c.get("n", 0) >= 30)

    if n_total < 500:
        verdict = "FAIL_LOW_SAMPLE"
    elif wl_overall < 0.52:
        verdict = "FAIL_OVERALL_WR"
    elif not all_cat_pass:
        verdict = "FAIL_CATEGORY"
    else:
        verdict = "PASS"

    print(f"\n=== VERDICT: {verdict} ===")
    print(f"  Pass criteria: n_signals ≥ 500, wilson_lower > 0.52 overall, wilson_lower > 0.50 per category")

    # Write markdown
    lines = [
        "---",
        "detector: D47_mean_reversion_20d_5d",
        f"backtest_date: {date.today()}",
        f"verdict: {verdict}",
        f"n_signals: {n_total}",
        f"wilson_lower_95: {wl_overall:.4f}",
        f"hold_days: {HOLD_DAYS}",
        f"lookback_days: {LOOKBACK_DAYS}",
        f"min_dte_days: {MIN_DTE_DAYS}",
        f"price_range: {PRICE_RANGE}",
        f"fee_pct: {FEE_RATE}",
        f"slippage_pct: {SLIPPAGE_PCT}",
        "---",
        "",
        "# D47 MeanReversion 20d/5d — Canonical Backtest",
        "",
        f"**Verdict:** {verdict}",
        "",
        "## Overall",
        f"- n_signals = {overall.get('n', 0)}",
        f"- WR = {overall.get('wr', 0):.3f}",
        f"- Wilson lower = {wl_overall:.3f}",
        f"- Mean net PnL = {overall.get('mean_pnl', 0):.4f}",
        "",
        "## Per category",
        "",
        "| Category | n | WR | Wilson | Mean PnL |",
        "|---|---|---|---|---|",
    ]
    for cat, m in per_cat.items():
        if m.get("n", 0) >= 10:
            lines.append(f"| {cat} | {m['n']} | {m['wr']:.3f} | {m['wilson']:.3f} | {m['mean_pnl']:.4f} |")
    lines.append("")
    lines.append("## Per year")
    lines.append("")
    lines.append("| Year | n | WR | Wilson |")
    lines.append("|---|---|---|---|")
    for year, m in per_year.items():
        if m.get("n", 0) >= 10:
            lines.append(f"| {year} | {m['n']} | {m['wr']:.3f} | {m['wilson']:.3f} |")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    print(f"\nReport: {out_path}")


def main(args) -> int:
    print(f"Loading {CANONICAL_MARKETS}")
    markets = pq.read_table(
        CANONICAL_MARKETS,
        columns=["condition_id", "category", "end_date", "yes_won",
                 "lifetime_volume_usd", "n_trades_total"],
    ).to_pandas()
    markets = markets[
        (markets["lifetime_volume_usd"] >= MIN_VOLUME_USD)
        & (markets["n_trades_total"] >= MIN_TRADES)
        & markets["yes_won"].isin([0, 1])
        & markets["category"].isin(CATEGORY_WHITELIST)
    ]
    print(f"  {len(markets):,} markets passing meta filter")

    print(f"Loading {CANONICAL_SNAPS}")
    snaps = pq.read_table(CANONICAL_SNAPS).to_pandas()
    snaps = snaps[snaps["condition_id"].isin(markets["condition_id"])]
    print(f"  {len(snaps):,} snapshots for filtered markets")

    print("Building daily series")
    daily = build_daily_series(snaps)
    print(f"  {len(daily):,} daily snapshot points")

    print("Finding entry points")
    entries = find_entry_points(daily, markets)
    print(f"  {len(entries):,} candidate entries")

    print("Computing exit PnL")
    entries = compute_exit_pnl(entries, daily)
    print(f"  {len(entries):,} entries with exit prices")

    render_report(entries, Path(args.out), args)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="/app/docs/backtests/D47_mean_reversion_20d_5d.md")
    sys.exit(main(p.parse_args()))
