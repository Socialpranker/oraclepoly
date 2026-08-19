"""Manifold ↔ canonical_events matcher job — every 4 hours.

Workflow:
1. Fetch active Manifold markets (top by volume_24h, BINARY only)
2. Fetch active canonical_events
3. Embed questions/titles via OpenAI text-embedding-3-small
4. match_manifold_to_canonical → ManifoldMatchCandidate
5. Upsert venue_market_mappings (venue='manifold', matched_by='embedding')

Skip cases:
- No active Manifold markets (manifold-scrape ещё не run)
- No canonical_events (seed не loaded)

Cost: ~$0.01 per run (10K markets × 50 tokens × $0.02/1M).
Cron: every 4 hours (parity с Kalshi matcher).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.embeddings import get_embedding_service
from app.matchers.manifold_canonical_matcher import (
    CanonicalEventInfo,
    ManifoldMarketInfo,
    infer_manifold_category,
    match_manifold_to_canonical,
)
from app.services.database import execute, fetch

logger = structlog.get_logger()


# Filter — only top-volume Manifold markets (mana, не USD)
MIN_VOLUME_24H_MANA = 100.0
MAX_MANIFOLD_MARKETS = 500


_MANIFOLD_SQL = """
SELECT id, slug, question, description, probability,
       COALESCE(volume_24h, 0) AS volume_24h,
       raw
  FROM public.manifold_markets
 WHERE is_resolved = false
   AND outcome_type = 'BINARY'
   AND COALESCE(volume_24h, 0) >= $1
 ORDER BY volume_24h DESC NULLS LAST
 LIMIT $2
"""


_CANONICAL_SQL = """
SELECT id::text AS id,
       slug,
       title,
       description,
       category
  FROM public.canonical_events
 WHERE status = 'active'
"""


_UPSERT_MAPPING_SQL = """
INSERT INTO venue_market_mappings (
    canonical_event_id, venue, venue_market_id, venue_market_url,
    side_semantics, confidence_score, matched_by, notes
)
VALUES ($1, 'manifold', $2, $3, 'direct', $4, 'embedding', $5)
ON CONFLICT (canonical_event_id, venue, venue_market_id) DO UPDATE SET
    confidence_score = GREATEST(EXCLUDED.confidence_score, venue_market_mappings.confidence_score),
    -- Никогда не downgrade manual mapping через embedding match
    matched_by = CASE
        WHEN venue_market_mappings.matched_by = 'manual' THEN 'manual'
        ELSE EXCLUDED.matched_by
    END,
    notes = COALESCE(venue_market_mappings.notes, EXCLUDED.notes),
    expired_at = NULL
