"""Kalshi ↔ Polymarket matcher — unit tests (pure module).

Covers:
- Category normalization (Polymarket → cohort, Kalshi → cohort)
- Cosine similarity correctness
- Inverse detection heuristic
- Main match_markets logic (cohort filter, top-K, threshold tiers)
"""

from __future__ import annotations

import math

import pytest

from app.matchers.kalshi_polymarket_matcher import (
    AUTO_MATCH_THRESHOLD,
    FLAG_THRESHOLD,
    MarketInfo,
    cosine_similarity,
    detect_inverse,
    match_markets,
    normalize_kalshi_category,
    normalize_polymarket_category,
)

# ─── Category normalization ──────────────────────────────────────────────────


def test_normalize_polymarket_sports():
    assert normalize_polymarket_category("Sports") == "Sports"


def test_normalize_polymarket_politics():
    assert normalize_polymarket_category("Politics") == "Politics"


def test_normalize_polymarket_world_economy_maps_to_economics():
    assert normalize_polymarket_category("World & Economy") == "Economics"


def test_normalize_polymarket_crypto_excluded():
    """Crypto не в Phase 1 cohort."""
    assert normalize_polymarket_category("Crypto") is None


def test_normalize_polymarket_culture_excluded():
    assert normalize_polymarket_category("Culture") is None


def test_normalize_polymarket_none():
    assert normalize_polymarket_category(None) is None
    assert normalize_polymarket_category("") is None


def test_normalize_kalshi_sports():
    assert normalize_kalshi_category("Sports") == "Sports"


def test_normalize_kalshi_economics():
    assert normalize_kalshi_category("Economics") == "Economics"


def test_normalize_kalshi_outside_cohort():
    """Anything outside {Sports, Politics, Economics} returns None."""
    assert normalize_kalshi_category("Weather") is None
    assert normalize_kalshi_category("Climate") is None
    assert normalize_kalshi_category(None) is None


# ─── Cosine ──────────────────────────────────────────────────────────────────


def test_cosine_identical_vectors_is_one():
    v = [0.6, 0.8]  # already normalized (3-4-5 triangle / 5)
    assert cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_vectors_is_minus_one():
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_handles_zero_vector():
    """Zero vector → return 0 (avoid div by zero)."""
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0


def test_cosine_length_mismatch_raises():
    with pytest.raises(ValueError, match="length mismatch"):
        cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


def test_cosine_unnormalized_vectors():
    """Cosine independent of magnitude."""
    a = [3.0, 4.0]
    b = [6.0, 8.0]  # collinear, double length
    assert cosine_similarity(a, b) == pytest.approx(1.0)


# ─── Inverse detection ──────────────────────────────────────────────────────


def test_detect_inverse_classic_pair():
    """Poly YES 0.7, Kalshi YES 0.3 → sum=1.0 → inverse."""
    assert detect_inverse(0.70, 0.30) is True


def test_detect_inverse_near_pair():
    """Poly 0.65, Kalshi 0.32 → sum=0.97, within 0.05 band → inverse."""
    assert detect_inverse(0.65, 0.32) is True


def test_detect_not_inverse_aligned():
    """Both saying YES likely — direct match, NOT inverse."""
    assert detect_inverse(0.60, 0.62) is False


def test_detect_not_inverse_extreme_zones():
    """В extreme zones (>0.90 или <0.10) эвристика выключена, чтобы не путаться."""
    assert detect_inverse(0.95, 0.05) is False
    assert detect_inverse(0.05, 0.95) is False


def test_detect_inverse_none_handling():
    """If any price is None — can't judge, return False."""
    assert detect_inverse(None, 0.50) is False
    assert detect_inverse(0.50, None) is False
    assert detect_inverse(None, None) is False


# ─── match_markets ──────────────────────────────────────────────────────────


def _make_market(id_: str, text: str, cohort: str, yes: float | None = 0.5, vol: float = 10_000) -> MarketInfo:
    return MarketInfo(id=id_, text=text, cohort=cohort, yes_price=yes, volume_usd=vol)


def test_match_markets_basic_auto_match():
    """Identical embedding vectors → cosine=1.0 → auto_matched=True."""
    poly = [_make_market("p1", "Trump wins NV", "Politics", yes=0.55)]
    kalshi = [_make_market("k1", "Will Trump win NV", "Politics", yes=0.57)]
    embed = [1.0, 0.0]  # arbitrary fixed vector
    candidates = match_markets(poly, kalshi, [embed], [embed])
    assert len(candidates) == 1
    c = candidates[0]
    assert c.polymarket_id == "p1"
    assert c.kalshi_ticker == "k1"
    assert c.similarity_score == pytest.approx(1.0)
    assert c.auto_matched is True
    assert c.inverse_side is False


