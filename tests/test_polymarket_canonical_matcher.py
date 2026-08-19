"""Polymarket markets ↔ canonical_events matcher unit tests."""

from __future__ import annotations

import pytest

from app.matchers.polymarket_canonical_matcher import (
    CanonicalEventInfo,
    PolymarketMarketInfo,
    cosine_similarity,
    map_polymarket_category,
    match_polymarket_to_canonical,
)

# --- cosine_similarity is reused from the Manifold matcher (no duplication) ---


def test_cosine_is_reused_from_manifold():
    """The matcher re-exports cosine_similarity from the Manifold module."""
    from app.matchers import manifold_canonical_matcher as mcm

    assert cosine_similarity is mcm.cosine_similarity


def test_cosine_identical_vectors():
    assert cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector():
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_length_mismatch_raises():
    with pytest.raises(ValueError):
        cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])


# --- map_polymarket_category ---


def test_map_category_sports():
    assert map_polymarket_category("Sports") == "sports"


def test_map_category_politics():
    assert map_polymarket_category("Politics") == "politics"


def test_map_category_crypto():
    assert map_polymarket_category("Crypto") == "crypto"


def test_map_category_world_economy_to_economics():
    assert map_polymarket_category("World & Economy") == "economics"
    assert map_polymarket_category("World and Economy") == "economics"


def test_map_category_tech_ai_to_macro():
    assert map_polymarket_category("Tech & AI") == "macro"


def test_map_category_culture_to_macro():
    assert map_polymarket_category("Culture") == "macro"


def test_map_category_case_insensitive():
    assert map_polymarket_category("  cRyPtO  ") == "crypto"


def test_map_category_unknown_falls_to_macro():
    assert map_polymarket_category("Esports") == "macro"


def test_map_category_empty_falls_to_macro():
    assert map_polymarket_category("") == "macro"
    assert map_polymarket_category(None) == "macro"  # type: ignore[arg-type]


# --- match_polymarket_to_canonical ---


def test_match_empty_inputs():
    result = match_polymarket_to_canonical([], [], [], [])
    assert result == []


def test_match_basic_high_similarity():
    """Identical embeddings → similarity 1.0 → auto-matched."""
    pm = PolymarketMarketInfo(
        id="pm-uuid-1",
        condition_id="0xabc",
        question="Will Trump win?",
        inferred_category="politics",
        current_price=0.55,
    )
    ce = CanonicalEventInfo(id="evt-uuid-1", slug="trump-2024", title="Will Trump win?", category="politics")
    embed = [1.0, 0.0, 0.0]
    result = match_polymarket_to_canonical([pm], [ce], [embed], [embed])
    assert len(result) == 1
    assert result[0].polymarket_market_id == "pm-uuid-1"
    assert result[0].canonical_event_id == "evt-uuid-1"
    assert result[0].similarity_score == pytest.approx(1.0)
    assert result[0].auto_matched is True


def test_match_filters_below_threshold():
    """Orthogonal embeddings → similarity 0 → not returned."""
    pm = PolymarketMarketInfo(
        id="pm1", condition_id="0x1", question="Q", inferred_category="politics", current_price=0.5
    )
    ce = CanonicalEventInfo(id="ce1", slug="ces", title="T", category="politics")
    result = match_polymarket_to_canonical([pm], [ce], [[1.0, 0.0]], [[0.0, 1.0]])
    assert result == []


def test_match_cohort_isolation():
    """Polymarket politics не matched к canonical sports."""
    pm = PolymarketMarketInfo(
        id="pm1", condition_id="0x1", question="Q", inferred_category="politics", current_price=0.5
    )
    ce = CanonicalEventInfo(id="ce1", slug="ces", title="T", category="sports")
    embed = [1.0, 0.0]
    result = match_polymarket_to_canonical([pm], [ce], [embed], [embed])
    assert result == []


