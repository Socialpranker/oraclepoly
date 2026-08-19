"""G18 Calibration Edge — replay backtest.

For each resolved signal in detector_signals (last 180 days):
  1. Find historical WR for (detector, category, price_bin) computed BEFORE this signal
  2. Combine with detector's raw confidence → calibrated_confidence
  3. Compare Brier score: raw vs calibrated

Output: docs/backtests/G18_calibration_edge.md with per-detector before/after.

Time-aware: для каждого signal используем только signals ВКЛЮЧАЯ
signals_before_t.generated_at < signal.generated_at - 7 days
(7d gap чтобы не быть зависимым от свежих unresolved outcomes).

Usage:
    python scripts/research/g18_calibration_replay.py \
        --start 2025-11-01 --end 2026-05-01 \
        --out docs/backtests/G18_calibration_edge.md
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.database import close_db, fetch, init_db  # noqa: E402

# Bin granularity
N_BINS = 10
BIN_STEP = 1.0 / N_BINS  # 0.0, 0.1, ..., 0.9


def price_to_bin(price: float) -> float:
    """Map 0..1 price to 0.0, 0.1, ..., 0.9."""
    return float(np.floor(price * N_BINS) / N_BINS)


def brier(p: np.ndarray, y: np.ndarray) -> float:
    """Brier score = mean((p - y)²). Lower = better."""
    return float(np.mean((p - y) ** 2))


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson lower 95% CI."""
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = z * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return float(max(0.0, center - margin))


def bayes_combine(prior: float, empirical: float, n_empirical: int,
                  prior_weight: int = 30) -> float:
    """Weighted avg of prior detector confidence and empirical historical WR."""
    if n_empirical < 30:
        return prior
    return (prior_weight * prior + n_empirical * empirical) / (prior_weight + n_empirical)


async def load_signals(start: datetime, end: datetime,
                        canonical_markets_path: str = "/mnt/data/canonical/markets_clean.parquet") -> pd.DataFrame:
    """Load resolved signals + category from BOTH:
       1. Postgres detector_signals (source of truth для confidence + outcome)
       2. Postgres markets (current active markets — category)
       3. Canonical markets_clean.parquet (для resolved/archived markets)
    """
    sql = """
    SELECT
      ds.id,
      ds.detector,
      ds.market_id,
      ds.side,
      ds.confidence,
      ds.current_price,
      ds.generated_at,
      ds.resolved,
      ds.outcome,
      ds.realized_roi,
      m.category AS category_pg
    FROM detector_signals ds
    LEFT JOIN markets m ON m.polymarket_id = ds.market_id
    WHERE ds.generated_at >= $1
      AND ds.generated_at < $2
      AND ds.resolved = true
      AND ds.outcome IN ('WIN', 'LOSS')
    """
    rows = await fetch(sql, start, end)
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["generated_at"] = pd.to_datetime(df["generated_at"], utc=True)
    df["confidence"] = df["confidence"].astype(float)
    df["current_price"] = df["current_price"].astype(float)
    df["won"] = (df["outcome"] == "WIN").astype(int)
    df["bin"] = df["current_price"].apply(price_to_bin)

    # Backfill category from canonical for unmatched signals
    import pyarrow.parquet as pq
    canonical = pq.read_table(canonical_markets_path, columns=["condition_id", "category"]).to_pandas()
    cat_lookup = dict(zip(canonical["condition_id"], canonical["category"]))

    def resolve_category(row) -> str:
        if pd.notna(row["category_pg"]):
            return row["category_pg"]
        return cat_lookup.get(row["market_id"], "unknown")

    df["category"] = df.apply(resolve_category, axis=1).fillna("unknown")
    return df


def compute_calibrated(df: pd.DataFrame, gap_days: int = 7) -> pd.DataFrame:
    """For each row, compute calibrated_confidence using only signals BEFORE generated_at - gap_days.

    Это leak-free: мы не используем будущее.
    """
    df = df.sort_values("generated_at").reset_index(drop=True)
    calibrated = np.zeros(len(df))
    n_used = np.zeros(len(df), dtype=int)

    for i, row in df.iterrows():
        cutoff = row["generated_at"] - pd.Timedelta(days=gap_days)
        # Same (detector, category, bin) historical signals before cutoff
        hist_mask = (
            (df["detector"] == row["detector"])
            & (df["category"] == row["category"])
            & (df["bin"] == row["bin"])
            & (df["generated_at"] < cutoff)
        )
        hist = df[hist_mask]
        n_hist = len(hist)
        n_used[i] = n_hist
        if n_hist >= 30:
            empirical_wr = float(hist["won"].mean())
            calibrated[i] = bayes_combine(float(row["confidence"]), empirical_wr, n_hist)
        else:
            calibrated[i] = float(row["confidence"])  # fallback

    df["calibrated"] = calibrated
    df["n_historical"] = n_used
    return df


