#!/usr/bin/env python3
"""G — Monte Carlo для оптимизации sizing (vectorized version).

Оригинальная версия на python-loop по trades была O(N_strategies × N_sims × N_trades).
Vectorized: для каждой strategy один numpy proход по всем sims simultaneously.

Strategies — см. STRATEGIES list ниже.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb
import numpy as np

# === Constants ==============================================================

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"

START_BANKROLL = 5_000.0
N_SIMULATIONS = 5_000
N_TRADES = 820  # ≈ 27.3 trades/day × 30 days (realistic short horizon)
EXCLUDED_CATS = ["Culture"]
ANCHORS = [4, 8, 12, 24, 48, 72]
EXTREME_VOL_GATE = 100_000
FEE_PCT = 5.0
# Honest haircut на raw historical WR — учитывает adverse selection,
# реальный fill rate, latency, vol_gate misclassification.
# Memory: D7b honest_haircut_factor = 0.40-0.50.
HAIRCUT = 0.4

# Per-(anchor_band, zone) WR из d7b_no_favorite_fade.py BUCKET_WR
BUCKET_WR = {
    ("4h", "0.85-0.90"): 0.159,
    ("4h", "0.90-0.95"): 0.104,
    ("4h", "0.95-0.97"): 0.079,
    ("8h", "0.85-0.90"): 0.148,
    ("8h", "0.90-0.95"): 0.121,
    ("8h", "0.95-0.97"): 0.110,
    ("12h", "0.85-0.90"): 0.153,
    ("12h", "0.90-0.95"): 0.132,
    ("12h", "0.95-0.97"): 0.107,
    ("24h", "0.85-0.90"): 0.167,
    ("24h", "0.90-0.95"): 0.135,
    ("24h", "0.95-0.97"): 0.085,
    ("48h", "0.85-0.90"): 0.149,
    ("48h", "0.90-0.95"): 0.128,
    ("48h", "0.95-0.97"): 0.092,
    ("72h", "0.85-0.90"): 0.171,
    ("72h", "0.90-0.95"): 0.124,
    ("72h", "0.95-0.97"): 0.086,
}


def _anchor_band(h: float) -> str:
    if h < 6:
        return "4h"
    if h < 10:
        return "8h"
    if h < 18:
        return "12h"
    if h < 36:
        return "24h"
    if h < 60:
        return "48h"
    return "72h"


def _zone(yes_price: float) -> str | None:
    if yes_price < 0.85 or yes_price >= 0.97:
        return None
    if yes_price < 0.90:
        return "0.85-0.90"
    if yes_price < 0.95:
        return "0.90-0.95"
    return "0.95-0.97"


# === Strategies =============================================================


class Strategy:
    """Sizing strategy с vectorized stake calculation.

    stake_arr = bankroll_arr × min(kelly_frac × f_star × zone_mult, max_stake_pct × zone_mult)

    Per-trade f_star = (p_win × b - q) / b где b = payout - 1.
    """

    def __init__(self, label, max_stake_pct, kelly_frac, zone_mult=None):
        self.label = label
        self.max_stake_pct = max_stake_pct
        self.kelly_frac = kelly_frac
        self.zone_mult = zone_mult or {}

    def zone_multiplier_array(self, zone_codes: np.ndarray) -> np.ndarray:
        """Возвращает per-trade zone multiplier."""
        # zone_codes: 0=0.85-0.90, 1=0.90-0.95, 2=0.95-0.97
        ZONES = ["0.85-0.90", "0.90-0.95", "0.95-0.97"]
        mults = np.array([self.zone_mult.get(z, 1.0) for z in ZONES])
        return mults[zone_codes]


STRATEGIES = [
    Strategy("S0_current_prod", max_stake_pct=0.02, kelly_frac=0.20),
    Strategy("S1_research_recom", max_stake_pct=0.01, kelly_frac=0.15),
    Strategy(
        "S2_zone_x130_x100_x70",
        max_stake_pct=0.02,
        kelly_frac=0.20,
        zone_mult={"0.85-0.90": 1.3, "0.90-0.95": 1.0, "0.95-0.97": 0.7},
    ),
    Strategy(
        "S3_zone_inverse_x70_x100_x130",
        max_stake_pct=0.02,
        kelly_frac=0.20,
        zone_mult={"0.85-0.90": 0.7, "0.90-0.95": 1.0, "0.95-0.97": 1.3},
    ),
    Strategy("S4_half_kelly", max_stake_pct=0.02, kelly_frac=0.10),
    Strategy(
        "S5_full_research",
        max_stake_pct=0.01,
        kelly_frac=0.15,
        zone_mult={"0.85-0.90": 1.3, "0.90-0.95": 1.0, "0.95-0.97": 0.7},
    ),
    Strategy(
        "S6_full_empirical",
        max_stake_pct=0.01,
        kelly_frac=0.15,
        zone_mult={"0.85-0.90": 0.7, "0.90-0.95": 1.0, "0.95-0.97": 1.3},
    ),
]


# === Data loading ============================================================


def load_signals(con):
    """Загрузить D7b extended signals из canonical V2 + apply HFT vol_gate."""
    excl = "', '".join(EXCLUDED_CATS)
    anchors_str = ",".join(str(h) for h in ANCHORS)
    df = con.execute(f"""
        WITH signals AS (
            SELECT
                m.condition_id,
                m.lifetime_volume_usd,
                m.yes_won,
                s.yes_price,
                s.hours_to_resolve,
                ROW_NUMBER() OVER (
                    PARTITION BY m.condition_id ORDER BY s.hours_to_resolve
                ) AS rn
            FROM read_parquet('{MARKETS}') m
            JOIN read_parquet('{SNAPS}') s ON s.condition_id = m.condition_id
            WHERE s.hours_to_resolve IN ({anchors_str})
              AND s.yes_price BETWEEN 0.85 AND 0.97
              AND m.yes_won IS NOT NULL
              AND m.lifetime_volume_usd >= 5000
              AND m.category NOT IN ('{excl}')
        )
        SELECT condition_id, lifetime_volume_usd, yes_won, yes_price, hours_to_resolve
        FROM signals
        WHERE rn = 1
        ORDER BY condition_id
    """).df()

    # Apply vol_gate для extreme zone
    mask = ~((df["yes_price"] >= 0.95) & (df["lifetime_volume_usd"] >= EXTREME_VOL_GATE))
    df = df[mask].copy()
    df["band"] = df["hours_to_resolve"].apply(_anchor_band)
    df["zone"] = df["yes_price"].apply(_zone)
    df = df[df["zone"].notna()].copy()
    df["wr"] = df.apply(lambda r: BUCKET_WR.get((r["band"], r["zone"]), 0.0), axis=1)
    df["cost_no"] = 1.0 - df["yes_price"]
    df["payout"] = (df["yes_won"] == 0).astype(int)
    # Zone code: 0=low, 1=mid, 2=extreme
    zone_to_code = {"0.85-0.90": 0, "0.90-0.95": 1, "0.95-0.97": 2}
    df["zone_code"] = df["zone"].map(zone_to_code)
    return df.reset_index(drop=True)


# === Vectorized simulator ===================================================


def simulate_strategy_vectorized(
    signals: dict,
    strategy: Strategy,
    n_sims: int,
    n_trades: int,
    bankroll_start: float,
    fee_pct: float,
    seed: int,
) -> dict:
    """Vectorized MC: для всех n_sims simultaneously симулируем n_trades.

    Args:
        signals: dict с per-row arrays cost_no, wr, payout, zone_code, n_signals.
    """
    rng = np.random.default_rng(seed)
    n_signals = signals["n"]

    # Sample trade indices: shape (n_sims, n_trades)
    trade_idx = rng.integers(0, n_signals, size=(n_sims, n_trades))

    # Get per-trade attributes
    cost = signals["cost_no"][trade_idx]      # (n_sims, n_trades)
    wr = signals["wr"][trade_idx]
    won = signals["payout"][trade_idx]        # 1=NO win, 0=NO loss
    zone_codes = signals["zone_code"][trade_idx]

    # Zone multiplier per trade
    zone_mults_lookup = np.array(
        [strategy.zone_mult.get(z, 1.0) for z in ["0.85-0.90", "0.90-0.95", "0.95-0.97"]]
    )
    zone_mult = zone_mults_lookup[zone_codes]

    # Kelly f_star per trade (constant across sims)
    payout = 1.0 / np.maximum(cost, 1e-6)
    b = payout - 1.0
    q = 1.0 - wr
    f_star = (wr * b - q) / np.maximum(b, 1e-6)
    f_star = np.maximum(f_star, 0.0)  # cap at 0

    # Per-trade gross PnL multiplier (won=1: 1/cost - 1, won=0: -1)
    gross_mult = np.where(won == 1, payout - 1.0, -1.0)
    fee_mult = -fee_pct / 100.0

    # Sequential bankroll loop (нужен для рекуррентности bankroll → stake → bankroll)
    bankroll = np.full(n_sims, bankroll_start, dtype=np.float64)
    peak = bankroll.copy()
    max_dd = np.zeros(n_sims, dtype=np.float64)
    days_to_double = np.full(n_sims, -1.0)

    avg_trades_per_day = n_trades / 365.0

    haircut = signals.get("haircut", 1.0)

    for t in range(n_trades):
        # Per-trade stake: bankroll × min(kelly × f_star, max_pct) × zone_mult
        kelly_stake = bankroll * strategy.kelly_frac * f_star[:, t] * zone_mult[:, t]
        cap_stake = bankroll * strategy.max_stake_pct * zone_mult[:, t]
        stake = np.minimum(kelly_stake, cap_stake)
        stake = np.where(stake > 0, stake, 0.0)

        # PnL — apply haircut к gross (моделирует execution losses + adverse selection)
        gross = stake * gross_mult[:, t] * haircut
        net_pnl = gross + stake * fee_mult
        bankroll = bankroll + net_pnl
        bankroll = np.maximum(bankroll, 0.0)

        # Track peak + DD
        peak = np.maximum(peak, bankroll)
        dd = np.where(peak > 0, (peak - bankroll) / peak, 0.0)
        max_dd = np.maximum(max_dd, dd)

        # Time to double
        doubled_now = (bankroll >= 2.0 * bankroll_start) & (days_to_double < 0)
        days_to_double = np.where(doubled_now, t / avg_trades_per_day, days_to_double)

    n_doubled = int((days_to_double > 0).sum())
    times = days_to_double[days_to_double > 0]
    # Geometric daily growth rate per sim
    valid_mask = bankroll > 0
    daily_growth = np.zeros(n_sims)
    if valid_mask.any():
        n_days = n_trades / (n_trades / 30.0)  # 30 days
        # daily multiplier: (B_T / B_0)^(1/n_days)
        ratio = bankroll[valid_mask] / bankroll_start
        daily_growth[valid_mask] = np.power(np.maximum(ratio, 1e-10), 1.0 / 30.0) - 1.0

    return {
        "label": strategy.label,
        "median_final": float(np.median(bankroll)),
        "mean_final": float(np.mean(bankroll)),
        "p10_final": float(np.percentile(bankroll, 10)),
        "p25_final": float(np.percentile(bankroll, 25)),
        "p75_final": float(np.percentile(bankroll, 75)),
        "p90_final": float(np.percentile(bankroll, 90)),
        "median_dd_pct": float(np.median(max_dd) * 100),
        "p90_dd_pct": float(np.percentile(max_dd, 90) * 100),
        "median_t2x_days": float(np.median(times)) if n_doubled else None,
        "pct_doubled": n_doubled / n_sims * 100,
        "pct_ruined": float((bankroll < 1.0).sum()) / n_sims * 100,
        "median_daily_growth_pct": float(np.median(daily_growth) * 100),
        "p10_daily_growth_pct": float(np.percentile(daily_growth, 10) * 100),
    }


def main():
    t0 = time.time()
    log_path = Path("/root/oracle/products/g-monte-carlo-sizing/run.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(msg):
        print(msg, flush=True)
        with open(log_path, "a") as f:
            f.write(msg + "\n")

    log(f"G — Monte Carlo Sizing (VECTORIZED)")
    log(f"  N_SIMS={N_SIMULATIONS}  N_TRADES={N_TRADES}  bankroll=${START_BANKROLL:,.0f}")
    log("=" * 100)

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    log("Loading historical D7b extended signals...")
    signals_df = load_signals(con)
    log(f"  Loaded: {len(signals_df):,} signals (post vol_gate)")

    # Convert to numpy dict for vectorized access
    signals = {
        "cost_no": signals_df["cost_no"].to_numpy(dtype=np.float64),
        "wr": signals_df["wr"].to_numpy(dtype=np.float64),
        "payout": signals_df["payout"].to_numpy(dtype=np.int64),
        "zone_code": signals_df["zone_code"].to_numpy(dtype=np.int64),
        "n": len(signals_df),
        "haircut": HAIRCUT,
    }

    # Per-zone WR check
    log("Per-zone empirical WR:")
    for z, group in signals_df.groupby("zone"):
        log(f"  {z}: n={len(group):,}, avg_wr={group['wr'].mean():.1%}")

    log(f"\nRunning {N_SIMULATIONS} sims × {len(STRATEGIES)} strategies...")
    results = []
    for s in STRATEGIES:
        ts = time.time()
        seed = hash(s.label) & 0xFFFFFFFF
        r = simulate_strategy_vectorized(
            signals=signals,
            strategy=s,
            n_sims=N_SIMULATIONS,
            n_trades=N_TRADES,
            bankroll_start=START_BANKROLL,
            fee_pct=FEE_PCT,
            seed=seed,
        )
        results.append(r)
        elapsed = time.time() - ts
        log(f"  {s.label:35s} median ${r['median_final']:>10,.0f} "
            f"p10 ${r['p10_final']:>10,.0f} dd_p90 {r['p90_dd_pct']:>5.1f}% "
            f"doubled {r['pct_doubled']:>5.1f}%  [{elapsed:.0f}s]")

    log("\n" + "=" * 100)
    log("RESULTS — sorted by median final bankroll")
    log("=" * 100)
    log(f"{'strategy':40s} {'median':>11s} {'p10':>11s} {'p90':>11s} "
        f"{'dd_med':>7s} {'dd_p90':>7s} {'t2x_d':>7s} {'doubled':>9s} {'ruined':>7s}")
    log("-" * 110)
    for r in sorted(results, key=lambda x: -x["median_final"]):
        t2x_str = f"{r['median_t2x_days']:.0f}" if r["median_t2x_days"] else "-"
        log(f"{r['label']:40s} ${r['median_final']:>10,.0f} ${r['p10_final']:>10,.0f} "
            f"${r['p90_final']:>10,.0f} {r['median_dd_pct']:>6.1f}% {r['p90_dd_pct']:>6.1f}% "
            f"{t2x_str:>7s} {r['pct_doubled']:>8.1f}% {r['pct_ruined']:>6.1f}%")

    log("\n" + "=" * 100)
    log("WINNERS BY METRIC")
    log("=" * 100)
    winner_median = max(results, key=lambda x: x["median_final"])
    winner_p10 = max(results, key=lambda x: x["p10_final"])
    winner_doubled = max(results, key=lambda x: x["pct_doubled"])
    winner_safest = min(results, key=lambda x: x["p90_dd_pct"])
    winner_low_ruin = min(results, key=lambda x: x["pct_ruined"])

    log(f"  Highest median EOY:    {winner_median['label']:40s} ${winner_median['median_final']:,.0f}")
    log(f"  Best p10 (downside):   {winner_p10['label']:40s} ${winner_p10['p10_final']:,.0f}")
    log(f"  Most likely 2x:        {winner_doubled['label']:40s} {winner_doubled['pct_doubled']:.1f}%")
    log(f"  Lowest p90 DD:         {winner_safest['label']:40s} {winner_safest['p90_dd_pct']:.1f}%")
    log(f"  Lowest ruin rate:      {winner_low_ruin['label']:40s} {winner_low_ruin['pct_ruined']:.1f}%")

    s0 = next(r for r in results if r["label"] == "S0_current_prod")
    best = winner_median
    delta_median = best["median_final"] - s0["median_final"]
    delta_p10 = best["p10_final"] - s0["p10_final"]
    delta_dd = best["p90_dd_pct"] - s0["p90_dd_pct"]

    log(f"\nCURRENT (S0): median ${s0['median_final']:,.0f}, p10 ${s0['p10_final']:,.0f}, "
        f"p90 DD {s0['p90_dd_pct']:.1f}%, ruined {s0['pct_ruined']:.1f}%")
    log(f"BEST median ({best['label']}): median ${best['median_final']:,.0f}, "
        f"p10 ${best['p10_final']:,.0f}, p90 DD {best['p90_dd_pct']:.1f}%, ruined {best['pct_ruined']:.1f}%")
    log(f"\nDelta vs S0 current:")
    log(f"  median: {delta_median:+,.0f}  ({delta_median/max(s0['median_final'],1)*100:+.1f}%)")
    log(f"  p10:    {delta_p10:+,.0f}  ({delta_p10/max(s0['p10_final'],1)*100:+.1f}%)")
    log(f"  p90 DD: {delta_dd:+.1f}pp")

    # Save markdown report
    out_dir = Path("/root/oracle/products/g-monte-carlo-sizing")
    out_path = out_dir / "monte_carlo_sizing.md"

    lines = [
        "# G — Monte Carlo Sizing Strategies (vectorized)",
        "",
        f"**Sims:** {N_SIMULATIONS} per strategy × {N_TRADES} trades · "
        f"start bankroll ${START_BANKROLL:,.0f} · fee {FEE_PCT}%",
        "",
        "## Strategies",
        "",
        "| label | max_stake_pct | kelly | zone mult |",
        "|---|---|---|---|",
    ]
    for s in STRATEGIES:
        zm = ", ".join(f"{k}:{v}" for k, v in s.zone_mult.items()) if s.zone_mult else "-"
        lines.append(f"| {s.label} | {s.max_stake_pct} | {s.kelly_frac} | {zm} |")

    lines.extend([
        "",
        "## Results (sorted by median EOY)",
        "",
        "| strategy | median | p10 | p90 | dd_med | dd_p90 | t2x_days | %doubled | %ruined |",
        "|---|---|---|---|---|---|---|---|---|",
    ])
    for r in sorted(results, key=lambda x: -x["median_final"]):
        t2x_str = f"{r['median_t2x_days']:.0f}" if r["median_t2x_days"] else "-"
        lines.append(
            f"| {r['label']} | ${r['median_final']:,.0f} | ${r['p10_final']:,.0f} | "
            f"${r['p90_final']:,.0f} | {r['median_dd_pct']:.1f}% | {r['p90_dd_pct']:.1f}% | "
            f"{t2x_str} | {r['pct_doubled']:.1f}% | {r['pct_ruined']:.1f}% |"
        )

    lines.extend([
        "",
        "## Winners",
        "",
        f"- Highest median EOY: `{winner_median['label']}` → ${winner_median['median_final']:,.0f}",
        f"- Best p10 (downside): `{winner_p10['label']}` → ${winner_p10['p10_final']:,.0f}",
        f"- Lowest p90 DD: `{winner_safest['label']}` → {winner_safest['p90_dd_pct']:.1f}%",
        f"- Lowest ruin rate: `{winner_low_ruin['label']}` → {winner_low_ruin['pct_ruined']:.1f}%",
        "",
        f"vs S0 current: median ${s0['median_final']:,.0f}, p10 ${s0['p10_final']:,.0f}, "
        f"p90 DD {s0['p90_dd_pct']:.1f}%",
        "",
        f"Delta best vs S0: median {delta_median:+,.0f}  p10 {delta_p10:+,.0f}  DD {delta_dd:+.1f}pp",
    ])
    out_path.write_text("\n".join(lines))
    log(f"\nReport: {out_path}")
    log(f"Total: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
