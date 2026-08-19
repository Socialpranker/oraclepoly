"""Manifold ↔ canonical_events matcher (pure logic, no I/O).

Workflow (caller responsibility — app/jobs/manifold_canonical_match.py):
1. Fetch active manifold_markets + active canonical_events с venue='polymarket' mapping
   (we anchor canonical events that have Polymarket mapping — это даёт smart vs retail
   pair candidate)
2. embed_batch(manifold questions + canonical titles) — OpenAI text-embedding-3-small
3. match_manifold_to_canonical(...) → list[ManifoldMatchCandidate]
4. Upsert venue_market_mappings (venue='manifold', matched_by='embedding')

Design:
- Pure module — нет network/db calls
- Category inference из Manifold question text (Manifold не имеет structured category)
- Top-K per Manifold market (default 2)
- AUTO_MATCH_THRESHOLD = 0.80 (немного ниже Kalshi 0.85 потому что Manifold question
  wording часто более casual / retail style)
- FLAG_THRESHOLD = 0.65 (review manual)

Cohorts: politics/economics/crypto/sports/macro — те же, что в canonical_events.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

# Thresholds tuned для Manifold (more casual wording vs Kalshi structured).
AUTO_MATCH_THRESHOLD = 0.80
FLAG_THRESHOLD = 0.65
TOP_K = 2


# Category inference keywords. Manifold questions free-form, нет structured cat.
# Order: проверяем по приоритету. Многозначные ключи (e.g. "rate" в политике и
# в экономике) разрулены порядком.
_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    (
        "crypto",
        [
            "btc",
            "bitcoin",
            "eth",
            "ethereum",
            "sol",
            "solana",
            "doge",
            "crypto",
            "usdc",
            "usdt",
            "stablecoin",
            "altcoin",
            "nft",
            "defi",
        ],
    ),
    (
        "sports",
        [
            "super bowl",
            "nfl",
            "nba",
            "mlb",
            "nhl",
            "mls",
            "world cup",
            "fifa",
            "olympics",
            "olympic",
            "champions league",
            "premier league",
            "tennis",
            "f1",
            "formula one",
            "boxing",
            "ufc",
            "wrestlemania",
            "oscars",
            "eurovision",
            "tournament",
            "champion",
            "winner of",
        ],
    ),
    (
        "economics",
        [
            "fed",
            "fomc",
            "federal reserve",
            "rate cut",
            "rate hike",
            "rate decision",
            "cpi",
            "inflation",
            "unemployment",
            "jobs report",
            "gdp",
            "recession",
            "ecb",
            "boe",
            "bank of japan",
            "nfp",
            "payroll",
            "interest rate",
            "yield",
            "treasury",
            "sofr",
        ],
    ),
    (
        "politics",
        [
            "trump",
            "biden",
            "harris",
            "vance",
            "putin",
            "zelensky",
            "netanyahu",
            "election",
            "primary",
            "vote",
            "senate",
            "house",
            "congress",
            "impeach",
            "impeachment",
            "potus",
            "president",
            "governor",
            "supreme court",
            "scotus",
            "republican",
            "democrat",
            "midterm",
            "speakership",
            "resignation",
            "resign",
            "indictment",
            "indicted",
            "confirm",
            "politics",
            "political",
            "us-politics",
        ],
    ),
]


def infer_manifold_category(question: str, group_slugs: list[str] | None = None) -> str:
    """Infer category из Manifold question text + optional group_slugs.

    Returns one of: 'politics', 'economics', 'crypto', 'sports', 'macro' (default).
    """
    text = (question or "").lower()
    slugs_text = " ".join(group_slugs or []).lower()
    combined = f"{text} {slugs_text}"
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in combined for kw in keywords):
            return category
    return "macro"


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Plain cosine; returns 0 для zero vectors."""
    if len(a) != len(b):
        raise ValueError(f"Vector length mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass(frozen=True)
class ManifoldMarketInfo:
    """Manifold market для matcher input."""

    id: str
    slug: str
    question: str
    inferred_category: str  # 'politics'|'economics'|'crypto'|'sports'|'macro'
    probability: float | None  # current YES prob


@dataclass(frozen=True)
class CanonicalEventInfo:
    """Canonical event для matcher input."""

    id: str  # UUID string
    slug: str
    title: str
    category: str


@dataclass(frozen=True)
class ManifoldMatchCandidate:
    """Match pair (Manifold market ↔ canonical event)."""

    manifold_market_id: str
    canonical_event_id: str
    similarity_score: float
    auto_matched: bool
    evidence: dict[str, object] = field(default_factory=dict)


def match_manifold_to_canonical(
    manifold_markets: list[ManifoldMarketInfo],
    canonical_events: list[CanonicalEventInfo],
    manifold_embeddings: list[list[float]],
    canonical_embeddings: list[list[float]],
    *,
    auto_match_threshold: float = AUTO_MATCH_THRESHOLD,
    flag_threshold: float = FLAG_THRESHOLD,
    top_k: int = TOP_K,
) -> list[ManifoldMatchCandidate]:
    """Match Manifold markets к canonical events via cosine similarity.

    Aligned embeddings: manifold_embeddings[i] для manifold_markets[i] etc.

    Returns all pairs с similarity ≥ flag_threshold, top-K per manifold market,
    auto_matched flag if ≥ auto_match_threshold.

    Only matches within same category (cohort isolation).
    """
    if len(manifold_markets) != len(manifold_embeddings):
        raise ValueError(f"manifold/embeddings length mismatch: {len(manifold_markets)} vs {len(manifold_embeddings)}")
    if len(canonical_events) != len(canonical_embeddings):
        raise ValueError(
            f"canonical/embeddings length mismatch: {len(canonical_events)} vs {len(canonical_embeddings)}"
        )

    # Index canonical events by category для O(1) cohort filter
    canonical_by_cat: dict[str, list[int]] = {}
    for idx, ce in enumerate(canonical_events):
        canonical_by_cat.setdefault(ce.category, []).append(idx)

    results: list[ManifoldMatchCandidate] = []
    for i, m in enumerate(manifold_markets):
        candidate_idxs = canonical_by_cat.get(m.inferred_category, [])
        if not candidate_idxs:
            continue
        scored: list[tuple[float, int]] = []
        for j in candidate_idxs:
            sim = cosine_similarity(manifold_embeddings[i], canonical_embeddings[j])
            if sim >= flag_threshold:
                scored.append((sim, j))
        scored.sort(key=lambda t: t[0], reverse=True)
        for sim, j in scored[:top_k]:
            ce = canonical_events[j]
            results.append(
                ManifoldMatchCandidate(
                    manifold_market_id=m.id,
                    canonical_event_id=ce.id,
                    similarity_score=round(sim, 4),
                    auto_matched=sim >= auto_match_threshold,
                    evidence={
                        "manifold_slug": m.slug,
                        "manifold_question": m.question[:200],
                        "canonical_slug": ce.slug,
                        "canonical_title": ce.title[:200],
                        "category": m.inferred_category,
                        "manifold_probability": m.probability,
                    },
                )
            )
    return results
