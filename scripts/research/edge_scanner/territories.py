"""Active detector territories + killed dimensions.

Hardcoded knowledge base for dedup. Updated when:
  - new detector promoted to virtual/live (add to ACTIVE_DETECTORS)
  - dimension killed via canonical FAIL (add to KILLED_DIMENSIONS)

Source of truth for territories: app/detectors/{d3,d5,d7b,d10b,d13,d14}_*.py
Source of truth for killed dims: ~/.claude/.../memory/MEMORY.md
"""

from __future__ import annotations

from typing import Any

# Each detector territory. Used by dedup.compute_overlap().
#
# Schema:
#   zones: list of (zone_name, lo, hi) tuples — single-market zone bounds
#   anchors: list of int — accepted hours_to_resolve buckets
#   categories: list[str] | None — None means ALL
#   sides: list[str] — accepted sides
#   extra_filters: list of (feature, op, value) — additional gates
#   event_level: bool — multi-market detectors never overlap with single-market cells
#   long_horizon_only: bool — D5-style horizons (>= 168h)

ACTIVE_DETECTORS: dict[str, dict[str, Any]] = {
    "D7b": {
        "zones": [("extreme", 0.85, 0.97)],
        "anchors": [4, 24, 72],
        "categories": None,
        "sides": ["NO"],
    },
    "D13": {
        "zones": [("extreme", 0.85, 0.95)],
        "anchors": [4, 24, 72],
        "categories": None,
        "sides": ["NO"],
        "extra_filters": [("smart_money_taker_share", "<", 0.05)],
    },
    "D14": {
        "zones": [("mid", 0.50, 0.70)],
        "anchors": [4],
        "categories": ["Crypto"],
        "sides": ["NO"],
        "extra_filters": [
            ("taker_pnl_weighted_usd_24h", ">", 0.0),
            ("taker_roi_weighted_24h", ">", 10.0),
            ("buy_imbalance_asof", "<", 0.45),
        ],
    },
    "D5": {
        "zones": [("extreme", 0.85, 0.97), ("longshot", 0.03, 0.15)],
        "anchors": [168, 720],
        "categories": None,
        "sides": ["NO", "YES"],
        "long_horizon_only": True,
    },
    # Event-level detectors — never overlap single-market cells
    "D3": {"event_level": True, "kind": "negrisk_arb"},
    "D10b": {"event_level": True, "kind": "multi_leg_arb"},
}


# Killed dimensions — auto-skip cells matching these signatures.
#
# Three modes:
#   feature: str — match if cell.feature == this
#   composite: dict — match if all keys match (zone, category, side)
#
# Reviving a killed dim requires explicit PR (sticky kill).

KILLED_DIMENSIONS: list[dict[str, Any]] = [
    {"feature": "cluster_spillover_score", "note": "D8 ClusterSpillover killed 2026-05-02"},
    {"feature": "is_offhours", "note": "D9 OffHoursThinBookFade killed 2026-05-02"},
    {"feature": "pinnacle_edge_pct", "note": "D11 PinnacleConsistency — external dep"},
    {"feature": "is_weekend", "note": "D12 WeekendLiquidityGap killed 2026-05-02"},
    # D14-old PoliticsLadderHonest killed (subsumed by D7b extended)
    {
        "composite": {"zone": "mid", "category": "Politics", "side": "NO"},
        "note": "D14-old PoliticsLadder killed 2026-05-02 (best variant subset of D7b extended)",
    },
    # D15 SportsUnderdogNO subsumed by D7b extended fight excl
    {
        "composite": {"category": "Sports", "side": "NO"},
        "note": "D15 SportsUnderdog subsumed by D7b extended (fight keywords excluded 2026-05-02)",
    },
    {"feature": "calibrator_top_decile", "note": "D-MLCalibratorTopDecile killed 2026-05-02"},
]


def matches_killed(cell) -> tuple[bool, str | None]:
    """Return (True, reason) if cell matches a killed dimension, else (False, None).

    cell is a dataclass-like with attrs: feature, zone, category, side.
    """
    for k in KILLED_DIMENSIONS:
        if "feature" in k and cell.feature == k["feature"]:
            return True, k["note"]
        if "composite" in k:
            comp = k["composite"]
            zone_match = ("zone" not in comp) or (comp["zone"] == cell.zone)
            cat_match = ("category" not in comp) or (comp["category"] == cell.category)
            side_match = ("side" not in comp) or (comp["side"] == cell.side)
            if zone_match and cat_match and side_match:
                return True, k["note"]
    return False, None