def evaluate(df: pd.DataFrame) -> dict:
    """Compute per-detector Brier before/after."""
    out = {}
    for detector, sub in df.groupby("detector"):
        y = sub["won"].values
        raw = sub["confidence"].values
        cal = sub["calibrated"].values
        applied = (sub["n_historical"] >= 30).sum()
        out[detector] = {
            "n": len(sub),
            "n_calibrated_applied": int(applied),
            "brier_raw": brier(raw, y),
            "brier_calibrated": brier(cal, y),
            "brier_delta": brier(cal, y) - brier(raw, y),
            "wr_observed": float(y.mean()),
            "mean_raw_conf": float(raw.mean()),
            "mean_cal_conf": float(cal.mean()),
        }
    return out


def render_report(per_det: dict, df: pd.DataFrame, args) -> str:
    """Render markdown report."""
    total_signals = len(df)
    overall_y = df["won"].values
    overall_raw = df["confidence"].values
    overall_cal = df["calibrated"].values

    brier_raw_all = brier(overall_raw, overall_y)
    brier_cal_all = brier(overall_cal, overall_y)
    delta_all = brier_cal_all - brier_raw_all

    verdict = "PASS" if delta_all < -0.005 else ("INDETERMINATE" if delta_all < 0 else "FAIL")

    lines = [
        "---",
        "spec: G18_calibration_edge",
        f"backtest_date: {datetime.now(UTC).date()}",
        f"verdict: {verdict}",
        f"window_start: {args.start}",
        f"window_end: {args.end}",
        f"n_signals_total: {total_signals}",
        f"brier_raw_overall: {brier_raw_all:.5f}",
        f"brier_calibrated_overall: {brier_cal_all:.5f}",
        f"brier_delta_overall: {delta_all:+.5f}",
        f"pass_threshold: -0.005 (calibrated must improve Brier by at least 0.005)",
        "---",
        "",
        "# G18 Calibration Edge — Replay Backtest",
        "",
        f"**Window:** {args.start} → {args.end}",
        f"**Verdict:** {verdict}",
        "",
        "## Overall",
        "",
        f"- Total signals: {total_signals}",
        f"- Brier (raw confidence): **{brier_raw_all:.5f}**",
        f"- Brier (calibrated): **{brier_cal_all:.5f}**",
        f"- Δ Brier: **{delta_all:+.5f}** (negative = improvement)",
        "",
        "## Per detector",
        "",
        "| Detector | n | Cal applied | Brier raw | Brier cal | Δ Brier | WR | Raw conf | Cal conf |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for det in sorted(per_det.keys(), key=lambda d: -per_det[d]["n"]):
        m = per_det[det]
        lines.append(
            f"| {det} | {m['n']} | {m['n_calibrated_applied']} | "
            f"{m['brier_raw']:.4f} | {m['brier_calibrated']:.4f} | {m['brier_delta']:+.4f} | "
            f"{m['wr_observed']:.3f} | {m['mean_raw_conf']:.3f} | {m['mean_cal_conf']:.3f} |"
        )

    # Per category
    lines += ["", "## Per category", "", "| Category | n | Brier raw | Brier cal | Δ |", "|---|---|---|---|---|"]
    for cat in sorted(df["category"].unique()):
        sub = df[df["category"] == cat]
        if len(sub) < 30:
            continue
        y = sub["won"].values
        r = brier(sub["confidence"].values, y)
        c = brier(sub["calibrated"].values, y)
        lines.append(f"| {cat} | {len(sub)} | {r:.4f} | {c:.4f} | {c-r:+.4f} |")

    # Bin coverage
    lines += ["", "## Bin coverage (how often calibration was applied)", ""]
    coverage = (df["n_historical"] >= 30).sum()
    lines.append(f"- Signals where ≥30 historical samples were available: {coverage} ({coverage/total_signals*100:.1f}%)")
    lines.append(f"- Signals using raw confidence (fallback): {total_signals - coverage} ({(total_signals-coverage)/total_signals*100:.1f}%)")

    return "\n".join(lines) + "\n"


async def main(args):
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)

    await init_db()
    try:
        print(f"Loading signals {start} → {end}")
        df = await load_signals(start, end)
    except Exception:
        await close_db()
        raise
    if df.empty:
        print("ERROR: No resolved signals in window")
        return 1
    print(f"Loaded {len(df):,} resolved signals from {df['detector'].nunique()} detectors")

    print("Computing calibrated confidence (time-aware, 7d gap)")
    df = compute_calibrated(df, gap_days=7)

    print("Evaluating per-detector Brier")
    per_det = evaluate(df)

    report = render_report(per_det, df, args)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"\nReport: {out_path}")

    # Print quick summary
    overall_y = df["won"].values
    delta = brier(df["calibrated"].values, overall_y) - brier(df["confidence"].values, overall_y)
    print(f"Overall Δ Brier: {delta:+.5f}")
    await close_db()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2025-11-01")
    p.add_argument("--end", default="2026-05-01")
    p.add_argument("--out", default="docs/backtests/G18_calibration_edge.md")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args)))
