"""G18 Calibration Edge — per-(detector × category × price_bin) lookup.

Применяется в `detector_signal_service._calibrate()` как FIRST tier перед LGB/Beta.

Logic:
    1. Compute (detector, category, price_bin) от signal
    2. Lookup в detector_calibration_bins
    3. Если n_observations >= MIN_OBS → return wr_observed (это и есть calibrated probability)
    4. Иначе → return None, fallback to next tier

Feature gates:
    - settings.calibration_bins_enabled — global rollout switch
    - WHITELIST_DETECTORS — per-detector enable list (rollout aware)
    - WHITELIST_CATEGORIES_EXCLUDE — categories где G18 ухудшила Brier (Minute Markets)

Refresh: bin lookup table обновляется weekly через calibration_bins_refit job.
Кэш в памяти service на TTL=5min (короткий т.к. таблица меняется редко).

См. docs/specs/G18_calibration_edge.md.
"""

from __future__ import annotations

import math
import time
from typing import Any

import structlog

from app.services.database import fetch

logger = structlog.get_logger(__name__)

# Lookup parameters
N_BINS = 10  # 0.0, 0.1, ..., 0.9
MIN_OBS_FOR_LOOKUP = 30

# Cache (per-process, TTL=5min)
_CACHE: dict[str, Any] = {"data": None, "loaded_at": 0.0}
_CACHE_TTL_SEC = 300

# Per-detector whitelist for production rollout.
# Phase 1: только детекторы с big-delta improvements в backtest.
# См. docs/backtests/G18_calibration_edge.md.
#
# To rollout to new detector: add to set + monitor cal_source counts.
# To disable: remove from set.
WHITELIST_DETECTORS: frozenset[str] = frozenset(
    {
        # Phase 1 — biggest backtest improvements (Δ Brier < -0.04)
        "D14_crypto_winners_distribute_no",  # Δ -0.0410 (raw 0.602 vs WR 0.816)
        # Phase 2 — RCA 2026-05-26 (research/detector-rca-2026-05-26/README.md):
        # Hardcoded cal_p=0.60 vs realized WR=0.405 → EV-sizer over-stake'ит ~50%
        # → 56% stop_loss rate. G18 даёт honest bin WR per (category × price),
        # стейк сожмётся, stop_loss survivability ↑.
        "D36_obi_pressure",
    }
)

# Categories где G18 не работает — skip lookup и fallback to next tier.
# Minute Markets из backtest: Δ Brier +0.003 (ухудшение).
WHITELIST_CATEGORIES_EXCLUDE: frozenset[str] = frozenset({"Minute Markets"})


def _price_bin(price: float) -> float:
    """Map 0..1 price to 0.0, 0.1, ..., 0.9 bin floor."""
    return float(math.floor(price * N_BINS) / N_BINS)


async def _load_bins() -> dict[tuple[str, str, float], dict[str, Any]]:
    """Load all bins from DB into in-memory dict. Cached with TTL."""
    now = time.time()
    if _CACHE["data"] is not None and (now - _CACHE["loaded_at"]) < _CACHE_TTL_SEC:
        return _CACHE["data"]

    rows = await fetch(
        "SELECT detector, category, price_bin::float8 AS price_bin, "
        "n_observations, n_wins, wr_observed::float8 AS wr_observed "
        "FROM detector_calibration_bins "
        "WHERE n_observations >= $1",
        MIN_OBS_FOR_LOOKUP,
    )
    out: dict[tuple[str, str, float], dict[str, Any]] = {}
    for r in rows:
        key = (r["detector"], r["category"], round(r["price_bin"], 1))
        out[key] = {
            "n": r["n_observations"],
            "wr": r["wr_observed"],
        }
    _CACHE["data"] = out
    _CACHE["loaded_at"] = now
    logger.info("g18_bins_loaded", n_bins=len(out))
    return out


async def bin_lookup(
    detector: str,
    category: str | None,
    current_price: float,
) -> tuple[float | None, str]:
    """G18 lookup: (detector, category, price_bin) → (wr_observed | None, source).

    Returns:
        (wr, "g18_bin")  — если bin найден и detector в whitelist
        (None, "g18_skip_detector")  — detector не в whitelist
        (None, "g18_skip_category")  — category в exclude list
        (None, "g18_no_data")  — bin не найден или n < MIN_OBS
    """
    if detector not in WHITELIST_DETECTORS:
        return None, "g18_skip_detector"

    cat = category or "unknown"
    if cat in WHITELIST_CATEGORIES_EXCLUDE:
        return None, "g18_skip_category"

    if not (0 < current_price < 1):
        return None, "g18_no_data"

    bins = await _load_bins()
    key = (detector, cat, _price_bin(current_price))
    entry = bins.get(key)
    if entry is None:
        return None, "g18_no_data"

    return float(entry["wr"]), "g18_bin"


def invalidate_cache() -> None:
    """Force reload at next lookup. Called after calibration_bins_refit."""
    _CACHE["data"] = None
    _CACHE["loaded_at"] = 0.0
