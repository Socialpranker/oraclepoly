#!/usr/bin/env python3
"""standalone_v2_predictor — shadow run для standalone LGB V2 model.

V2 (MVP backtest mode): прогоняет standalone LGB на резолвнувшихся markets за последний
N дней через ml_features.parquet, INSERT predictions в БД для сравнения с L2 monolith.

Production-grade live mode требует live feature computation (snapshot price + trade flow
+ wallet flow per anchor) — отдельная задача после MVP.

Запуск:
    /root/oracle/.venv-research/bin/python /root/oracle/scripts/research/standalone_v2_predictor.py
    /root/oracle/.venv-research/bin/python /root/oracle/scripts/research/standalone_v2_predictor.py --backfill-days 30

Идемпотентен через UNIQUE (market_id, anchor_hours, model_version, hour-truncated inference_at).
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

import asyncpg
import duckdb
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

MODELS_DIR = Path("/mnt/data/canonical/models")
ML_FEATURES_PATH = "/mnt/data/canonical/ml_features.parquet"

SHADOW_ANCHORS = [24, 72, 168]

def _get_db_dsn() -> str:
    """Resolve DB DSN from env at connect time (fail-fast, no hardcoded secret).

    Resolved lazily — importing this module must not require DB_DSN (tests
    import it to exercise pure functions without a database).
    """
    try:
        return os.environ["DB_DSN"]
    except KeyError as exc:
        raise SystemExit(
            "DB_DSN env var must be set "
            "(in prod cron it is exported as $DATABASE_URL inside the container; "
            "for manual runs export DB_DSN before invoking this script)"
        ) from exc


def load_models() -> dict[int, dict]:
    """Load latest standalone_lgb_v2_anchorNh_*.pkl per anchor."""
    models: dict[int, dict] = {}
    for anchor_h in SHADOW_ANCHORS:
        candidates = sorted(MODELS_DIR.glob(f"standalone_lgb_v2_anchor{anchor_h}h_*.pkl"))
        if not candidates:
            logger.warning("no_model_found anchor=%dh", anchor_h)
            continue
        latest = candidates[-1]
        with open(latest, "rb") as f:
            payload = pickle.load(f)
        models[anchor_h] = {
            "model": payload["model"],
            "feat_cols": payload["feat_cols"],
            "version": latest.stem,
        }
        logger.info("loaded_model anchor=%dh version=%s feat_cols=%d",
                    anchor_h, models[anchor_h]["version"], len(payload["feat_cols"]))
    return models


def load_features_backtest(backfill_days: int) -> pd.DataFrame:
    """BACKTEST mode: load ml_features из canonical parquet (резолвнутые markets).

    Используется для retrospective evaluation моделей на резолвнутых markets.
    """
    con = duckdb.connect()
    con.execute("SET memory_limit='2GB'")
    cutoff_year_month = pd.Timestamp.now() - pd.Timedelta(days=backfill_days)
    cutoff_y = cutoff_year_month.year
    cutoff_m = cutoff_year_month.month

    df = con.execute(f"""
        SELECT *
        FROM read_parquet('{ML_FEATURES_PATH}')
        WHERE anchor_hours IN (24, 72, 168)
          AND yes_won IS NOT NULL
          AND (
            resolve_year > {cutoff_y}
            OR (resolve_year = {cutoff_y} AND resolve_month >= {cutoff_m})
          )
    """).df()
    logger.info(
        "loaded_features_backtest rows=%d markets=%d events=%d",
        len(df),
        df["condition_id"].nunique() if not df.empty else 0,
        df["event_id"].nunique() if not df.empty else 0,
    )
    return df


async def load_features_live() -> pd.DataFrame:
    """LIVE mode: load свежие features из live_ml_features Postgres table.

    Этот path читает features активных markets для inference в virtual mode.
    Использует новый pipeline: live_*_writers + live_ml_features_builder.
    """
    conn = await asyncpg.connect(_get_db_dsn())
    try:
        rows = await conn.fetch(
            """
            SELECT *
              FROM live_ml_features
             WHERE anchor_hours IN (24, 72, 168)
               AND computed_at > now() - INTERVAL '1 hour'
            """
        )
    finally:
        await conn.close()
    if not rows:
        logger.warning("loaded_features_live rows=0 (live_ml_features пуст или stale)")
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    # В live_ml_features колонка называется market_id, но models trained с condition_id.
    # Маппим обратно для совместимости с predict_all() (который использует condition_id).
    df["condition_id"] = df["market_id"]

    # Postgres NUMERIC возвращается как Decimal → pandas object. LightGBM требует float.
    # Coerce все non-id/categorical/datetime колонки в float.
    keep_as_is = {"condition_id", "market_id", "event_id", "category", "computed_at", "anchor_hours"}
    for col in df.columns:
        if col in keep_as_is:
            continue
        if df[col].dtype == "object":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    logger.info(
        "loaded_features_live rows=%d markets=%d events=%d",
        len(df),
        df["condition_id"].nunique(),
        df["event_id"].nunique() if "event_id" in df.columns else 0,
    )
    return df


def decide_side_ev(p_yes: float, yes_price: float) -> tuple[str, float, float]:
    """Side + EV (%) на $1 stake из вероятности p_yes и рыночной yes_price.

    ev_yes/ev_no — ожидаемый ROI % покупки соответствующей стороны.
    Сторона = та, у которой EV выше (argmax). p_yes ДОЛЖЕН быть уже
    откалиброван — side-решение чувствительно к overshoot raw-модели.
    """
    ev_yes = (p_yes - yes_price) / yes_price * 100
    ev_no = ((1 - p_yes) - (1 - yes_price)) / (1 - yes_price) * 100
    side = "YES" if ev_yes >= ev_no else "NO"
    return side, ev_yes, ev_no


async def predict_all(
    df: pd.DataFrame,
    models: dict[int, dict],
    cals: dict | None = None,
    lgb_cals: dict | None = None,
) -> list[tuple]:
    """Run prediction для каждого row. Returns list of tuples for INSERT.

    Калибрует raw p_yes через _calibrate chain (G18→LGB→Beta→raw) ДО EV/side.
    cals/lgb_cals — пред-загруженные per-detector калибраторы (None = raw passthrough,
    cold-start safe). См. план atomic-launching-lecun.md.
    """
    from app.services.detector_signal_service import _calibrate

    cal_src_counts: dict[str, int] = {}
    rows = []
    for anchor_h, info in models.items():
        sub = df[df['anchor_hours'] == anchor_h].copy()
        if sub.empty:
            continue

        sub['category'] = sub['category'].astype('category')
        feat_cols = info['feat_cols']
        X = sub[feat_cols].copy()
        X['category'] = X['category'].astype('category')

        probs = info['model'].predict_proba(X)[:, 1]
        sub['p_yes'] = probs

        for r in sub.itertuples(index=False):
            yes_price = getattr(r, 'yes_price_at_anchor', None)
            # Skip predictions без real price — иначе модель видит synthetic mid-zone
            # и emit'ит fake huge edge. Bug discovered 2026-05-14: 76% snapshots
            # имели placeholder 0.5 из broken REST book ordering.
            if yes_price is None or yes_price == 0.5 or not (0 < yes_price < 1):
                continue

            raw_p = float(r.p_yes)
            # Провизорные side/edge из RAW p — для train/serve консистентности
            # build_features (калибратор фитился на фичах из raw-стороны синкнутых
            # сигналов). Финальные side/EV считаем уже из calibrated_p ниже.
            prov_side, prov_ev_yes, prov_ev_no = decide_side_ev(raw_p, yes_price)
            cal_row = {
                "raw_p": raw_p,
                "current_price": float(yes_price),
                "side": prov_side,
                "expected_edge_pct": max(prov_ev_yes, prov_ev_no),
                "horizon_sec": int(getattr(r, 'horizon_days', 0) or 0) * 86400,
                "category": str(r.category) if r.category is not None else None,
                # anchor_band — чтобы LGB-калибратор получил anchor-фичу (build_features
                # читает anchor_band). Без неё anchor one-hot = 0. См. lgb_features.py.
                "anchor_band": f"{int(anchor_h)}h",
                "evidence": {"anchor_hours": int(anchor_h)},
            }
            calibrated_p, cal_src = await _calibrate(
                raw_p, "STANDALONE_V2_LGB", cals or {}, row=cal_row, lgb_cals=lgb_cals or {},
                anchor_hours=int(anchor_h),
            )
            cal_src_counts[cal_src] = cal_src_counts.get(cal_src, 0) + 1

            # Финальное решение — на калиброванном p (это и есть фикс).
            _side, ev_yes, ev_no = decide_side_ev(calibrated_p, yes_price)

            rows.append((
                r.condition_id,                  # market_id (mapped)
                r.condition_id,
                r.event_id,
                int(anchor_h),
                info['version'],
                float(calibrated_p),  # калиброванный p_yes (был raw — root cause YES -5.4%)
                float(yes_price) if yes_price is not None else None,
                float(ev_yes) if ev_yes is not None else None,
                float(ev_no) if ev_no is not None else None,
                str(r.category) if r.category is not None else None,
                float(r.horizon_days) if r.horizon_days is not None else None,
                len(feat_cols),
            ))
    logger.info("calibration_sources=%s", cal_src_counts)
    return rows


async def insert_predictions(conn: asyncpg.Connection, rows: list[tuple]) -> int:
    """Bulk INSERT с idempotency через ON CONFLICT."""
    if not rows:
        return 0
    await conn.executemany(
        """
        INSERT INTO standalone_v2_predictions (
            market_id, condition_id, event_id, anchor_hours, model_version,
            predicted_p_yes, market_yes_price, ev_yes_pct, ev_no_pct,
            category, horizon_days, n_features_used
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT DO NOTHING
        """,
        rows,
    )
    return len(rows)


async def backfill_outcomes(conn: asyncpg.Connection) -> int:
    """Update yes_won + brier_squared для predictions where market теперь резолвнут.

    Источник истины: markets.winner ('yes'/'no'). Раньше резолюция шла через
    oracle_snapshots, но та таблица потеряна при миграции 2026-05-11 (0 строк) →
    brier_squared оставался NULL у ВСЕХ predictions → точность ML не измерялась.
    markets.winner жив (обновляется resolve-outcomes cron) и хранит ровно 'yes'/'no'.
    Фикс 2026-06-02.
    """
    n = await conn.execute("""
        UPDATE standalone_v2_predictions p
        SET yes_won = CASE WHEN LOWER(m.winner) = 'yes' THEN 1 ELSE 0 END,
            resolved_at = NOW(),
            brier_squared = POWER(
                p.predicted_p_yes - (CASE WHEN LOWER(m.winner) = 'yes' THEN 1.0 ELSE 0.0 END),
                2
            )
        FROM markets m
        WHERE p.resolved_at IS NULL
          AND m.condition_id = p.condition_id
          AND LOWER(m.winner) IN ('yes', 'no')
    """)
    return int(n.split()[-1]) if n else 0


async def compute_metrics(conn: asyncpg.Connection) -> None:
    """Print summary metrics for resolved predictions."""
    rows = await conn.fetch("""
        SELECT
            anchor_hours,
            COUNT(*)::int AS n,
            AVG(brier_squared)::float8 AS mean_brier,
            COUNT(DISTINCT event_id)::int AS n_events
        FROM standalone_v2_predictions
        WHERE brier_squared IS NOT NULL
        GROUP BY anchor_hours
        ORDER BY anchor_hours
    """)
    if not rows:
        logger.info("no_resolved_predictions yet")
        return
    logger.info("=== shadow_v2 metrics ===")
    for r in rows:
        # mean_brier может быть None если в группе нет строк с brier — guard от %.4f-краша
        mean_brier = r['mean_brier']
        if mean_brier is None:
            logger.info("  anchor=%dh: n=%d events=%d mean_brier=n/a",
                        r['anchor_hours'], r['n'], r['n_events'])
        else:
            logger.info("  anchor=%dh: n=%d events=%d mean_brier=%.4f",
                        r['anchor_hours'], r['n'], r['n_events'], mean_brier)


async def main_async(*, mode: str, backfill_days: int) -> None:
    t0 = time.time()
    logger.info("standalone_v2_predictor_start mode=%s backfill_days=%d", mode, backfill_days)

    models = load_models()
    if not models:
        logger.error("no_models_loaded")
        sys.exit(1)

    if mode == "live":
        df = await load_features_live()
    else:
        df = load_features_backtest(backfill_days)

    if df.empty:
        logger.warning("no_features_loaded")
        return

    # Schema validation: убедиться что все feat_cols модели присутствуют в df.
    # Это критично для live mode (live_ml_features schema может разойтись с тренировкой).
    for anchor_h, info in models.items():
        missing = [c for c in info["feat_cols"] if c not in df.columns]
        if missing:
            logger.error(
                "schema_mismatch anchor=%dh missing_features=%s — модель не сможет inference",
                anchor_h,
                missing[:10],
            )
            sys.exit(2)

    # Load per-detector calibrators (same chain as monolith) для применения к
    # raw p_yes ДО EV/side. Требует global pool → init_db()/close_db().
    from app.services.database import close_db, init_db
    from app.services.detector_signal_service import _load_l1_calibrators, _load_lgb_calibrators

    await init_db()
    try:
        cals = await _load_l1_calibrators()
        lgb_cals = await _load_lgb_calibrators()
    finally:
        await close_db()
    logger.info("loaded_calibrators beta=%d lgb_active=%d", len(cals), len(lgb_cals))

    rows = await predict_all(df, models, cals=cals, lgb_cals=lgb_cals)
    logger.info("computed_predictions=%d", len(rows))

    conn = await asyncpg.connect(_get_db_dsn())
    try:
        n_inserted = await insert_predictions(conn, rows)
        logger.info("inserted=%d", n_inserted)

        n_resolved = await backfill_outcomes(conn)
        logger.info("backfilled_outcomes=%d", n_resolved)

        await compute_metrics(conn)
    finally:
        await conn.close()

    logger.info("standalone_v2_predictor_done elapsed=%.1fs", time.time() - t0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["live", "backtest"],
        default="backtest",
        help=(
            "live: read live_ml_features Postgres table (active markets, для virtual trading); "
            "backtest: read ml_features.parquet (resolved markets, для retrospective audit). "
            "Default backtest для backward-compat."
        ),
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=30,
        help="Look back N days для backtest mode (default 30, ignored in live)",
    )
    args = parser.parse_args()
    asyncio.run(main_async(mode=args.mode, backfill_days=args.backfill_days))


if __name__ == "__main__":
    main()
