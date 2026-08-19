"""Stage 1: Grid generator — enumerate (feature × threshold × zone × anchor × category × side) cells.

Computes per-feature quantile thresholds once on a sample of canonical data,
then iterates the cartesian product. Skips:
  - features with degenerate distribution (>90% identical values)
  - cells matching KILLED_DIMENSIONS (territories.py)
  - cells matching obvious detector subsets (zone+anchor+category+side strict equality)
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import duckdb

from .config import (
    ANCHORS,
    CATEGORIES,
    NUMERIC_FEATURES,
    QUANTILES,
    SIDES,
    THRESHOLD_OPS,
    ZONES,
)
from .territories import ACTIVE_DETECTORS, matches_killed

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Cell:
    """One hypothesis cell."""

    feature: str
    threshold_op: str  # '>' | '<'
    threshold_value: float
    threshold_quantile_name: str  # 'q10' | 'q25' | etc — for traceability
    zone: str
    zone_lo: float
    zone_hi: float
    anchor: int
    category: str | None  # None = ALL
    side: str  # 'YES' | 'NO'

    def signature(self) -> str:
        """Stable sha1 hash for cross-week dedup. Quantile NAME used (not value)
        so that small data shifts don't break dedup of "same" hypothesis."""
        payload = {
            "feature": self.feature,
            "op": self.threshold_op,
            "q": self.threshold_quantile_name,
            "zone": self.zone,
            "anchor": self.anchor,
            "category": self.category or "ALL",
            "side": self.side,
        }
        s = json.dumps(payload, sort_keys=True)
        return hashlib.sha1(s.encode()).hexdigest()


def compute_quantiles(
    parquet_path: str,
    features: list[str] = NUMERIC_FEATURES,
    sample_n: int = 200_000,
    degenerate_threshold: float = 0.90,
) -> dict[str, dict[str, float]]:
    """One-pass per-feature quantile estimation.

    Skips features where:
      - >degenerate_threshold of values are identical (e.g. boolean flags)
      - <1000 non-null values
      - all quantiles equal (zero variance)

    Returns: {feature_name: {q10: ..., q25: ..., q50: ..., q75: ..., q90: ...}}
    """
    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=4")

    out: dict[str, dict[str, float]] = {}

    available_cols = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{parquet_path}') LIMIT 1"
    ).fetchdf()
    available_set = set(available_cols["column_name"].tolist())

    sample_sql = f"""
        SELECT * FROM read_parquet('{parquet_path}')
        WHERE yes_won IS NOT NULL
        USING SAMPLE {sample_n}
    """
    df = con.execute(sample_sql).fetchdf()

    for feat in features:
        if feat not in available_set:
            logger.warning("grid_generator_feature_missing", extra={"feature": feat})
            continue
        s = df[feat].dropna()
        if len(s) < 1000:
            continue
        # degenerate: most-common value occupies > threshold
        try:
            top_freq = s.value_counts(dropna=False).iloc[0] / len(s)
            if top_freq > degenerate_threshold:
                continue
        except Exception:
            continue

        qs = {}
        for q in QUANTILES:
            try:
                qs[f"q{int(q * 100)}"] = float(s.quantile(q))
            except Exception:
                qs = {}
                break
        if not qs:
            continue
        # zero variance check
        if len(set(qs.values())) <= 1:
            continue
        out[feat] = qs

    return out


def _is_obvious_subset(cell: Cell) -> bool:
    """Skip cells that exactly match an active detector's (zone × anchor × category × side).

    NOTE: this is the *fast* dedup. Real overlap calc happens in dedup.py
    which queries actual signal universes. Here we just filter cells where
    all zone/anchor/category/side dimensions identically match a detector.
    """
    for det_name, spec in ACTIVE_DETECTORS.items():
        if spec.get("event_level"):
            continue
        # zone match
        zones = spec.get("zones") or []
        zone_match = any(zname == cell.zone for zname, _, _ in zones)
        if not zone_match:
            continue
        # anchor match
        if cell.anchor not in (spec.get("anchors") or []):
            continue
        # side match
        if cell.side not in (spec.get("sides") or []):
            continue
        # category match
        cats = spec.get("categories")
        if cats is not None and cell.category is not None and cell.category not in cats:
            continue
        # All match → potential subset. We DON'T skip — let dedup.py compute
        # actual overlap. Returning False here means cells go through full
        # gate evaluation, then dedup classifies as 'subset' if overlap > 80%.
        # Keeping cells gives us "partial_overlap" findings (50-80%) which
        # are interesting enrichments.
        return False
    return False


def generate_cells(
    parquet_path: str,
) -> tuple[list[Cell], dict[str, Any]]:
    """Generate all candidate cells. Returns (cells, stats)."""
    quantiles = compute_quantiles(parquet_path)
    logger.info(
        "grid_generator_quantiles_computed",
        extra={"n_features": len(quantiles)},
    )

    cells: list[Cell] = []
    n_killed = 0
    n_total = 0

    for feat, qs in quantiles.items():
        for q_name, q_val in qs.items():
            for op in THRESHOLD_OPS:
                for zone_name, zlo, zhi in ZONES:
                    for anchor in ANCHORS:
                        for cat in CATEGORIES:
                            for side in SIDES:
                                n_total += 1
                                cell = Cell(
                                    feature=feat,
                                    threshold_op=op,
                                    threshold_value=q_val,
                                    threshold_quantile_name=q_name,
                                    zone=zone_name,
                                    zone_lo=zlo,
                                    zone_hi=zhi,
                                    anchor=anchor,
                                    category=cat,
                                    side=side,
                                )
                                killed, _reason = matches_killed(cell)
                                if killed:
                                    n_killed += 1
                                    continue
                                cells.append(cell)

    stats = {
        "total_enumerated": n_total,
        "n_features_kept": len(quantiles),
        "n_killed_filtered": n_killed,
        "n_cells_to_evaluate": len(cells),
    }
    logger.info("grid_generator_cells_generated", extra=stats)
    return cells, stats
