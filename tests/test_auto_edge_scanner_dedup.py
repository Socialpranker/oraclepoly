"""Auto Edge Scanner — dedup unit tests."""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

from scripts.research.edge_scanner.dedup import (
    _can_overlap_fast,
    _zones_overlap,
    classify_cell,
    compute_overlap,
)
from scripts.research.edge_scanner.gate import CellResult
from scripts.research.edge_scanner.grid_generator import Cell
from scripts.research.edge_scanner.territories import (
    ACTIVE_DETECTORS,
    matches_killed,
)


def _make_cell(**overrides) -> Cell:
    base = dict(
        feature="smart_money_taker_share",
        threshold_op=">",
        threshold_value=0.5,
        threshold_quantile_name="q50",
        zone="extreme",
        zone_lo=0.85,
        zone_hi=0.97,
        anchor=24,
        category=None,
        side="NO",
    )
    base.update(overrides)
    return Cell(**base)


def _make_result(cell: Cell) -> CellResult:
    return CellResult(
        cell=cell,
        n_signals=1000,
        n_unique_markets=500,
        win_rate=0.30,
        avg_cost=0.10,
        net_roi=0.15,
        mirror_net_roi=-0.20,
        mirror_diff_pp=35.0,
        year_2025_net_roi=0.15,
        year_2026_net_roi=0.10,
        year_consistency=0.67,
        daily_signals=1.0,
        daily_raw_pnl=10.0,
        daily_honest_pnl=5.0,
        p_value=0.001,
        bh_adjusted_p=0.001,
        verdict="PASS",
    )


def _make_synthetic_parquet(tmp_path) -> str:
    """Synthetic parquet with mix of zones/anchors/categories для overlap testing."""
    rng = np.random.default_rng(42)
    rows = []
    # Add 1000 rows in extreme zone, 24h, NO-side wins (D7b territory)
    for i in range(1000):
        rows.append({
            "yes_won": int(rng.random() > 0.85),  # NO wins ~85%
            "yes_price_at_anchor": 0.90,
            "smart_money_taker_share": 0.6,  # > 0.5 (matches our cell)
            "anchor_hours": 24,
            "category": "Politics",
            "condition_id": f"market_a_{i}",
            "resolve_year": 2025,
        })
    # Add 1000 rows in mid zone, 4h Crypto (D14 territory)
    for i in range(1000):
        rows.append({
            "yes_won": 0,
            "yes_price_at_anchor": 0.55,
            "smart_money_taker_share": 0.3,
            "anchor_hours": 4,
            "category": "Crypto",
            "condition_id": f"market_b_{i}",
            "resolve_year": 2025,
        })
    df = pd.DataFrame(rows)
    p = tmp_path / "synthetic.parquet"
    df.to_parquet(p, index=False)
    return str(p)


# ─── _zones_overlap ───────────────────────────────────────────────────────────


def test_zones_overlap_disjoint():
    assert _zones_overlap((0.10, 0.30), (0.50, 0.70)) is False


def test_zones_overlap_partial():
    assert _zones_overlap((0.10, 0.40), (0.30, 0.50)) is True


def test_zones_overlap_subset():
    assert _zones_overlap((0.10, 0.50), (0.20, 0.40)) is True


def test_zones_overlap_touching_boundary():
    """Boundary touch (0.30, 0.30) — exclusive end means no overlap."""
    assert _zones_overlap((0.10, 0.30), (0.30, 0.50)) is False


# ─── _can_overlap_fast ────────────────────────────────────────────────────────


def test_overlap_fast_zero_when_zone_disjoint():
    """cell zone [0.30, 0.50] vs D7b [0.85, 0.97] → False."""
    cell = _make_cell(zone="low", zone_lo=0.30, zone_hi=0.50)
    assert _can_overlap_fast(cell, ACTIVE_DETECTORS["D7b"]) is False


def test_overlap_fast_zero_when_anchor_disjoint():
    """cell anchor=720 vs D14 [4] → False."""
    cell = _make_cell(zone="mid", zone_lo=0.50, zone_hi=0.70, anchor=720)
    assert _can_overlap_fast(cell, ACTIVE_DETECTORS["D14"]) is False


def test_overlap_fast_zero_when_side_disjoint():
    """cell side=YES vs D7b sides=[NO] → False."""
    cell = _make_cell(side="YES")
    assert _can_overlap_fast(cell, ACTIVE_DETECTORS["D7b"]) is False


def test_overlap_fast_zero_when_event_level():
    """D3, D10b are event-level → never overlap with single-market cell."""
    cell = _make_cell()
    assert _can_overlap_fast(cell, ACTIVE_DETECTORS["D3"]) is False
    assert _can_overlap_fast(cell, ACTIVE_DETECTORS["D10b"]) is False