"""


def _manifold_text(question: str, description: str | None) -> str:
    """Manifold question + truncated description для embedding."""
    if not description:
        return question
    return f"{question}\n\n{description[:300]}"


def _canonical_text(title: str, description: str | None) -> str:
    if not description:
        return title
    return f"{title}\n\n{description[:300]}"


def _extract_group_slugs(raw: dict[str, Any] | None) -> list[str]:
    """Manifold market.raw содержит groupSlugs — используем для category inference."""
    if not raw or not isinstance(raw, dict):
        return []
    slugs = raw.get("groupSlugs") or []
    if isinstance(slugs, list):
        return [str(s) for s in slugs]
    return []


def _build_manifold_info(row: dict[str, Any]) -> ManifoldMarketInfo | None:
    question = row.get("question") or ""
    if not question:
        return None
    raw = row.get("raw")
    # asyncpg returns JSONB as dict (or str — handle both)
    if isinstance(raw, str):
        try:
            import json

            raw = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            raw = None
    group_slugs = _extract_group_slugs(raw if isinstance(raw, dict) else None)
    category = infer_manifold_category(question, group_slugs=group_slugs)
    probability = row.get("probability")
    return ManifoldMarketInfo(
        id=str(row["id"]),
        slug=str(row.get("slug") or row["id"]),
        question=question,
        inferred_category=category,
        probability=float(probability) if probability is not None else None,
    )


def _build_canonical_info(row: dict[str, Any]) -> CanonicalEventInfo | None:
    title = row.get("title") or ""
    if not title:
        return None
    category = (row.get("category") or "macro").lower()
    return CanonicalEventInfo(
        id=str(row["id"]),
        slug=str(row.get("slug") or row["id"]),
        title=title,
        category=category,
    )


async def run_manifold_canonical_match() -> dict[str, Any]:
    """Embed manifold + canonical, match, upsert venue_market_mappings."""
    started = datetime.now(UTC)
    manifold_rows = await fetch(_MANIFOLD_SQL, MIN_VOLUME_24H_MANA, MAX_MANIFOLD_MARKETS)
    canonical_rows = await fetch(_CANONICAL_SQL)

    if not manifold_rows:
        logger.info("manifold_match_skipped_no_manifold", canonical_count=len(canonical_rows))
        return {"status": "skipped", "reason": "no_manifold_markets"}
    if not canonical_rows:
        logger.info("manifold_match_skipped_no_canonical", manifold_count=len(manifold_rows))
        return {"status": "skipped", "reason": "no_canonical_events"}

    manifold_infos: list[ManifoldMarketInfo] = []
    for row in manifold_rows:
        info = _build_manifold_info(dict(row))
        if info is not None:
            manifold_infos.append(info)

    canonical_infos: list[CanonicalEventInfo] = []
    for row in canonical_rows:
        info = _build_canonical_info(dict(row))
        if info is not None:
            canonical_infos.append(info)

    if not manifold_infos or not canonical_infos:
        logger.info(
            "manifold_match_skipped_empty_after_build",
            manifold=len(manifold_infos),
            canonical=len(canonical_infos),
        )
        return {"status": "skipped", "reason": "empty_after_build"}

    # Embed
    embed_svc = get_embedding_service()
    try:
        manifold_texts = [
            _manifold_text(
                m.question,
                next(
                    (str(r.get("description")) for r in manifold_rows if str(r["id"]) == m.id),
                    None,
                ),
            )
            for m in manifold_infos
        ]
        canonical_texts = [
            _canonical_text(
                c.title,
                next(
                    (str(r.get("description")) for r in canonical_rows if str(r["id"]) == c.id),
                    None,
                ),
            )
            for c in canonical_infos
        ]
        manifold_embeddings = await embed_svc.embed_batch(manifold_texts)
        canonical_embeddings = await embed_svc.embed_batch(canonical_texts)
    except Exception as exc:
        logger.error("manifold_match_embed_failed", error=str(exc))
        return {"status": "error", "reason": "embed_failed", "detail": str(exc)}

    # Match
    candidates = match_manifold_to_canonical(
        manifold_infos,
        canonical_infos,
        manifold_embeddings,
        canonical_embeddings,
    )

    # Upsert mappings (auto_matched only — flagged candidates can be Phase 3)
    n_upserted = 0
    for cand in candidates:
        if not cand.auto_matched:
            continue
        try:
            await execute(
                _UPSERT_MAPPING_SQL,
                cand.canonical_event_id,
                cand.manifold_market_id,
                f"https://manifold.markets/embed/{cand.evidence.get('manifold_slug')}",
                float(cand.similarity_score),
                f"matched via embedding (sim={cand.similarity_score:.3f})",
            )
            n_upserted += 1
        except Exception as exc:
            logger.warning(
                "manifold_match_upsert_failed",
                error=str(exc),
                manifold=cand.manifold_market_id,
                canonical=cand.canonical_event_id,
            )

    elapsed_sec = (datetime.now(UTC) - started).total_seconds()
    logger.info(
        "manifold_match_done",
        manifold_count=len(manifold_infos),
        canonical_count=len(canonical_infos),
        candidates_total=len(candidates),
        auto_matched_upserted=n_upserted,
        elapsed_sec=round(elapsed_sec, 2),
    )
    return {
        "status": "ok",
        "manifold_count": len(manifold_infos),
        "canonical_count": len(canonical_infos),
        "candidates": len(candidates),
        "auto_matched": n_upserted,
    }