def test_match_auto_vs_flag_thresholds():
    """Similarity между FLAG (0.65) и AUTO (0.80) → auto_matched=False."""
    pm = PolymarketMarketInfo(
        id="pm1", condition_id="0x1", question="Q", inferred_category="politics", current_price=0.5
    )
    ce = CanonicalEventInfo(id="ce1", slug="ces", title="T", category="politics")
    # Vectors that yield ~0.75 cosine: between flag (0.65) and auto (0.80)
    e1 = [1.0, 0.0]
    e2 = [0.75, (1 - 0.75**2) ** 0.5]  # cos(e1, e2) = 0.75
    result = match_polymarket_to_canonical([pm], [ce], [e1], [e2])
    assert len(result) == 1
    assert result[0].similarity_score == pytest.approx(0.75, abs=0.01)
    assert result[0].auto_matched is False


def test_match_top_k_per_market():
    """Polymarket pm1 matched к top-K canonical events (default K=2)."""
    pm = PolymarketMarketInfo(
        id="pm1", condition_id="0x1", question="Trump?", inferred_category="politics", current_price=0.5
    )
    canonical = [
        CanonicalEventInfo(id=f"ce{i}", slug=f"s{i}", title=f"T{i}", category="politics") for i in range(5)
    ]
    # Embeddings designed so similarities декрементируются
    pm_embed = [1.0, 0.0]
    ce_embeddings = [
        [1.0, 0.0],  # sim 1.0
        [0.95, 0.31],  # ~0.95
        [0.80, 0.60],  # ~0.80
        [0.60, 0.80],  # ~0.60 (below flag)
        [0.50, 0.87],  # ~0.50 (below flag)
    ]
    result = match_polymarket_to_canonical([pm], canonical, [pm_embed], ce_embeddings, top_k=2)
    assert len(result) == 2  # only top 2 above flag
    # Sorted by similarity desc
    assert result[0].similarity_score > result[1].similarity_score


def test_match_embedding_length_mismatch_raises_polymarket():
    pm = PolymarketMarketInfo(
        id="pm1", condition_id="0x1", question="Q", inferred_category="politics", current_price=0.5
    )
    ce = CanonicalEventInfo(id="ce1", slug="ces", title="T", category="politics")
    with pytest.raises(ValueError):
        match_polymarket_to_canonical([pm, pm], [ce], [[1.0]], [[1.0]])


def test_match_embedding_length_mismatch_raises_canonical():
    pm = PolymarketMarketInfo(
        id="pm1", condition_id="0x1", question="Q", inferred_category="politics", current_price=0.5
    )
    ce = CanonicalEventInfo(id="ce1", slug="ces", title="T", category="politics")
    with pytest.raises(ValueError):
        match_polymarket_to_canonical([pm], [ce, ce], [[1.0]], [[1.0]])


def test_match_evidence_includes_metadata():
    pm = PolymarketMarketInfo(
        id="pm-uuid-9",
        condition_id="0xdeadbeef",
        question="Will Trump win?",
        inferred_category="politics",
        current_price=0.55,
    )
    ce = CanonicalEventInfo(id="ce1", slug="trump-2026", title="Trump impeachment", category="politics")
    embed = [1.0, 0.0]
    result = match_polymarket_to_canonical([pm], [ce], [embed], [embed])
    assert len(result) == 1
    ev = result[0].evidence
    assert ev["polymarket_market_id"] == "pm-uuid-9"
    assert ev["polymarket_condition_id"] == "0xdeadbeef"
    assert ev["canonical_slug"] == "trump-2026"
    assert ev["category"] == "politics"
    assert ev["polymarket_current_price"] == 0.55


def test_match_none_current_price_ok():
    """current_price=None propagates into evidence без ошибок."""
    pm = PolymarketMarketInfo(
        id="pm1", condition_id="0x1", question="Q", inferred_category="crypto", current_price=None
    )
    ce = CanonicalEventInfo(id="ce1", slug="ces", title="T", category="crypto")
    embed = [1.0, 0.0]
    result = match_polymarket_to_canonical([pm], [ce], [embed], [embed])
    assert len(result) == 1
    assert result[0].evidence["polymarket_current_price"] is None
