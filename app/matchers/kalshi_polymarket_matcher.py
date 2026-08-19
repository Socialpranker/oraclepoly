"""Kalshi ↔ Polymarket market matcher (pure logic, no I/O).

Source: research/kalshi-integration-2026-05/prd.md Phase 1.1.

Workflow (caller responsibility):
1. Caller fetches active markets из обеих платформ
2. Caller получает embeddings через app.core.embeddings.EmbeddingService.embed_batch
3. Caller вызывает `match_markets(...)` → возвращает [MatchCandidate, ...]
4. Caller persist'ит в market_pair_matches (см. app/jobs/kalshi_polymarket_match.py)

Design decisions:
- Pure module — нет network/db calls (легко тестировать)
- Category mapping hardcoded в `POLYMARKET_TO_COHORT` — Phase 1 cohort: Sports + Economics + Politics
- Inverse detection через price gradient: |yes_poly + yes_kalshi - 1.0| < 0.05 → flip
- Top-k=3 candidates per Polymarket market, returns ones above AUTO_MATCH_THRESHOLD
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

# Phase 1 cohort из решения по PRD: Sports + Economics + Politics
# Polymarket "World & Economy" мапим как "Economics" (Kalshi convention)
POLYMARKET_TO_COHORT: dict[str, str] = {
    "Sports": "Sports",
    "Politics": "Politics",
    "World & Economy": "Economics",
}

# Kalshi category names per их docs (capitalized)
KALSHI_COHORT_CATEGORIES: set[str] = {"Sports", "Politics", "Economics"}

# Auto-match threshold (cosine similarity). Below this — flagged для manual review.
AUTO_MATCH_THRESHOLD = 0.85

# Flag threshold — pairs в зоне [FLAG, AUTO_MATCH) пишутся с auto_matched=false
FLAG_THRESHOLD = 0.70

# Top-N candidates per Polymarket market (returned even ниже threshold, для TG review)
TOP_K = 3

# Inverse market detection: если sum(yes_poly, yes_kalshi) ≈ 1.0 → semantic mismatch
INVERSE_PRICE_SUM_BAND = 0.05  # |sum - 1.0| < этого = inverse


@dataclass(frozen=True)
class MarketInfo:
    """Generic market description для matcher input. Платформо-нейтральный."""

    id: str  # polymarket_id или kalshi ticker
    text: str  # текст для embedding (question/title + опционально description)
    cohort: str  # 'Sports'|'Politics'|'Economics' после mapping
    yes_price: float | None  # 0..1 (Polymarket) или /100 (Kalshi cents → float)
    volume_usd: float  # для filter min volume


@dataclass(frozen=True)
class MatchCandidate:
    """Результат matching — одна пара (Polymarket, Kalshi)."""

    polymarket_id: str
    kalshi_ticker: str
    similarity_score: float
    auto_matched: bool  # True если ≥ AUTO_MATCH_THRESHOLD
    inverse_side: bool  # True если sum(yes_a, yes_b) ≈ 1.0
    notes: str = ""
    evidence: dict[str, object] = field(default_factory=dict)


def normalize_polymarket_category(category: str | None) -> str | None:
    """Map Polymarket category → unified cohort label.

    Returns None если категория вне Phase 1 cohort.
    """
    if not category:
        return None
    return POLYMARKET_TO_COHORT.get(category)


def normalize_kalshi_category(category: str | None) -> str | None:
    """Map Kalshi category → unified cohort label.

    Kalshi уже использует "Sports", "Politics", "Economics".
    """
    if not category:
        return None
    if category in KALSHI_COHORT_CATEGORIES:
        return category
    return None


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Plain cosine. a, b — equal-length vectors. Returns 0.0 если zero vector."""
    if len(a) != len(b):
        raise ValueError(f"Vector length mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def detect_inverse(poly_yes: float | None, kalshi_yes: float | None) -> bool:
    """Determine if pair is semantically inverse (Poly YES ↔ Kalshi NO).

    Heuristic: если sum of YES prices ≈ 1.0 (±0.05), это означает что Polymarket
    "X happens" matched с Kalshi "X does NOT happen". Не идеальная эвристика —
    может срабатывать ложно на близких 50/50 коинцидентно. Phase 2 добавит
    text-pattern detection ("will not", "remain below", etc.).

    Returns False если any price is None — нельзя судить.
    """
    if poly_yes is None or kalshi_yes is None:
        return False
    # И поли и кальшы должны быть в "решаемой" зоне, иначе суждение шумное
    if poly_yes < 0.10 or poly_yes > 0.90 or kalshi_yes < 0.10 or kalshi_yes > 0.90:
        return False
    return abs(poly_yes + kalshi_yes - 1.0) < INVERSE_PRICE_SUM_BAND


def match_markets(
    polymarket: list[MarketInfo],
    kalshi: list[MarketInfo],
    poly_embeddings: list[list[float]],
    kalshi_embeddings: list[list[float]],
    *,
    auto_match_threshold: float = AUTO_MATCH_THRESHOLD,
    flag_threshold: float = FLAG_THRESHOLD,
    top_k: int = TOP_K,
) -> list[MatchCandidate]:
    """Match Polymarket markets к Kalshi via cosine similarity over embeddings.

    Args:
        polymarket: list[MarketInfo] — active Polymarket markets, filtered to cohort
        kalshi: list[MarketInfo] — active Kalshi markets, filtered to cohort
        poly_embeddings: embedding vectors aligned to `polymarket` order
        kalshi_embeddings: embedding vectors aligned to `kalshi` order
        auto_match_threshold: ≥ this similarity → auto_matched=True
        flag_threshold: < this similarity → skip (not returned)
        top_k: candidates per Polymarket market (above flag_threshold only)

    Returns:
        list[MatchCandidate] — all pairs ≥ flag_threshold, with auto_matched flag

    Constraints:
        - Only matches within SAME cohort (Sports↔Sports etc)
        - Returns at most `top_k * len(polymarket)` rows (each Polymarket → top-K Kalshi)
    """
    if len(polymarket) != len(poly_embeddings):
        raise ValueError(f"polymarket/embeddings length mismatch: {len(polymarket)} vs {len(poly_embeddings)}")
    if len(kalshi) != len(kalshi_embeddings):
        raise ValueError(f"kalshi/embeddings length mismatch: {len(kalshi)} vs {len(kalshi_embeddings)}")

    # Index Kalshi by cohort для O(1) filter per Polymarket
    kalshi_by_cohort: dict[str, list[int]] = {}
    for idx, k in enumerate(kalshi):
        kalshi_by_cohort.setdefault(k.cohort, []).append(idx)

    results: list[MatchCandidate] = []
    for i, p in enumerate(polymarket):
        candidates_idxs = kalshi_by_cohort.get(p.cohort, [])
        if not candidates_idxs:
            continue
        # Score against same-cohort Kalshi markets
        scored: list[tuple[float, int]] = []
        for j in candidates_idxs:
            sim = cosine_similarity(poly_embeddings[i], kalshi_embeddings[j])
            if sim >= flag_threshold:
                scored.append((sim, j))
        # Top-K по сходству
        scored.sort(key=lambda t: t[0], reverse=True)
        for sim, j in scored[:top_k]:
            k = kalshi[j]
            results.append(
                MatchCandidate(
                    polymarket_id=p.id,
                    kalshi_ticker=k.id,
                    similarity_score=round(sim, 4),
                    auto_matched=sim >= auto_match_threshold,
                    inverse_side=detect_inverse(p.yes_price, k.yes_price),
                    evidence={
                        "polymarket_text": p.text[:200],
                        "kalshi_text": k.text[:200],
                        "cohort": p.cohort,
                        "poly_yes": p.yes_price,
                        "kalshi_yes": k.yes_price,
                    },
                )
            )
    return results