def test_overlap_fast_true_when_compatible():
    """cell extreme + 24h + NO + Politics → potentially in D7b territory."""
    cell = _make_cell(zone="extreme", zone_lo=0.85, zone_hi=0.97, anchor=24, side="NO", category="Politics")
    assert _can_overlap_fast(cell, ACTIVE_DETECTORS["D7b"]) is True


def test_overlap_fast_category_disjoint():
    """cell category=Politics vs D14 categories=[Crypto] → False."""
    cell = _make_cell(zone="mid", zone_lo=0.50, zone_hi=0.70, anchor=4, side="NO", category="Politics")
    assert _can_overlap_fast(cell, ACTIVE_DETECTORS["D14"]) is False


# ─── compute_overlap (with synthetic parquet) ────────────────────────────────


def test_overlap_high_for_d7b_subset(tmp_path):
    """Cell exactly in D7b territory → overlap should be ~1.0."""
    path = _make_synthetic_parquet(tmp_path)
    con = duckdb.connect()
    cell = _make_cell(zone="extreme", anchor=24, side="NO", category="Politics")
    overlap = compute_overlap(con, cell, ACTIVE_DETECTORS["D7b"], path)
    # All 1000 cell rows are in extreme + 24h + NO, all match D7b
    assert overlap > 0.95


def test_overlap_zero_for_disjoint_zone(tmp_path):
    """Cell in mid zone vs D7b extreme zone → overlap = 0."""
    path = _make_synthetic_parquet(tmp_path)
    con = duckdb.connect()
    cell = _make_cell(zone="mid", zone_lo=0.50, zone_hi=0.70, anchor=4, side="NO", category="Crypto")
    overlap = compute_overlap(con, cell, ACTIVE_DETECTORS["D7b"], path)
    assert overlap == 0.0


def test_overlap_event_level_detectors_zero(tmp_path):
    """Any single-market cell vs D3/D10b → overlap = 0."""
    path = _make_synthetic_parquet(tmp_path)
    con = duckdb.connect()
    cell = _make_cell(zone="extreme", anchor=24)
    assert compute_overlap(con, cell, ACTIVE_DETECTORS["D3"], path) == 0.0
    assert compute_overlap(con, cell, ACTIVE_DETECTORS["D10b"], path) == 0.0


# ─── classify_cell ────────────────────────────────────────────────────────────


def test_classify_subset_when_high_overlap(tmp_path):
    """Cell in D7b territory → status='subset'."""
    path = _make_synthetic_parquet(tmp_path)
    con = duckdb.connect()
    cell = _make_cell(zone="extreme", anchor=24, side="NO", category="Politics")
    result = _make_result(cell)
    status, overlaps = classify_cell(con, result, path)
    assert status == "subset"
    assert overlaps["D7b"] > 0.80


def test_classify_novel_when_low_overlap(tmp_path):
    """Cell in disjoint zone → status='novel'."""
    path = _make_synthetic_parquet(tmp_path)
    con = duckdb.connect()
    cell = _make_cell(zone="low", zone_lo=0.30, zone_hi=0.50, anchor=24, side="YES", category="Politics")
    result = _make_result(cell)
    status, overlaps = classify_cell(con, result, path)
    assert status == "novel"
    assert max(overlaps.values()) < 0.50


# ─── Killed dimensions ───────────────────────────────────────────────────────


def test_killed_pinnacle_short_circuits():
    """Cell with feature='pinnacle_edge_pct' → matches_killed returns True."""
    cell = _make_cell(feature="pinnacle_edge_pct")
    killed, reason = matches_killed(cell)
    assert killed is True
    assert reason and "D11" in reason


def test_killed_d14_old_politics_composite():
    """zone=mid + Politics + NO → matches D14-old kill."""
    cell = _make_cell(zone="mid", zone_lo=0.50, zone_hi=0.70, category="Politics", side="NO")
    killed, reason = matches_killed(cell)
    assert killed is True
    assert reason and "D14-old" in reason


def test_killed_sports_subsumed_by_d7b():
    """Sports + NO → matches D15 kill (subsumed by D7b)."""
    cell = _make_cell(category="Sports", side="NO")
    killed, reason = matches_killed(cell)
    assert killed is True
    assert reason and "D15" in reason


def test_killed_does_not_match_safe_cell():
    """Politics + extreme + NO (D7b territory but not killed) → not killed."""
    cell = _make_cell(zone="extreme", category="Politics", side="NO")
    killed, _ = matches_killed(cell)
    assert killed is False


def test_killed_clusters_disabled():
    """cluster_spillover_score → killed (D8)."""
    cell = _make_cell(feature="cluster_spillover_score")
    killed, _ = matches_killed(cell)
    assert killed is True
