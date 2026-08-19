"""Polymarket markets ↔ canonical_events matcher (pure logic, no I/O).

Symmetric mirror of app/matchers/manifold_canonical_matcher.py. Reuses
`cosine_similarity` и `CanonicalEventInfo` оттуда (no duplication).

Workflow (caller responsibility — app/jobs/polymarket_canonical_match.py):
1. Fetch active markets (top by volume_24h, condition_id IS NOT NULL)
2. Fetch active canonical_events
3. embed_batch(market questions + canonical titles) — OpenAI text-embedding-3-small
4. match_polymarket_to_canonical(...) → list[PolymarketMatchCandidate]
5. Upsert venue_market_mappings (venue='polymarket', matched_by='embedding').
   venue_market_id = markets.id (UUID!) — это даёт vmm реальный markets.id,
   чтобы mv_manifold_polymarket_pairs нашёл пары (раньше там seed-плейсхолдеры
   вместо UUID).

Design:
- Pure module — нет network/db calls
- Category берём из markets.category (Polymarket structured) через
  map_polymarket_category → canonical cohort
- Top-K per Polymarket market (default 2)
- AUTO_MATCH_THRESHOLD = 0.80 (parity с Manifold matcher)
- FLAG_THRESHOLD = 0.65 (review manual)

Cohorts: politics/economics/crypto/sports/macro — те же, что в canonical_events.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Reuse pure helpers from the Manifold matcher — НЕ дублируем.
from app.matchers.manifold_canonical_matcher import (
    CanonicalEventInfo,
    cosine_similarity,
)

__all__ = [
    "AUTO_MATCH_THRESHOLD",
    "FLAG_THRESHOLD",
    "TOP_K",
    "CanonicalEventInfo",
    "PolymarketMarketInfo",
    "PolymarketMatchCandidate",
    "cosine_similarity",
    "map_polymarket_category",
    "match_polymarket_to_canonical",
]

# Thresholds — parity с Manifold matcher.
AUTO_MATCH_THRESHOLD = 0.80
FLAG_THRESHOLD = 0.65
TOP_K = 2


# Polymarket markets.category values → canonical cohort.
# Canonical cohorts (из canonical_events.category / Manifold infer):
# 'politics' | 'economics' | 'crypto' | 'sports' | 'macro'.
_CATEGORY_MAP: dict[str, str] = {
    "sports": "sports",
    "politics": "politics",
    "crypto": "crypto",
    "world & economy": "economics",
    "world and economy": "economics",
    "tech & ai": "macro",
    "tech and ai": "macro",
    "culture": "macro",
}


def map_polymarket_category(category: str) -> str:
    """Map Polymarket markets.category → canonical cohort.

    Returns one of: 'politics', 'economics', 'crypto', 'sports', 'macro' (default).
    Safe fallback to 'macro' для unknown/None categories.
    """
    if not category:
        return "macro"
    return _CATEGORY_MAP.get(category.strip().lower(), "macro")


@dataclass(frozen=True)
class PolymarketMarketInfo:
    """Polymarket market для matcher input."""

    id: str  # markets.id UUID string
    condition_id: str
    question: str
    inferred_category: str  # 'politics'|'economics'|'crypto'|'sports'|'macro'
    current_price: float | None  # current YES price


@dataclass(frozen=True)
class PolymarketMatchCandidate:
    """Match pair (Polymarket market ↔ canonical event)."""

    polymarket_market_id: str
    canonical_event_id: str
    similarity_score: float
    auto_matched: bool
    evidence: dict[str, object] = field(default_factory=dict)


def match_polymarket_to_canonical(
    polymarket_markets: list[PolymarketMarketInfo],
    canonical_events: list[CanonicalEventInfo],
    poly_embeddings: list[list[float]],
    canonical_embeddings: list[list[float]],
    *,
    auto_match_threshold: float = AUTO_MATCH_THRESHOLD,
    flag_threshold: float = FLAG_THRESHOLD,
    top_k: int = TOP_K,
) -> list[PolymarketMatchCandidate]:
    """Match Polymarket markets к canonical events via cosine similarity.

    Aligned embeddings: poly_embeddings[i] для polymarket_markets[i] etc.

    Returns all pairs с similarity ≥ flag_threshold, top-K per market,
    auto_matched flag if ≥ auto_match_threshold.

    Only matches within same category (cohort isolation).
    """
    if len(polymarket_markets) != len(poly_embeddings):
        raise ValueError(f"polymarket/embeddings length mismatch: {len(polymarket_markets)} vs {len(poly_embeddings)}")
    if len(canonical_events) != len(canonical_embeddings):
        raise ValueError(
            f"canonical/embeddings length mismatch: {len(canonical_events)} vs {len(canonical_embeddings)}"
        )

    # Index canonical events by category для O(1) cohort filter
    canonical_by_cat: dict[str, list[int]] = {}
    for idx, ce in enumerate(canonical_events):
        canonical_by_cat.setdefault(ce.category, []).append(idx)

    results: list[PolymarketMatchCandidate] = []
    for i, pm in enumerate(polymarket_markets):
        candidate_idxs = canonical_by_cat.get(pm.inferred_category, [])
        if not candidate_idxs:
            continue
        scored: list[tuple[float, int]] = []
        for j in candidate_idxs:
            sim = cosine_similarity(poly_embeddings[i], canonical_embeddings[j])
            if sim >= flag_threshold:
                scored.append((sim, j))
        scored.sort(key=lambda t: t[0], reverse=True)
        for sim, j in scored[:top_k]:
            ce = canonical_events[j]
            results.append(
                PolymarketMatchCandidate(
                    polymarket_market_id=pm.id,
                    canonical_event_id=ce.id,
                    similarity_score=round(sim, 4),
                    auto_matched=sim >= auto_match_threshold,
                    evidence={
                        "polymarket_market_id": pm.id,
                        "polymarket_condition_id": pm.condition_id,
                        "polymarket_question": pm.question[:200],
                        "canonical_slug": ce.slug,
                        "canonical_title": ce.title[:200],
                        "category": pm.inferred_category,
                        "polymarket_current_price": pm.current_price,
                    },
                )
            )
    return results
