"""D42 Event-driven ML — backfill detector_signals из model predictions.

Архитектура:
1. Load latest d42_event_driven_{window}_*.pkl per window
2. Для каждого window: SELECT ml_features WHERE anchor_hours=anchor_from
3. Inference → predicted_delta_pp
4. Filter |delta| >= 0.05 → emit Signal через d42_event_driven_ml.detect()
5. Resolve outcome через yes_won (markets resolved) или target_yes_price comparison
6. INSERT в detector_signals(source='hf_backfill', resolved=...)

Outcome logic:
  D42 signal.side = YES (predicted rise) or NO (predicted fall).
  При WIN: actual_delta направление совпало с predicted.
  При LOSS: направления противоположные.
  EXPIRED: yes_won IS NULL (market не резолвнулся).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pickle
import sys
import time
from pathlib import Path
from typing import Any

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import duckdb
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("d42_backfill")

CANONICAL_DIR = Path("/mnt/data/canonical")
MODELS_DIR = CANONICAL_DIR / "models"
ML_FEATURES = CANONICAL_DIR / "ml_features.parquet"
SNAPSHOTS = CANONICAL_DIR / "price_snapshots.parquet"
MARKETS = CANONICAL_DIR / "markets_clean.parquet"

WINDOWS: dict[str, tuple[int, int]] = {
    "168_to_24": (168, 24),
    "72_to_24": (72, 24),
    "24_to_4": (24, 4),
}

MIN_DELTA_PP = 0.05  # match d42_event_driven_ml.detect()

_INSERT_SQL = """
INSERT INTO detector_signals (
    detector, market_id, event_id, side,
    confidence, expected_edge_pct, fair_price, current_price,
    horizon_sec, token_id, evidence, generated_at,
    source, resolved, outcome, realized_roi, realized_roi_net, resolved_at
)
VALUES (
    $1, $2, $3, $4,
    $5, $6, $7, $8,
    $9, $10, $11::jsonb, $12,
    'hf_backfill', true, $13, $14, $15, $16
)
ON CONFLICT DO NOTHING
"""


def load_latest_model(window_name: str) -> dict[str, Any] | None:
    """Load newest d42_event_driven_{window}_*.pkl."""
    candidates = sorted(MODELS_DIR.glob(f"d42_event_driven_{window_name}_*.pkl"))
    if not candidates:
        return None
    with candidates[-1].open("rb") as f:
        bundle = pickle.load(f)  # noqa: S301
    bundle["path"] = candidates[-1].name
    return bundle


def fetch_inference_data(window_name: str) -> pd.DataFrame:
    """Load ml_features + JOIN price_snapshots для actual delta + markets для metadata."""
    anchor_from, anchor_to = WINDOWS[window_name]
    con = duckdb.connect(":memory:")
    query = f"""
        SELECT
            f.*,
            m.event_id                    AS market_event_id,
            m.category                    AS market_category,
            m.end_date                    AS market_end_date,
            s_to.yes_price                AS yes_price_at_target,
            s_to.yes_price - f.yes_price_at_anchor AS actual_delta
          FROM read_parquet('{ML_FEATURES}') f
          JOIN read_parquet('{SNAPSHOTS}') s_to
            ON s_to.condition_id = f.condition_id
           AND s_to.hours_to_resolve = {anchor_to}
          JOIN read_parquet('{MARKETS}') m
            ON m.condition_id = f.condition_id
         WHERE f.anchor_hours = {anchor_from}
           AND f.yes_price_at_anchor IS NOT NULL
           AND s_to.yes_price IS NOT NULL
    """
    df = con.execute(query).fetchdf()
    con.close()
    return df


async def backfill_window(window_name: str, *, dry_run: bool, limit: int | None) -> dict[str, int]:
    from datetime import UTC

    import asyncpg

    from app.detectors import d42_event_driven_ml

    stats = {"rows_scanned": 0, "signals_emitted": 0, "inserts_done": 0, "errors": 0}

    bundle = load_latest_model(window_name)
    if bundle is None:
        log.error("no model for window=%s", window_name)
        return stats

    model = bundle["model"]
    feat_cols = bundle["feature_cols"]
    log.info("loaded model=%s features=%d", bundle["path"], len(feat_cols))

    df = fetch_inference_data(window_name)
    if limit:
        df = df.head(limit)
    log.info("inference data rows=%d", len(df))

    # Prepare X for inference — same cols as fit
    X = df[feat_cols].copy().replace([np.inf, -np.inf], np.nan)

    predicted = model.predict(X)

    # Setup asyncpg pool if not dry-run
    pool = None
    if not dry_run:
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("DB_DSN")
        if not dsn:
            raise RuntimeError("DATABASE_URL / DB_DSN required для не-dry-run")
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)

    try:
        for i, (_, row) in enumerate(df.iterrows()):
            stats["rows_scanned"] += 1
            pred = float(predicted[i])
            if abs(pred) < MIN_DELTA_PP:
                continue

            # Build market_event и вызовем detector
            yes_price = float(row["yes_price_at_anchor"])
            anchor_from, _ = WINDOWS[window_name]
            me = {
                "market_id": row["condition_id"],
                "event_id": row.get("market_event_id"),
                "category": row.get("market_category"),
                "current_price": yes_price,
                "hours_to_resolve": float(anchor_from),
                "predicted_delta_pp": pred,
                "window_name": window_name,
                "model_version": bundle["path"],
                "clob_token_ids": None,
            }
            sig = d42_event_driven_ml.detect(me)
            if sig is None:
                continue

            stats["signals_emitted"] += 1

            # Resolve outcome через actual_delta
            actual_delta = float(row.get("actual_delta") or 0)
            if actual_delta == 0:
                outcome = "EXPIRED"
            else:
                win = (pred > 0) == (actual_delta > 0)
                outcome = "WIN" if win else "LOSS"

            # Generated_at: pick a synthetic timestamp (resolve_dt - anchor_from hours)
            end_dt = row.get("market_end_date")
            if hasattr(end_dt, "to_pydatetime"):
                end_dt = end_dt.to_pydatetime()
            if end_dt is None:
                continue
            from datetime import timedelta
            gen_at = end_dt - timedelta(hours=anchor_from)
            if gen_at.tzinfo is None:
                gen_at = gen_at.replace(tzinfo=UTC)
            resolved_at = end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=UTC)

            # Realized ROI: для D42 сложно — используем abs(actual_delta) * 100 как proxy gross
            # win: gross_roi ≈ |actual_delta| * 100 (rough proxy). loss: -100.
            roi_gross = abs(actual_delta) * 100.0 if outcome == "WIN" else (-100.0 if outcome == "LOSS" else 0.0)
            # Net ROI: subtract 2 × fee (~1% on each side) + half-spread
            roi_net = roi_gross - 4.0 if outcome == "WIN" else roi_gross  # rough

            evidence = dict(sig.evidence)
            evidence["backfill"] = True
            evidence["actual_delta_pp"] = round(actual_delta, 4)
            evidence["tx_hash"] = f"backfill_D42_{sig.market_id}_{window_name}"

            if dry_run:
                if stats["signals_emitted"] <= 5:
                    log.info(
                        "[DRY] D42 %s market=%s side=%s pred=%+.3f actual=%+.3f outcome=%s",
                        window_name, sig.market_id, sig.side, pred, actual_delta, outcome,
                    )
            else:
                try:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            _INSERT_SQL,
                            sig.detector, sig.market_id, sig.event_id, sig.side,
                            sig.confidence, sig.expected_edge_pct,
                            sig.fair_price, sig.current_price,
                            sig.horizon_sec, sig.token_id,
                            __import__("json").dumps(evidence),
                            gen_at, outcome, roi_gross, roi_net, resolved_at,
                        )
                        stats["inserts_done"] += 1
                except Exception as exc:
                    log.warning("insert_failed market=%s: %s", sig.market_id, exc)
                    stats["errors"] += 1
    finally:
        if pool is not None:
            await pool.close()

    return stats


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", default="all", help=f"One of {list(WINDOWS.keys())} or 'all'")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    targets = list(WINDOWS.keys()) if args.window == "all" else [args.window]

    total = {"rows_scanned": 0, "signals_emitted": 0, "inserts_done": 0, "errors": 0}
    for window_name in targets:
        log.info("══════════ %s ══════════", window_name)
        t0 = time.time()
        stats = await backfill_window(window_name, dry_run=args.dry_run, limit=args.limit)
        log.info("%s done in %.1fs: %s", window_name, time.time() - t0, stats)
        for k, v in stats.items():
            total[k] = total.get(k, 0) + v

    log.info("═════════ TOTAL ═════════ %s", total)


if __name__ == "__main__":
    asyncio.run(main())
