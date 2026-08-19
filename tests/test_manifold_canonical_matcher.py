"""Manifold ↔ canonical_events matcher unit tests."""

from __future__ import annotations

import pytest

from app.matchers.manifold_canonical_matcher import (
    CanonicalEventInfo,
    ManifoldMarketInfo,
    cosine_similarity,
    infer_manifold_category,
    match_manifold_to_canonical,
)


def test_cosine_identical_vectors():
    assert cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector():
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_length_mismatch_raises():
    with pytest.raises(ValueError):
        cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])


def test_infer_category_politics():
    assert infer_manifold_category("Will Trump be re-elected?") == "politics"
    assert infer_manifold_category("Senate confirmation vote") == "politics"


def test_infer_category_crypto():
    assert infer_manifold_category("Will BTC reach 200k?") == "crypto"
    assert infer_manifold_category("ETH price end of 2026") == "crypto"


def test_infer_category_economics():
    assert infer_manifold_category("Fed cuts rates in March?") == "economics"
    assert infer_manifold_category("US CPI YoY for April") == "economics"


def test_infer_category_sports():
    assert infer_manifold_category("Super Bowl LX winner") == "sports"
    assert infer_manifold_category("World Cup 2026 final") == "sports"


def test_infer_category_default_macro():
    """Unknown topic falls to macro."""
    assert infer_manifold_category("Will aliens land in 2026?") == "macro"


def test_infer_category_uses_group_slugs():
    """Group slugs are matched too."""
    assert infer_manifold_category("Some neutral question", group_slugs=["us-politics-2026"]) == "politics"


def test_match_empty_inputs():
    result = match_manifold_to_canonical([], [], [], [])
    assert result == []


def test_match_basic_high_similarity():
    """Identical embeddings → similarity 1.0 → auto-matched."""
    m = ManifoldMarketInfo(id="m1", slug="ms", question="Will Trump win?", inferred_category="politics", probability=0.55)
    ce = CanonicalEventInfo(id="evt-uuid-1", slug="trump-2024", title="Will Trump win?", category="politics")
    embed = [1.0, 0.0, 0.0]
    result = match_manifold_to_canonical([m], [ce], [embed], [embed])
    assert len(result) == 1
    assert result[0].manifold_market_id == "m1"
    assert result[0].canonical_event_id == "evt-uuid-1"
    assert result[0].similarity_score == pytest.approx(1.0)
    assert result[0].auto_matched is True


def test_match_filters_below_threshold():
    """Orthogonal embeddings → similarity 0 → not returned."""
    m = ManifoldMarketInfo(id="m1", slug="ms", question="Q", inferred_category="politics", probability=0.5)
    ce = CanonicalEventInfo(id="ce1", slug="ces", title="T", category="politics")
    result = match_manifold_to_canonical([m], [ce], [[1.0, 0.0]], [[0.0, 1.0]])
    assert result == []


def test_match_cohort_isolation():
    """Manifold politics не matched к canonical sports."""
    m = ManifoldMarketInfo(id="m1", slug="ms", question="Q", inferred_category="politics", probability=0.5)
    ce = CanonicalEventInfo(id="ce1", slug="ces", title="T", category="sports")
    embed = [1.0, 0.0]
    result = match_manifold_to_canonical([m], [ce], [embed], [embed])
    assert result == []


def test_match_auto_vs_flag_thresholds():
    """Similarity между FLAG (0.70) и AUTO (0.85) → auto_matched=False."""
    m = ManifoldMarketInfo(id="m1", slug="ms", question="Q", inferred_category="politics", probability=0.5)
    ce = CanonicalEventInfo(id="ce1", slug="ces", title="T", category="politics")
    # Vectors that yield ~0.75 cosine: take from cos table
    e1 = [1.0, 0.0]
    e2 = [0.75, (1 - 0.75**2) ** 0.5]  # cos(e1, e2) = 0.75
    result = match_manifold_to_canonical([m], [ce], [e1], [e2])
    assert len(result) == 1
    assert result[0].similarity_score == pytest.approx(0.75, abs=0.01)
    assert result[0].auto_matched is False


def test_match_top_k_per_manifold():
    """Manifold m1 matched к top-K canonical events (default K=2)."""
    m = ManifoldMarketInfo(id="m1", slug="ms", question="Trump?", inferred_category="politics", probability=0.5)
    canonical = [
        CanonicalEventInfo(id=f"ce{i}", slug=f"s{i}", title=f"T{i}", category="politics")
        for i in range(5)
    ]
    # Embedings designed so similarities декрементируются
    m_embed = [1.0, 0.0]
    ce_embeddings = [
        [1.0, 0.0],   # sim 1.0
        [0.95, 0.31], # ~0.95
        [0.80, 0.60], # ~0.80
        [0.60, 0.80], # ~0.60 (below flag)
        [0.50, 0.87], # ~0.50 (below flag)
    ]
    result = match_manifold_to_canonical([m], canonical, [m_embed], ce_embeddings, top_k=2)
    assert len(result) == 2  # only top 2 above flag
    # Sorted by similarity desc
    assert result[0].similarity_score > result[1].similarity_score


def test_match_embedding_length_mismatch_raises_manifold():
    m = ManifoldMarketInfo(id="m1", slug="ms", question="Q", inferred_category="politics", probability=0.5)
    ce = CanonicalEventInfo(id="ce1", slug="ces", title="T", category="politics")
    with pytest.raises(ValueError):
        match_manifold_to_canonical([m, m], [ce], [[1.0]], [[1.0]])


def test_match_embedding_length_mismatch_raises_canonical():
    m = ManifoldMarketInfo(id="m1", slug="ms", question="Q", inferred_category="politics", probability=0.5)
    ce = CanonicalEventInfo(id="ce1", slug="ces", title="T", category="politics")
    with pytest.raises(ValueError):
        match_manifold_to_canonical([m], [ce, ce], [[1.0]], [[1.0]])


def test_match_evidence_includes_metadata():
    m = ManifoldMarketInfo(id="m1", slug="will-trump-win", question="Will Trump win?", inferred_category="politics", probability=0.55)
    ce = CanonicalEventInfo(id="ce1", slug="trump-2026", title="Trump impeachment", category="politics")
    embed = [1.0, 0.0]
    result = match_manifold_to_canonical([m], [ce], [embed], [embed])
    assert len(result) == 1
    ev = result[0].evidence
    assert ev["manifold_slug"] == "will-trump-win"
    assert ev["canonical_slug"] == "trump-2026"
    assert ev["category"] == "politics"
    assert ev["manifold_probability"] == 0.55
