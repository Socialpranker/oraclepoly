#!/usr/bin/env python3
"""EV-calculator для выбора следующего шага после A2/A2-ext/HFT check.

Считает ожидаемый daily PnL @ $5K bankroll, effort (ч), risk-adjusted score.
Uses canonical V2 dataset для D7b-extended + D17 опций.
"""

from __future__ import annotations

import sys
import time

import duckdb

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"
N_DAYS_DATASET = 821
BANKROLL_USD = 5_000
EXCLUDED_CATS = ["Culture"]

# Фракции от raw → realistic после fills/slippage/timing
HONEST_HAIRCUT = 0.5  # стандарт для D7b-class detectors
HONEST_HAIRCUT_EXTREME = 0.4  # для [0.95, 0.97] — больше adverse selection
HONEST_HAIRCUT_HFT_THIN = 0.4  # для thin markets [0.95, 0.97] — ещё agressive

ANCHORS = [4, 8, 12, 24, 48, 72]


def fetch(con, anchor: int, lo_p: float, hi_p: float, lo_v: float = 5000, hi_v: float = 1e10):
    excl = "', '".join(EXCLUDED_CATS)
    return con.execute(f"""
        SELECT m.condition_id, m.lifetime_volume_usd, m.yes_won, s.yes_price
        FROM read_parquet('{MARKETS}') m
        JOIN read_parquet('{SNAPS}') s ON s.condition_id = m.condition_id
        WHERE s.hours_to_resolve = {anchor}
          AND s.yes_price BETWEEN {lo_p} AND {hi_p}
          AND m.yes_won IS NOT NULL
          AND m.lifetime_volume_usd >= {lo_v}
          AND m.lifetime_volume_usd <  {hi_v}
          AND m.category NOT IN ('{excl}')
    """).df()


def calc_daily_pnl(con, *, label: str, zones, vol_filter, fee_pct: float = 5.0,
                   stake_pct: float = 0.005, haircut: float = HONEST_HAIRCUT) -> dict:
    """Calculate expected daily PnL для combination zones × vol_filter."""
    stake_usd = BANKROLL_USD * stake_pct
    total_n = 0
    total_net_raw = 0.0
    total_won = 0
    by_zone = {}

    for lo_p, hi_p, zone_label in zones:
        zone_n = 0
        zone_pnl = 0.0
        for anchor in ANCHORS:
            df = fetch(con, anchor, lo_p, hi_p, vol_filter[0], vol_filter[1])
            if len(df) == 0:
                continue
            df = df.copy()
            df["cost_no"] = 1 - df["yes_price"]
            df["payout"] = (df["yes_won"] == 0).astype(int)
            # Per-trade PnL: buy 1/cost_no shares, win = 1*shares - stake, lose = -stake
            df["raw_pnl"] = df.apply(
                lambda r: stake_usd * (1 / r["cost_no"] - 1) if r["payout"] == 1 else -stake_usd,
                axis=1,
            )
            df["fee_drag"] = stake_usd * fee_pct / 100.0
            df["net_pnl"] = df["raw_pnl"] - df["fee_drag"]
            zone_n += len(df)
            zone_pnl += df["net_pnl"].sum()
            total_n += len(df)
            total_net_raw += df["net_pnl"].sum()
            total_won += df["payout"].sum()
        by_zone[zone_label] = {"n": zone_n, "pnl_raw": zone_pnl}

    daily_pnl_raw = total_net_raw / N_DAYS_DATASET
    daily_pnl_realistic = daily_pnl_raw * haircut
    trades_per_day = total_n / N_DAYS_DATASET
    wr = total_won / max(total_n, 1)
    annual_realistic = daily_pnl_realistic * 365

    return {
        "label": label,
        "n": total_n,
        "trades_per_day": trades_per_day,
        "wr": wr,
        "daily_pnl_raw": daily_pnl_raw,
        "daily_pnl_realistic": daily_pnl_realistic,
        "annual_realistic": annual_realistic,
        "haircut": haircut,
        "by_zone": by_zone,
    }


