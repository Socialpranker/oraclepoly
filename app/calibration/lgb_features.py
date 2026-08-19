"""Shared feature builder для LgbCalibrator (fit + inference).

КРИТИЧНО: одна логика для train и predict — иначе train/serve skew.
Все feature names стабильны (порядок берётся в fit через sorted()), а тут
гарантируется, что одни и те же ключи генерируются для signal row и для
fresh inference row.

Schema источника:
    detector_signals + LEFT JOIN markets (см. _FETCH_FRESH_SIGNALS_SQL и
    fit-job SQL — оба должны давать одинаковый набор полей).

Required fields (могут быть None — заполняются defaults):
    confidence / raw_p, expected_edge_pct, current_price, fair_price,
    side, horizon_sec, event_size, category, volume_24h, evidence(jsonb).

Tier 3.1 features (2026-05-19): leak-free rolling-window метрики.
Эти 3 поля считаются caller-ом (fit-job SQL window functions, inference SQL
correlated subquery) и кладутся в row до передачи в build_features:
    - hours_to_resolution_at_signal: (market.end_date - generated_at)/3600
      fallback на evidence.hours_to_resolve если markets.end_date нет.
    - recent_wr_30d: rolling WR detector'а за 30 дней до signal
      (strictly < generated_at — leak-free).
    - detector_recent_count_30d: сколько signals детектор выдал за 30 дней
      до signal (proxy на зрелость детектора / sample size).

Evidence-fields опциональны — отсутствуют у части детекторов; missing → 0/default.
"""

from __future__ import annotations

import json
import math
from typing import Any


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse_zone_top(zone_str: str | None) -> float:
    """Parse '0.95-0.97' → 0.97. Returns 0.5 if can't parse."""
    if not zone_str or "-" not in zone_str:
        return 0.5
    try:
        return float(zone_str.split("-")[1])
    except (ValueError, IndexError, TypeError):
        return 0.5


def _evidence_dict(ev: Any) -> dict[str, Any]:
    if not ev:
        return {}
    if isinstance(ev, dict):
        return ev
    if isinstance(ev, str):
        try:
            return json.loads(ev)
        except (ValueError, TypeError):
            return {}
    return {}


def build_features(row: dict[str, Any]) -> dict[str, float]:
    """Превращает detector_signal row (+ market join) в feature dict.

    row может прийти из fit (signal+resolved+outcome) или inference (signal+market).
    Контракт ключей одинаковый — outcome не нужен для feature build.

    Tier 3.1 контракт (вычисляется в SQL caller'ом, в row кладутся как-есть):
        hours_to_resolution_at_signal: float | None  — fallback на evidence.hours_to_resolve
        recent_wr_30d:                  float | None  — fallback 0.5 (no info)
        detector_recent_count_30d:      int   | None  — fallback 0
    """
    ev = _evidence_dict(row.get("evidence"))

    # anchor_band fallback: STANDALONE кладёт anchor_hours (int) в evidence, не anchor_band.
    # Без этого anchor one-hot всегда 0 (мёртвая фича). Деривим band из anchor_hours.
    if "anchor_band" not in ev:
        _ah = ev.get("anchor_hours")
        if _ah is not None:
            ev = {**ev, "anchor_band": f"{int(_ah)}h"}

    side = str(row.get("side") or "YES")
    cur = _safe_float(row.get("current_price"), 0.5)
    fair = _safe_float(row.get("fair_price"), 0.5)
    raw_p = _safe_float(row.get("confidence") if "confidence" in row else row.get("raw_p"), 0.5)

    # entry_price side-aware (как в pilot и detector_signal_service SQL)
    entry = cur if side == "YES" else 1.0 - cur

    cat = (row.get("category") or "").lower()

    # Tier 3.1: hours_to_resolution — приоритет SQL-computed, потом evidence, потом default
    htr = row.get("hours_to_resolution_at_signal")
    if htr is None:
        htr = ev.get("hours_to_resolve")
    hours_to_res = _safe_float(htr, 24.0)
    # Clip — extreme values (>30 days) приводят к outlier weights в LGB
    hours_to_res = min(max(hours_to_res, 0.0), 720.0)

    # Tier 3.1: recent_wr_30d — rolling per-detector WR (leak-free, computed в SQL)
    recent_wr = _safe_float(row.get("recent_wr_30d"), 0.5)
    # Sample-size clip — для редких детекторов recent_wr_30d может быть из 1-2 рядов
    recent_wr = min(max(recent_wr, 0.0), 1.0)

    # Tier 3.1: detector_recent_count_30d — log1p compression (variance reduction)
    raw_count = _safe_int(row.get("detector_recent_count_30d"), 0)
    recent_count_log = math.log1p(max(raw_count, 0))

    return {
        # raw signal features
        "raw_p": raw_p,
        "edge_pct": _safe_float(row.get("expected_edge_pct"), 0.0),
        "current_price": cur,
        "fair_price": fair,
        "entry_price": entry,
        "horizon_sec": float(_safe_int(row.get("horizon_sec"), 0)),
        "side_yes": 1.0 if side == "YES" else 0.0,
        "event_size": float(_safe_int(row.get("event_size"), 1) or 1),
        # market features
        "category_sports": 1.0 if cat == "sports" else 0.0,
        "category_politics": 1.0 if cat == "politics" else 0.0,
        "category_crypto": 1.0 if cat == "crypto" else 0.0,
        "category_world_economy": 1.0 if cat == "world & economy" else 0.0,
        "volume_24h": _safe_float(row.get("volume_24h"), 0.0),
        # evidence-derived features (опциональны — у D7b есть, у других может не быть)
        "ev_yes_price": _safe_float(ev.get("yes_price"), cur),
        "ev_baseline_wr": _safe_float(ev.get("baseline_wr"), 0.2),
        "ev_hours_to_resolve": _safe_float(ev.get("hours_to_resolve"), 24.0),
        "ev_lifetime_volume": _safe_float(ev.get("lifetime_volume_usd"), 0.0),
        "ev_smart_money_share": _safe_float(ev.get("smart_money_taker_share"), 0.0),
        # anchor band one-hot
        "anchor_4h": 1.0 if ev.get("anchor_band") == "4h" else 0.0,
        "anchor_24h": 1.0 if ev.get("anchor_band") == "24h" else 0.0,
        "anchor_72h": 1.0 if ev.get("anchor_band") == "72h" else 0.0,
        "anchor_168h": 1.0 if ev.get("anchor_band") == "168h" else 0.0,
        # zone (numerical: top of band)
        "zone_top": _parse_zone_top(ev.get("zone")),
        # Tier 3.1 features (2026-05-19)
        "hours_to_resolution": hours_to_res,
        "recent_wr_30d": recent_wr,
        "detector_recent_count_30d_log": recent_count_log,
    }


# Канонический порядок feature_names для stability.
# Используется в случаях, когда нужен detached от dict source порядок.
FEATURE_NAMES: tuple[str, ...] = tuple(
    sorted(
        build_features(
            {
                "side": "YES",
                "current_price": 0.5,
                "evidence": {},
            }
        ).keys()
    )
)
