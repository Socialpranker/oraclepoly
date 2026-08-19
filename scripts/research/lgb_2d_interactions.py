#!/usr/bin/env python3
"""LGB 2D Interactions — find edge в (top_feature × yes_price_at_anchor) cells.

Hypothesis: simple single-feature thresholds не дают edge (все арбитражены).
Edge может существовать в pairwise interactions: top microstructure feature
combined with price zone. Пример: high VPIN AND extreme NO zone → fade NO.

Approach:
  1. Take top-15 features by LGB gain (excluding price aggregates)
  2. For each: bucket the feature into quartiles
  3. Cross-tab with yes_price_at_anchor zones [longshot/low/mid/high/extreme]
  4. Per cell: WR + ROI (NO and YES sides), filter for positive net ROI

Strict gate: n ≥ 500 AND net_ROI ≥ 10%.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ML_FEATURES = "/mnt/data/canonical/ml_features.parquet"
OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)

ANCHORS = [4, 24, 72]

# Features to test (top microstructure + flow, excluding raw price aggregates)
TOP_FEATURES = [
    "vpin_proxy_lifetime",
    "kyle_lambda_proxy",
    "liquidity_quality_proxy",
    "realized_vol_24h",
    "realized_vol_72h",
    "max_drawdown_24h_pct",
    "drift_72h_to_anchor",
    "drift_7d_to_anchor",
    "whale_buy_pressure_24h",
    "retail_taker_avg_size_24h",
    "taker_hhi_proxy_24h",
    "repeat_taker_ratio_24h",
    "vol_24h_concentration",
    "buy_imbalance_asof",
    "buy_ratio_24h_before_anchor",
    "event_yes_sum",
    "event_arb_distance_yes",
    "event_largest_market_share",
    "event_thinnest_market_vol",
]

ZONE_BUCKETS = [
    ("longshot", 0.10, 0.30),
    ("low", 0.30, 0.50),
    ("mid", 0.50, 0.70),
    ("high", 0.70, 0.85),
    ("extreme", 0.85, 0.95),
]

DEFAULT_FEE_PCT = 1.5
SLIPPAGE_PP_BY_ZONE = {
    "longshot": 4.0, "low": 2.5, "mid": 2.0, "high": 2.5, "extreme": 4.0,
}

MIN_N = 500
MIN_NET_ROI = 0.10  # 10%


def zone_label(p):
    for label, lo, hi in ZONE_BUCKETS:
        if lo <= p < hi:
            return label
    return None


def feature_quartile(series, q):
    """Returns string label like 'q0', 'q1', 'q2', 'q3' based on quartile."""
    quantiles = np.unique(np.quantile(series.dropna().values, [0, 0.25, 0.5, 0.75, 1.0]))
    if len(quantiles) < 2:
        return None
    labels = ["q0", "q1", "q2", "q3"][: len(quantiles) - 1]
    return labels[min(np.searchsorted(quantiles[1:], q, side="left"), len(labels) - 1)]


def main():
    print("=" * 70)
    print("LGB 2D INTERACTIONS — feature × price zone")
    print(f"Strict: n≥{MIN_N} AND net_ROI≥{MIN_NET_ROI*100:.0f}%")
    print("=" * 70)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    con.execute("SET threads=4")

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    all_cells: list[dict] = []

    for anchor_h in ANCHORS:
        print(f"\n→ anchor={anchor_h}h", flush=True)
        df = con.execute(
            f"""
            SELECT *
            FROM read_parquet('{ML_FEATURES}')
            WHERE anchor_hours = {anchor_h}
              AND yes_won IS NOT NULL
              AND yes_price_at_anchor >= 0.10
              AND yes_price_at_anchor < 0.95
            """
        ).fetchdf()
        print(f"  rows: {len(df):,}")
        if len(df) < 1000:
            continue

        # Add zone
        df["zone"] = df["yes_price_at_anchor"].apply(zone_label)
        df = df.dropna(subset=["zone"])

        for feat in TOP_FEATURES:
            if feat not in df.columns:
                continue
            sub_df = df.dropna(subset=[feat])
            if len(sub_df) < 1000:
                continue

            # Quartile label per row
            quantiles = np.quantile(sub_df[feat].values, [0, 0.25, 0.5, 0.75, 1.0])
            quantiles = np.unique(quantiles)
            if len(quantiles) < 4:
                continue

            sub_df = sub_df.copy()
            sub_df["q_label"] = pd.cut(
                sub_df[feat],
                bins=quantiles,
                labels=[f"q{i}" for i in range(len(quantiles) - 1)],
                include_lowest=True,
            )

            # Cross-tab: zone × quartile
            for zone in ["longshot", "low", "mid", "high", "extreme"]:
                for q in [f"q{i}" for i in range(4)]:
                    cell = sub_df[(sub_df["zone"] == zone) & (sub_df["q_label"] == q)]
                    n = len(cell)
                    if n < MIN_N:
                        continue
                    avg_yes = cell["yes_price_at_anchor"].mean()
                    if pd.isna(avg_yes) or avg_yes <= 0 or avg_yes >= 1:
                        continue

                    slip = SLIPPAGE_PP_BY_ZONE[zone] / 100
                    fee = DEFAULT_FEE_PCT * 2 / 100

                    for side in ["NO", "YES"]:
                        if side == "NO":
                            wins = (cell["yes_won"] == 0).sum()
                            cost = 1 - avg_yes
                        else:
                            wins = (cell["yes_won"] == 1).sum()
                            cost = avg_yes
                        wr = wins / n
                        cost_net = cost + slip
                        net_roi = (wr * (1 - fee) - cost_net) / cost_net
                        if net_roi < MIN_NET_ROI:
                            continue

                        # Feature value range for the quartile
                        feat_lo = quantiles[int(q[1])]
                        feat_hi = quantiles[int(q[1]) + 1]

                        all_cells.append({
                            "anchor_h": anchor_h,
                            "feature": feat,
                            "feat_quartile": q,
                            "feat_lo": round(float(feat_lo), 5),
                            "feat_hi": round(float(feat_hi), 5),
                            "zone": zone,
                            "side": side,
                            "n": int(n),
                            "wins": int(wins),
                            "wr": round(wr * 100, 2),
                            "avg_cost": round(cost, 4),
                            "net_roi_pct": round(net_roi * 100, 1),
                        })

    if not all_cells:
        print("\n⚠️  No 2D cells passed strict gate.")
        return

    df = pd.DataFrame(all_cells).sort_values("net_roi_pct", ascending=False)
    out_csv = OUT_DIR / f"lgb_2d_interactions_{ts}.csv"
    df.to_csv(out_csv, index=False)
    print(f"\n📊 Wrote {len(df)} cells → {out_csv}")

    print("\n" + "=" * 70)
    print("TOP-30 cells by net_ROI")
    print("=" * 70)
    for _, row in df.head(30).iterrows():
        print(
            f"anchor={row['anchor_h']}h side={row['side']} | "
            f"{row['feature']:<28} ∈ [{row['feat_lo']:>10.4f},{row['feat_hi']:>10.4f}) | "
            f"zone={row['zone']:<8} | "
            f"n={row['n']:>5} WR={row['wr']:>5.1f}% net_ROI={row['net_roi_pct']:+.1f}%"
        )

    print(f"\nDone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
