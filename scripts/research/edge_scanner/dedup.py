"""Stage 3: Dedup — territory overlap calculation vs active detectors.

Two-layer:
  1. Fast pre-filter (zone × anchor × category × side disjoint → overlap=0)
  2. Empirical signal-set overlap via DuckDB JOIN on condition_id.

Returns directional overlap |cell ∩ det| / |cell|, NOT Jaccard. We want to
know "is this cell a subset of detector". A cell can be strict subset even
when det is much larger.
"""

from __future__ import annotations

import logging
from typing import Any

import duckdb

from .config import OVERLAP_NOVEL_THRESHOLD, OVERLAP_SUBSET_THRESHOLD
from .gate import CellResult
from .grid_generator import Cell
from .territories import ACTIVE_DETECTORS

logger = logging.getLogger(__name__)


def _zones_overlap(zone1: tuple[float, float], zone2: tuple[float, float]) -> bool:
    """Return True if two price intervals overlap."""
    a_lo, a_hi = zone1
    b_lo, b_hi = zone2
    return a_lo < b_hi and b_lo < a_hi


def _can_overlap_fast(cell: Cell, det_spec: dict[str, Any]) -> bool:
    """Fast pre-filter — return False (no overlap possible) if any dimension disjoint.

    Returns True only if all dimensions are at least partially compatible.
    """
    if det_spec.get("event_level"):
        # Event-level detectors (D3, D10b) — single-market cells never overlap by design.
        return False

    # Zone overlap
    cell_zone = (cell.zone_lo, cell.zone_hi)
    det_zones = det_spec.get("zones") or []
    if not any(_zones_overlap(cell_zone, (zlo, zhi)) for _, zlo, zhi in det_zones):
        return False

    # Anchor overlap
    if cell.anchor not in (det_spec.get("anchors") or []):
        return False

    # Side overlap
    if cell.side not in (det_spec.get("sides") or []):
        return False

    # Category overlap
    det_cats = det_spec.get("categories")
    if det_cats is not None:
        # Detector restricted to specific categories
        if cell.category is not None and cell.category not in det_cats:
            return False
        # If cell is ALL but detector is specific, could partially overlap → keep True

    return True


def _build_detector_where(det_spec: dict[str, Any]) -> str:
    """Build SQL WHERE fragment for detector territory."""
    parts = []
    # Zone (use union of all zones — for D5 with multi-zone)
    zone_clauses = []
    for _, zlo, zhi in det_spec.get("zones") or []:
        zone_clauses.append(f"(yes_price_at_anchor >= {zlo} AND yes_price_at_anchor < {zhi})")
    if zone_clauses:
        parts.append("(" + " OR ".join(zone_clauses) + ")")

    # Anchors
    anchors = det_spec.get("anchors") or []
    if anchors:
        anchor_str = ",".join(str(a) for a in anchors)
        parts.append(f"anchor_hours IN ({anchor_str})")

    # Categories
    det_cats = det_spec.get("categories")
    if det_cats is not None:
        safe_cats = [c.replace("'", "''") for c in det_cats]
        cat_str = ",".join(f"'{c}'" for c in safe_cats)
        parts.append(f"category IN ({cat_str})")

    # Extra filters
    for f, op, v in det_spec.get("extra_filters") or []:
        parts.append(f"{f} {op} {v}")

    return " AND ".join(parts) if parts else "TRUE"


def compute_overlap(
    con: duckdb.DuckDBPyConnection,
    cell: Cell,
    det_spec: dict[str, Any],
    parquet_path: str,
) -> float:
    """Compute |cell ∩ detector| / |cell| via DuckDB JOIN on condition_id.

    Returns directional overlap in [0, 1]. 0 means no overlap, 1 means cell ⊂ detector.
    """
    if not _can_overlap_fast(cell, det_spec):
        return 0.0

    # Cell WHERE
    from .gate import _build_where_clause as build_cell_where  # avoid circular

    cell_where = build_cell_where(cell)
    det_where = _build_detector_where(det_spec)

    sql = f"""
        WITH cell_rows AS (
            SELECT DISTINCT condition_id FROM read_parquet('{parquet_path}')
            WHERE anchor_hours = {cell.anchor}
              AND yes_won IS NOT NULL
              AND yes_price_at_anchor >= {cell.zone_lo}
              AND yes_price_at_anchor < {cell.zone_hi}
              AND {cell_where}
        ),
        det_rows AS (
            SELECT DISTINCT condition_id FROM read_parquet('{parquet_path}')
            WHERE yes_won IS NOT NULL
              AND ({det_where})
        )
        SELECT
            (SELECT COUNT(*) FROM cell_rows) AS cell_n,
            (SELECT COUNT(*) FROM cell_rows c JOIN det_rows d USING (condition_id)) AS overlap_n
    """
    try:
        row = con.execute(sql).fetchone()
    except Exception as exc:
        logger.warning("dedup_query_failed", extra={"detector": det_spec, "error": str(exc)})
        return 0.0

    if row is None or row[0] is None or row[0] == 0:
        return 0.0
    return float(row[1] or 0) / float(row[0])


def classify_cell(
    con: duckdb.DuckDBPyConnection,
    result: CellResult,
    parquet_path: str,
) -> tuple[str, dict[str, float]]:
    """Compute overlap vs each active detector. Return (status, overlaps_dict).

    status:
      - 'novel'           if overlap_max < OVERLAP_NOVEL_THRESHOLD
      - 'subset'          if overlap_max > OVERLAP_SUBSET_THRESHOLD
      - 'partial_overlap' if 0.50 <= overlap_max <= 0.80
    """
    overlaps: dict[str, float] = {}
    for det_name, spec in ACTIVE_DETECTORS.items():
        overlaps[det_name] = compute_overlap(con, result.cell, spec, parquet_path)

    overlap_max = max(overlaps.values()) if overlaps else 0.0

    if overlap_max > OVERLAP_SUBSET_THRESHOLD:
        status = "subset"
    elif overlap_max < OVERLAP_NOVEL_THRESHOLD:
        status = "novel"
    else:
        status = "partial_overlap"

    return status, overlaps
