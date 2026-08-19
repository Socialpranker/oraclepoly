"""Auto Edge Scanner — grid_generator unit tests."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from scripts.research.edge_scanner import grid_generator
from scripts.research.edge_scanner.config import (
    ANCHORS,
    QUANTILES,
    THRESHOLD_OPS,
    ZONES,
)
from scripts.research.edge_scanner.grid_generator import (
    Cell,
    compute_quantiles,
    generate_cells,
)

# ─── Cell.signature ──────────────────────────────────────────────────────────


def test_signature_stable_across_value_drift():
    """Same quantile NAME → same signature even if value drifts slightly."""
    a = Cell(
        feature="smart_money_taker_share",
        threshold_op=">",
        threshold_value=0.05,
        threshold_quantile_name="q50",
        zone="extreme",
        zone_lo=0.85,
        zone_hi=0.97,
        anchor=24,
        category="Politics",
        side="NO",
    )
    b = Cell(
        feature="smart_money_taker_share",
        threshold_op=">",
        threshold_value=0.052,  # value drifted slightly
        threshold_quantile_name="q50",
        zone="extreme",
        zone_lo=0.85,
        zone_hi=0.97,
        anchor=24,
        category="Politics",
        side="NO",
    )
    assert a.signature() == b.signature()


def test_signature_differs_with_op():
    a = Cell("f", ">", 0.5, "q50", "mid", 0.5, 0.7, 4, "Crypto", "NO")
    b = Cell("f", "<", 0.5, "q50", "mid", 0.5, 0.7, 4, "Crypto", "NO")
    assert a.signature() != b.signature()


def test_signature_differs_with_zone():
    a = Cell("f", ">", 0.5, "q50", "mid", 0.5, 0.7, 4, "Crypto", "NO")
    b = Cell("f", ">", 0.5, "q50", "high", 0.7, 0.85, 4, "Crypto", "NO")
    assert a.signature() != b.signature()


def test_signature_treats_none_category_as_all():
    """ALL category should hash same as ALL category."""
    a = Cell("f", ">", 0.5, "q50", "mid", 0.5, 0.7, 4, None, "NO")
    b = Cell("f", ">", 0.5, "q50", "mid", 0.5, 0.7, 4, None, "NO")
    assert a.signature() == b.signature()


# ─── compute_quantiles ────────────────────────────────────────────────────────


def _make_synthetic_parquet(tmp_path, df: pd.DataFrame) -> str:
    """Helper: write df to parquet and return path."""
    p = tmp_path / "synthetic.parquet"
    df.to_parquet(p, index=False)
    return str(p)


def test_quantiles_computed_correctly_on_uniform(tmp_path):
    """Feature drawn from [0, 100] uniform → q50 ≈ 50."""
    n = 5000
    df = pd.DataFrame(
        {
            "yes_won": [1] * n,
            "yes_price_at_anchor": [0.5] * n,
            "smart_money_taker_share": list(range(n)),  # 0..n-1
            "anchor_hours": [4] * n,
            "category": ["Crypto"] * n,
        }
    )
    path = _make_synthetic_parquet(tmp_path, df)
    qs = compute_quantiles(path, features=["smart_money_taker_share"], sample_n=n)
    assert "smart_money_taker_share" in qs
    q = qs["smart_money_taker_share"]
    # n=5000, so q50 ≈ 2500
    assert abs(q["q50"] - 2500) < 200


def test_quantiles_skips_degenerate_feature(tmp_path):
    """Feature where 95% identical → skipped."""
    n = 5000
    vals = [0.0] * 4800 + list(range(200))  # 96% zeros
    df = pd.DataFrame(
        {
            "yes_won": [1] * n,
            "yes_price_at_anchor": [0.5] * n,
            "is_offhours": vals,
            "anchor_hours": [4] * n,
            "category": ["Crypto"] * n,
        }
    )
    path = _make_synthetic_parquet(tmp_path, df)
    qs = compute_quantiles(path, features=["is_offhours"], sample_n=n)
    assert "is_offhours" not in qs


def test_quantiles_skips_missing_feature(tmp_path):
    """Feature not in parquet → skipped silently."""
    n = 1500
    df = pd.DataFrame(
        {
            "yes_won": [1] * n,
            "yes_price_at_anchor": [0.5] * n,
            "anchor_hours": [4] * n,
            "category": ["Crypto"] * n,
        }
    )
    path = _make_synthetic_parquet(tmp_path, df)
    qs = compute_quantiles(path, features=["nonexistent_feature"], sample_n=n)
    assert "nonexistent_feature" not in qs


def test_quantiles_skips_below_min_samples(tmp_path):
    """<1000 non-null values → skipped."""
    n = 500
    df = pd.DataFrame(
        {
            "yes_won": [1] * n,
            "yes_price_at_anchor": [0.5] * n,
            "smart_money_taker_share": list(range(n)),
            "anchor_hours": [4] * n,
            "category": ["Crypto"] * n,
        }
    )
    path = _make_synthetic_parquet(tmp_path, df)
    qs = compute_quantiles(path, features=["smart_money_taker_share"], sample_n=n)
    assert "smart_money_taker_share" not in qs


# ─── generate_cells ──────────────────────────────────────────────────────────


def test_generate_cells_explodes_to_expected_count(tmp_path):
    """With 1 feature, 5 quantiles, 2 ops, 5 zones, 3 anchors, 7 cats, 2 sides:
    1 × 5 × 2 × 5 × 3 × 7 × 2 = 2100 cells (minus killed dims)."""
    n = 2000
    df = pd.DataFrame(
        {
            "yes_won": [1] * n,
            "yes_price_at_anchor": [0.5] * n,
            "smart_money_taker_share": [i / n for i in range(n)],
            "anchor_hours": [4] * n,
            "category": ["Crypto"] * n,
        }
    )
    path = _make_synthetic_parquet(tmp_path, df)

    with patch.object(grid_generator, "NUMERIC_FEATURES", ["smart_money_taker_share"]):
        cells, stats = generate_cells(path)

    expected_max = 1 * len(QUANTILES) * len(THRESHOLD_OPS) * len(ZONES) * len(ANCHORS) * 7 * 2
    assert stats["total_enumerated"] == expected_max
    # Some cells eliminated by killed dim filter (D14-old Politics composite, etc)
    assert stats["n_cells_to_evaluate"] < expected_max
    assert stats["n_killed_filtered"] > 0


def test_generate_cells_respects_killed_dimensions(tmp_path):
    """Cell with feature='pinnacle_edge_pct' should be auto-killed."""
    n = 2000
    df = pd.DataFrame(
        {
            "yes_won": [1] * n,
            "yes_price_at_anchor": [0.5] * n,
            "pinnacle_edge_pct": [i / n for i in range(n)],
            "anchor_hours": [4] * n,
            "category": ["Crypto"] * n,
        }
    )
    path = _make_synthetic_parquet(tmp_path, df)

    with patch.object(grid_generator, "NUMERIC_FEATURES", ["pinnacle_edge_pct"]):
        cells, _stats = generate_cells(path)

    # Killed feature → ALL cells filtered out
    assert all(c.feature != "pinnacle_edge_pct" for c in cells)


def test_generate_cells_returns_dataclass_cells(tmp_path):
    n = 2000
    df = pd.DataFrame(
        {
            "yes_won": [1] * n,
            "yes_price_at_anchor": [0.5] * n,
            "smart_money_taker_share": [i / n for i in range(n)],
            "anchor_hours": [4] * n,
            "category": ["Crypto"] * n,
        }
    )
    path = _make_synthetic_parquet(tmp_path, df)
    with patch.object(grid_generator, "NUMERIC_FEATURES", ["smart_money_taker_share"]):
        cells, _ = generate_cells(path)
    assert len(cells) > 0
    c = cells[0]
    assert isinstance(c, Cell)
    assert c.feature == "smart_money_taker_share"
    assert c.threshold_op in (">", "<")
