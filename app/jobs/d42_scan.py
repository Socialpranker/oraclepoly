"""D42 Scan — emit EventDrivenML signals из live_ml_features.

D42 = LGBM Regressor предсказывает изменение цены (delta), не outcome.
Per-window модели (anchor_from → anchor_to): 168→24h, 72→24h, 24→4h.

Поток (live mode, паттерн scripts/research/standalone_v2_predictor.py:load_features_live):
1. Загрузить 3 модели из /mnt/data/canonical/models/d42_event_driven_{window}_*.pkl
2. Прочитать свежие фичи из live_ml_features (anchor_hours ∈ {24,72,168}, computed_at < 60 мин)
3. Per-window: X = rows[feature_cols] → model.predict(X) → predicted_delta_pp
4. d42_event_driven_ml.detect(market_event) — emit YES/NO когда |delta| ≥ 0.05
5. repo.insert() в detector_signals (source='live_v2')

Per-event dedup: outcomes коррелированы → один сигнал на event. Windows
обрабатываются от long-horizon (168) к short (24); первый эмитнувший сигнал
на event блокирует остальные windows на тот же event в этом проходе.

Idempotency: synthetic tx_hash на 4h bucket (UNIQUE detector+evidence->>'tx_hash').

⚠ Фичи берутся при anchor_hours == anchor_from (точка решения). current_price =
yes_price_at_anchor. Модели trained на canonical (14 мая); live_ml_features
schema совместима (feature_cols ⊆ table columns, verified 2026-06-09).
"""

from __future__ import annotations

import pickle
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import structlog

from app.detectors import d42_event_driven_ml
from app.detectors.contract import Signal
from app.repositories.detector_signal_repository import get_detector_signal_repository
from app.services.database import fetch
from app.services.detector_helpers import bucket_tx_hash, fetch_open_event_ids

logger = structlog.get_logger()

DETECTOR_NAME = "D42_event_driven_ml"
MODELS_DIR = Path("/mnt/data/canonical/models")

# window_name → (anchor_from, anchor_to). Совпадает с fit/backfill WINDOWS.
WINDOWS: dict[str, tuple[int, int]] = {
    "168_to_24": (168, 24),
    "72_to_24": (72, 24),
    "24_to_4": (24, 4),
}

# anchor_from значения для SQL-фильтра live_ml_features.
_ANCHOR_FROM = sorted({af for af, _ in WINDOWS.values()})  # [24, 72, 168]

# Колонки live_ml_features, которые НЕ coerce'им в float (id/categorical/datetime).
# market_event_id — из _TRIGGER_SQL (m.event_id AS market_event_id). БЕЗ него
# _coerce_features делал to_numeric("ev1", errors="coerce") → NaN → dedup
# open_events ломался (event_id→None, проверка `is not None` отсекала skip).
_KEEP_AS_IS = {
    "condition_id",
    "market_id",
    "event_id",
    "market_event_id",
    "category",
    "market_category",
    "clob_token_ids",
    "computed_at",
    "anchor_hours",
}

_TRIGGER_SQL = """
SELECT lf.*,
       m.event_id AS market_event_id,
       m.category AS market_category,
       m.clob_token_ids
  FROM live_ml_features lf
  JOIN markets m ON m.condition_id = lf.market_id
 WHERE m.is_active = true
   AND m.resolved = false
   AND m.end_date IS NOT NULL
   AND m.end_date > now() + INTERVAL '2 hours'
   AND lf.computed_at > now() - INTERVAL '60 minutes'
   AND lf.anchor_hours = ANY($1::int[])
"""


def _load_models() -> dict[str, dict[str, Any]]:
    """Load newest d42_event_driven_{window}_*.pkl per window.

    Graceful: window без модели на диске пропускается (warning), не падаем.
    """
    models: dict[str, dict[str, Any]] = {}
    for window_name in WINDOWS:
        candidates = sorted(MODELS_DIR.glob(f"d42_event_driven_{window_name}_*.pkl"))
        if not candidates:
            logger.warning("d42_scan_no_model", window=window_name)
            continue
        latest = candidates[-1]
        with latest.open("rb") as f:
            bundle = pickle.load(f)  # noqa: S301 — trusted model artifact on prod disk
        models[window_name] = {
            "model": bundle["model"],
            "feature_cols": bundle["feature_cols"],
            "version": latest.name,
        }
        logger.info(
            "d42_scan_model_loaded",
            window=window_name,
            version=latest.name,
            n_features=len(bundle["feature_cols"]),
        )
    return models