def test_match_markets_different_cohort_skipped():
    """Cohort filter: Politics Poly не матчится с Sports Kalshi даже при cosine=1.0."""
    poly = [_make_market("p1", "Trump wins", "Politics")]
    kalshi = [_make_market("k1", "Lakers win", "Sports")]
    embed = [1.0, 0.0]
    candidates = match_markets(poly, kalshi, [embed], [embed])
    assert candidates == []


def test_match_markets_below_flag_threshold_skipped():
    """Cosine < FLAG_THRESHOLD (0.70) → не возвращается."""
    poly = [_make_market("p1", "x", "Politics")]
    kalshi = [_make_market("k1", "y", "Politics")]
    # 60° between vectors → cosine = 0.5
    angle = math.pi / 3
    poly_emb = [1.0, 0.0]
    kalshi_emb = [math.cos(angle), math.sin(angle)]
    candidates = match_markets(poly, kalshi, [poly_emb], [kalshi_emb])
    assert candidates == []


def test_match_markets_flagged_zone():
    """Cosine в [0.70, 0.85) → auto_matched=False но возвращается."""
    poly = [_make_market("p1", "x", "Politics")]
    kalshi = [_make_market("k1", "y", "Politics")]
    # cosine = 0.75 (between FLAG_THRESHOLD=0.70 and AUTO_MATCH=0.85)
    angle = math.acos(0.75)
    poly_emb = [1.0, 0.0]
    kalshi_emb = [math.cos(angle), math.sin(angle)]
    candidates = match_markets(poly, kalshi, [poly_emb], [kalshi_emb])
    assert len(candidates) == 1
    c = candidates[0]
    assert c.similarity_score == pytest.approx(0.75, abs=0.001)
    assert c.auto_matched is False
    assert FLAG_THRESHOLD <= c.similarity_score < AUTO_MATCH_THRESHOLD


def test_match_markets_top_k_limit():
    """For each Polymarket → only top-K Kalshi candidates."""
    poly = [_make_market("p1", "x", "Politics")]
    # 5 Kalshi с разной similarity к poly
    kalshi = [_make_market(f"k{i}", "y", "Politics") for i in range(5)]
    poly_emb = [1.0, 0.0]
    # Сделаем разные cosines: 0.99, 0.95, 0.90, 0.85, 0.80
    kalshi_embs = []
    for cos_val in [0.99, 0.95, 0.90, 0.85, 0.80]:
        angle = math.acos(cos_val)
        kalshi_embs.append([math.cos(angle), math.sin(angle)])
    candidates = match_markets(poly, kalshi, [poly_emb], kalshi_embs, top_k=3)
    # Должны вернуть только top-3
    assert len(candidates) == 3
    # Sorted descending
    sims = [c.similarity_score for c in candidates]
    assert sims == sorted(sims, reverse=True)
    assert sims[0] >= sims[1] >= sims[2]


def test_match_markets_inverse_detection_propagates():
    """Если price gradient signals inverse — флаг inverse_side=True."""
    poly = [_make_market("p1", "X wins", "Politics", yes=0.70)]
    kalshi = [_make_market("k1", "X does not win", "Politics", yes=0.30)]
    embed = [1.0, 0.0]
    candidates = match_markets(poly, kalshi, [embed], [embed])
    assert len(candidates) == 1
    assert candidates[0].inverse_side is True


def test_match_markets_evidence_contains_metadata():
    poly = [_make_market("p1", "Some question", "Politics", yes=0.50)]
    kalshi = [_make_market("k1", "Some title", "Politics", yes=0.50)]
    embed = [1.0, 0.0]
    candidates = match_markets(poly, kalshi, [embed], [embed])
    e = candidates[0].evidence
    assert e["cohort"] == "Politics"
    assert e["poly_yes"] == 0.50
    assert e["kalshi_yes"] == 0.50
    assert "Some question" in e["polymarket_text"]


def test_match_markets_length_mismatch_raises():
    poly = [_make_market("p1", "x", "Politics")]
    kalshi = [_make_market("k1", "y", "Politics")]
    with pytest.raises(ValueError, match="polymarket/embeddings"):
        match_markets(poly, kalshi, [[1.0, 0.0], [0.5, 0.5]], [[1.0, 0.0]])


def test_match_markets_empty_inputs():
    """No markets → no candidates."""
    assert match_markets([], [], [], []) == []


def test_match_markets_no_cohort_match():
    """Polymarket cohort A, Kalshi cohort B → no candidates."""
    poly = [_make_market("p1", "x", "Sports")]
    kalshi = [_make_market("k1", "y", "Economics")]
    assert match_markets(poly, kalshi, [[1.0, 0.0]], [[1.0, 0.0]]) == []
