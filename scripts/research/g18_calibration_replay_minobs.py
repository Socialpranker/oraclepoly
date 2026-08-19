"""G18 Calibration Edge — replay backtest с ПАРАМЕТРИЗОВАННЫМ min-obs.

Копия g18_calibration_replay.py для диагностики: «съедает ли порог N=30 бины
на короткой истории (данные с мая 2026)?». Единственное отличие от оригинала —
порог накопления/применения калибровки вынесен в CLI `--min-obs` (дефолт 30).

⚠️ prior_weight в bayes_combine остаётся 30 — это ВЕС prior в байес-смешивании,
НЕ порог доступности. Меняем только N-порог (n_empirical/n_hist), не формулу.

⚠️ n<30 → WR ненадёжен (широкий Wilson). Это ДИАГНОСТИКА «сколько детекторов
всплывёт», НЕ рекомендация ставить min-obs<30 в прод.

Usage (на проде, docker):
    python scripts/research/g18_calibration_replay_minobs.py \
        --start 2026-05-01 --end 2026-06-09 --min-obs 20 --out /tmp/g18_minobs20.md
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.database import close_db, fetch, init_db  # noqa: E402

N_BINS = 10
BIN_STEP = 1.0 / N_BINS

# Вес prior в bayes_combine — НЕ порог доступности. Фиксирован как в оригинале.
PRIOR_WEIGHT = 30


def price_to_bin(price: float) -> float:
    return float(np.floor(price * N_BINS) / N_BINS)


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def bayes_combine(prior: float, empirical: float, n_empirical: int, min_obs: int) -> float:
    """Weighted avg prior conf и empirical WR. min_obs — порог применения."""
    if n_empirical < min_obs:
        return prior
    return (PRIOR_WEIGHT * prior + n_empirical * empirical) / (PRIOR_WEIGHT + n_empirical)


async def load_signals(
    start: datetime,
    end: datetime,
    canonical_markets_path: str = "/mnt/data/canonical/markets_clean.parquet",
) -> pd.DataFrame:
    sql = """
    SELECT ds.id, ds.detector, ds.market_id, ds.side, ds.confidence,
           ds.current_price, ds.generated_at, ds.resolved, ds.outcome, ds.realized_roi,
           m.category AS category_pg
    FROM detector_signals ds
    LEFT JOIN markets m ON m.polymarket_id = ds.market_id
    WHERE ds.generated_at >= $1 AND ds.generated_at < $2
      AND ds.resolved = true AND ds.outcome IN ('WIN', 'LOSS')
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

    import pyarrow.parquet as pq

    canonical = pq.read_table(canonical_markets_path, columns=["condition_id", "category"]).to_pandas()
    cat_lookup = dict(zip(canonical["condition_id"], canonical["category"]))

    def resolve_category(row) -> str:
        if pd.notna(row["category_pg"]):
            return row["category_pg"]
        return cat_lookup.get(row["market_id"], "unknown")

    df["category"] = df.apply(resolve_category, axis=1).fillna("unknown")
    return df


def compute_calibrated(df: pd.DataFrame, min_obs: int, gap_days: int = 7) -> pd.DataFrame:
    df = df.sort_values("generated_at").reset_index(drop=True)
    calibrated = np.zeros(len(df))
    n_used = np.zeros(len(df), dtype=int)

    for i, row in df.iterrows():
        cutoff = row["generated_at"] - pd.Timedelta(days=gap_days)
        hist_mask = (
            (df["detector"] == row["detector"])
            & (df["category"] == row["category"])
            & (df["bin"] == row["bin"])
            & (df["generated_at"] < cutoff)
        )
        hist = df[hist_mask]
        n_hist = len(hist)
        n_used[i] = n_hist
        if n_hist >= min_obs:
            empirical_wr = float(hist["won"].mean())
            calibrated[i] = bayes_combine(float(row["confidence"]), empirical_wr, n_hist, min_obs)
        else:
            calibrated[i] = float(row["confidence"])

    df["calibrated"] = calibrated
    df["n_historical"] = n_used
    return df


def evaluate(df: pd.DataFrame, min_obs: int) -> dict:
    out = {}
    for detector, sub in df.groupby("detector"):
        y = sub["won"].values
        raw = sub["confidence"].values
        cal = sub["calibrated"].values
        applied = (sub["n_historical"] >= min_obs).sum()
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


def render_report(per_det: dict, df: pd.DataFrame, args, min_obs: int) -> str:
    total_signals = len(df)
    overall_y = df["won"].values
    brier_raw_all = brier(df["confidence"].values, overall_y)
    brier_cal_all = brier(df["calibrated"].values, overall_y)
    delta_all = brier_cal_all - brier_raw_all
    verdict = "PASS" if delta_all < -0.005 else ("INDETERMINATE" if delta_all < 0 else "FAIL")

    lines = [
        "---",
        "spec: G18_calibration_edge_minobs",
        f"backtest_date: {datetime.now(UTC).date()}",
        f"min_obs: {min_obs}",
        f"verdict: {verdict}",
        f"window: {args.start} -> {args.end}",
        f"n_signals_total: {total_signals}",
        f"brier_delta_overall: {delta_all:+.5f}",
        "---",
        "",
        f"# G18 Replay — min_obs={min_obs}",
        "",
        f"**Overall Δ Brier:** {delta_all:+.5f} (verdict {verdict})",
        "",
        "## Per detector (порог Δ Brier < -0.005, нужно Cal applied > 0)",
        "",
        "| Detector | n | Cal applied | Δ Brier | WR | Raw conf | Cal conf |",
        "|---|---|---|---|---|---|---|",
    ]
    for det in sorted(per_det.keys(), key=lambda d: per_det[d]["brier_delta"]):
        m = per_det[det]
        flag = " ✅" if (m["brier_delta"] < -0.005 and m["n_calibrated_applied"] > 0) else ""
        lines.append(
            f"| {det} | {m['n']} | {m['n_calibrated_applied']} | {m['brier_delta']:+.4f} | "
            f"{m['wr_observed']:.3f} | {m['mean_raw_conf']:.3f} | {m['mean_cal_conf']:.3f} |{flag}"
        )

    coverage = (df["n_historical"] >= min_obs).sum()
    lines += [
        "",
        f"- Cal applied (≥{min_obs} hist): {coverage} ({coverage / total_signals * 100:.1f}%)",
        f"- Fallback to raw: {total_signals - coverage} ({(total_signals - coverage) / total_signals * 100:.1f}%)",
    ]
    return "\n".join(lines) + "\n"


async def main(args):
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    min_obs = args.min_obs

    await init_db()
    try:
        print(f"Loading signals {start} -> {end} (min_obs={min_obs})")
        df = await load_signals(start, end)
    except Exception:
        await close_db()
        raise
    if df.empty:
        print("ERROR: No resolved signals in window")
        await close_db()
        return 1
    print(f"Loaded {len(df):,} resolved signals from {df['detector'].nunique()} detectors")

    df = compute_calibrated(df, min_obs=min_obs, gap_days=7)
    per_det = evaluate(df, min_obs=min_obs)
    report = render_report(per_det, df, args, min_obs)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"\nReport: {out_path}")
    delta = brier(df["calibrated"].values, df["won"].values) - brier(df["confidence"].values, df["won"].values)
    print(f"Overall Δ Brier: {delta:+.5f}")
    await close_db()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-05-01")
    p.add_argument("--end", default="2026-06-09")
    p.add_argument("--min-obs", type=int, default=30)
    p.add_argument("--out", default="/tmp/g18_minobs.md")
    args = p.parse_args()
    sys.exit(asyncio.run(main(args)))