def _coerce_features(df: pd.DataFrame) -> pd.DataFrame:
    """Postgres NUMERIC → Decimal → object; LightGBM требует float. Coerce."""
    for col in df.columns:
        if col in _KEEP_AS_IS:
            continue
        if df[col].dtype == "object":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _normalize_event_id(raw: Any) -> str | None:
    """pandas row.get() может вернуть NaN (float) или numpy-тип вместо str/None.

    NaN != NaN и `NaN in set` ведёт себя непредсказуемо между версиями pandas
    (py3.11 CI vs py3.13 local) → dedup `event_id in open_events` ломался на CI.
    Нормализуем: NaN/None → None, иначе str.
    """
    if raw is None:
        return None
    try:
        if pd.isna(raw):
            return None
    except (ValueError, TypeError):
        pass
    return str(raw)


def _build_market_event(row: pd.Series, predicted_delta: float, window_name: str, version: str) -> dict[str, Any]:
    """market_event dict для d42_event_driven_ml.detect()."""
    anchor_from, _ = WINDOWS[window_name]
    now = datetime.now(UTC)
    market_id = str(row["market_id"])
    event_id = _normalize_event_id(row.get("market_event_id"))
    return {
        "market_id": market_id,
        "event_id": event_id,
        "category": row.get("market_category"),
        "current_price": float(row["yes_price_at_anchor"]),
        "hours_to_resolve": float(anchor_from),
        "predicted_delta_pp": predicted_delta,
        "window_name": window_name,
        "model_version": version,
        "clob_token_ids": row.get("clob_token_ids"),
        "tx_hash": bucket_tx_hash(bucket_id=market_id, ts=now, namespace="d42"),
    }


async def run_d42_scan() -> dict[str, Any]:
    models = _load_models()
    if not models:
        logger.error("d42_scan_no_models_loaded")
        return {"markets": 0, "signals": 0, "inserted": 0, "windows_run": 0, "skipped_open_event": 0}

    rows = await fetch(_TRIGGER_SQL, _ANCHOR_FROM)
    if not rows:
        logger.info("d42_scan_no_live_features")
        return {"markets": 0, "signals": 0, "inserted": 0, "windows_run": 0, "skipped_open_event": 0}

    df = _coerce_features(pd.DataFrame([dict(r) for r in rows]))

    open_events = await fetch_open_event_ids(DETECTOR_NAME)
    repo = get_detector_signal_repository()

    n_signals = 0
    n_inserted = 0
    n_skipped_open = 0
    n_windows_run = 0

    # Long-horizon первым: при конфликте на одном event сигнал ставит 168_to_24.
    for window_name in WINDOWS:
        info = models.get(window_name)
        if info is None:
            continue
        anchor_from, _ = WINDOWS[window_name]
        sub = df[df["anchor_hours"] == anchor_from]
        if sub.empty:
            continue

        feature_cols = info["feature_cols"]
        missing = [c for c in feature_cols if c not in sub.columns]
        if missing:
            logger.error("d42_scan_schema_mismatch", window=window_name, missing=missing[:10])
            continue

        n_windows_run += 1
        X = sub[feature_cols].copy().replace([np.inf, -np.inf], np.nan)
        predicted = info["model"].predict(X)

        for i, (_, row) in enumerate(sub.iterrows()):
            event_id = _normalize_event_id(row.get("market_event_id"))
            if event_id is not None and event_id in open_events:
                n_skipped_open += 1
                continue

            yes_price = row.get("yes_price_at_anchor")
            # Skip placeholder price (broken book ordering bug 2026-05-14): иначе
            # модель видит synthetic 0.5 и emit'ит fake edge.
            if yes_price is None or float(yes_price) == 0.5 or not (0 < float(yes_price) < 1):
                continue

            market_event = _build_market_event(row, float(predicted[i]), window_name, info["version"])
            signal: Signal | None = d42_event_driven_ml.detect(market_event)
            if signal is None:
                continue

            n_signals += 1
            try:
                if await repo.insert(signal):
                    n_inserted += 1
                    if event_id is not None:
                        open_events.add(event_id)
            except Exception as exc:  # noqa: BLE001 — insert не должен ронять scan
                logger.warning("d42_scan_insert_failed", error=str(exc), market=signal.market_id, window=window_name)

    logger.info(
        "d42_scan_completed",
        markets=len(df),
        windows_run=n_windows_run,
        signals=n_signals,
        inserted=n_inserted,
        skipped_open_event=n_skipped_open,
    )
    return {
        "markets": len(df),
        "signals": n_signals,
        "inserted": n_inserted,
        "windows_run": n_windows_run,
        "skipped_open_event": n_skipped_open,
    }