def main():
    t0 = time.time()
    print(f"EV Calculator — bankroll ${BANKROLL_USD}, stake 0.5%, haircut {HONEST_HAIRCUT}/{HONEST_HAIRCUT_EXTREME}")
    print(f"Dataset window: {N_DAYS_DATASET} days canonical V2")
    print("=" * 100)

    con = duckdb.connect()
    con.execute('SET memory_limit="3GB"')
    con.execute('SET threads=4')

    # === Option A: D7b current — [0.85, 0.95) all volumes ===
    opt_a = calc_daily_pnl(
        con,
        label="A: D7b CURRENT [0.85, 0.95) all volumes",
        zones=[(0.85, 0.90, "low"), (0.90, 0.95, "high")],
        vol_filter=(5_000, 1e10),
        haircut=HONEST_HAIRCUT,
    )

    # === Option B: D7b EXTENDED [0.85, 0.97) с vol gate для [0.95, 0.97] ===
    # Trick: считаем 2 субзоны отдельно с разными vol filters, суммируем PnL
    # Подзона 1: [0.85, 0.95) all volumes (как D7b current)
    sub1 = calc_daily_pnl(
        con,
        label="sub1",
        zones=[(0.85, 0.90, "low"), (0.90, 0.95, "high")],
        vol_filter=(5_000, 1e10),
        haircut=HONEST_HAIRCUT,
    )
    # Подзона 2: [0.95, 0.97] только thin/mid (vol < $100K)
    sub2 = calc_daily_pnl(
        con,
        label="sub2",
        zones=[(0.95, 0.97, "extreme_lowvol")],
        vol_filter=(5_000, 100_000),
        haircut=HONEST_HAIRCUT_EXTREME,
    )
    opt_b = {
        "label": "B: D7b EXTENDED [0.85, 0.97) + vol_gate <$100K for extreme",
        "n": sub1["n"] + sub2["n"],
        "trades_per_day": sub1["trades_per_day"] + sub2["trades_per_day"],
        "wr": (sub1["wr"] * sub1["n"] + sub2["wr"] * sub2["n"]) / max(sub1["n"] + sub2["n"], 1),
        "daily_pnl_realistic": sub1["daily_pnl_realistic"] + sub2["daily_pnl_realistic"],
        "annual_realistic": (sub1["daily_pnl_realistic"] + sub2["daily_pnl_realistic"]) * 365,
        "haircut": "mixed (0.5/0.4)",
        "marginal_over_a": (sub1["daily_pnl_realistic"] + sub2["daily_pnl_realistic"]) - opt_a["daily_pnl_realistic"],
    }

    # === Option C: D17-LowVolExtremeZone — ТОЛЬКО [0.95, 0.97] thin only ===
    opt_c = calc_daily_pnl(
        con,
        label="C: D17 NEW [0.95, 0.97] thin markets ($5K-$30K)",
        zones=[(0.95, 0.97, "extreme_thin")],
        vol_filter=(5_000, 30_000),
        haircut=HONEST_HAIRCUT_HFT_THIN,
    )

    # Print
    for opt in [opt_a, opt_b, opt_c]:
        print(f"\n--- {opt['label']} ---")
        print(f"  n total: {opt['n']:,}")
        print(f"  trades/day: {opt['trades_per_day']:.1f}")
        print(f"  WR: {opt['wr']:.1%}")
        print(f"  Daily PnL realistic: ${opt['daily_pnl_realistic']:+.2f}")
        print(f"  Annual realistic: ${opt['annual_realistic']:+,.0f}")
        if "marginal_over_a" in opt:
            print(f"  Marginal vs Option A: ${opt['marginal_over_a']:+.2f}/day = ${opt['marginal_over_a']*365:+,.0f}/year")
        print(f"  Haircut applied: {opt['haircut']}")

    # Final scoring
    print("\n" + "=" * 100)
    print("SCORING TABLE — daily realistic PnL × confidence × ease")
    print("=" * 100)
    print(f"{'option':80} {'daily_PnL':>10} {'annual':>10}")
    print("-" * 100)
    for opt in [opt_a, opt_b, opt_c]:
        print(f"{opt['label']:80} ${opt['daily_pnl_realistic']:>+8.2f} ${opt['annual_realistic']:>+9,.0f}")

    print(f"\nTotal: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
