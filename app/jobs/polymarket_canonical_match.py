"""Polymarket markets ↔ canonical_events matcher job — every 4 hours.

Symmetric mirror of app/jobs/manifold_canonical_match.py.

Goal: наполнить venue_market_mappings реальными markets.id (UUID) для
venue='polymarket', чтобы mv_manifold_polymarket_pairs нашёл smart-vs-retail
пары (раньше polymarket-маппинги были seed-плейсхолдеры, не UUID → 0 пар).

Workflow:
1. Fetch active Polymarket markets (top by volume_24h, condition_id IS NOT NULL)
2. Fetch active canonical_events
3. Embed questions/titles via OpenAI text-embedding-3-small
4. match_polymarket_to_canonical → PolymarketMatchCandidate
5. Upsert venue_market_mappings (venue='polymarket', matched_by='embedding',
   venue_market_id = markets.id UUID)

Skip cases:
- No active Polymarket markets (market-sync ещё не run / БД пуста)
- No canonical_events (seed не loaded)

Cost: ~$0.01 per run (≤500 markets × ~50 tokens × $0.02/1M).
Cron: every 4 hours (parity с Manifold/Kalshi matchers).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.embeddings import get_embedding_service
from app.matchers.polymarket_canonical_matcher import (
    CanonicalEventInfo,
    PolymarketMarketInfo,
    map_polymarket_category,
    match_polymarket_to_canonical,
)
from app.services.database import execute, fetch

logger = structlog.get_logger()


# Filter — only top-volume active markets с condition_id.
MIN_VOLUME_24H = 100.0
MAX_POLYMARKET_MARKETS = 500


_POLYMARKET_SQL = """
SELECT id::text AS id,
       condition_id,
       question,
       category,
       current_price,
       COALESCE(volume_24h, 0) AS volume_24h
  FROM public.markets
 WHERE is_active = true
   AND condition_id IS NOT NULL
   AND question IS NOT NULL
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
VALUES ($1, 'polymarket', $2, $3, 'direct', $4, 'embedding', $5)
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


def _polymarket_text(question: str) -> str:
    """Polymarket question для embedding (markets нет отдельного description-поля
    в matcher input — используем только question)."""
    return question


def _canonical_text(title: str, description: str | None) -> str:
    if not description:
        return title
    return f"{title}\n\n{description[:300]}"


def _build_polymarket_info(row: dict[str, Any]) -> PolymarketMarketInfo | None:
    question = row.get("question") or ""
    condition_id = row.get("condition_id") or ""
    if not question or not condition_id:
        return None
    category = map_polymarket_category(str(row.get("category") or ""))
    current_price = row.get("current_price")
    return PolymarketMarketInfo(
        id=str(row["id"]),
        condition_id=str(condition_id),
        question=question,
        inferred_category=category,
        current_price=float(current_price) if current_price is not None else None,
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


async def run_polymarket_canonical_match() -> dict[str, Any]:
    """Embed polymarket + canonical, match, upsert venue_market_mappings."""
    started = datetime.now(UTC)
    polymarket_rows = await fetch(_POLYMARKET_SQL, MIN_VOLUME_24H, MAX_POLYMARKET_MARKETS)
    canonical_rows = await fetch(_CANONICAL_SQL)

    if not polymarket_rows:
        logger.info("polymarket_match_skipped_no_polymarket", canonical_count=len(canonical_rows))
        return {"status": "skipped", "reason": "no_polymarket_markets"}
    if not canonical_rows:
        logger.info("polymarket_match_skipped_no_canonical", polymarket_count=len(polymarket_rows))
        return {"status": "skipped", "reason": "no_canonical_events"}

    polymarket_infos: list[PolymarketMarketInfo] = []
    for row in polymarket_rows:
        info = _build_polymarket_info(dict(row))
        if info is not None:
            polymarket_infos.append(info)

    canonical_infos: list[CanonicalEventInfo] = []
    for row in canonical_rows:
        info = _build_canonical_info(dict(row))
        if info is not None:
            canonical_infos.append(info)

    if not polymarket_infos or not canonical_infos:
        logger.info(
            "polymarket_match_skipped_empty_after_build",
            polymarket=len(polymarket_infos),
            canonical=len(canonical_infos),
        )
        return {"status": "skipped", "reason": "empty_after_build"}

    # Embed
    embed_svc = get_embedding_service()
    try:
        polymarket_texts = [_polymarket_text(pm.question) for pm in polymarket_infos]
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
        polymarket_embeddings = await embed_svc.embed_batch(polymarket_texts)
        canonical_embeddings = await embed_svc.embed_batch(canonical_texts)
    except Exception as exc:
        logger.error("polymarket_match_embed_failed", error=str(exc))
        return {"status": "error", "reason": "embed_failed", "detail": str(exc)}

    # Match
    candidates = match_polymarket_to_canonical(
        polymarket_infos,
        canonical_infos,
        polymarket_embeddings,
        canonical_embeddings,
    )

    # Upsert mappings (auto_matched only — flagged candidates can be Phase 3)
    n_upserted = 0
    for cand in candidates:
        if not cand.auto_matched:
            continue
        try:
            condition_id = cand.evidence.get("polymarket_condition_id")
            venue_market_url = f"https://polymarket.com/market/{condition_id}" if condition_id else None
            await execute(
                _UPSERT_MAPPING_SQL,
                cand.canonical_event_id,
                cand.polymarket_market_id,
                venue_market_url,
                float(cand.similarity_score),
                f"matched via embedding (sim={cand.similarity_score:.3f})",
            )
            n_upserted += 1
        except Exception as exc:
            logger.warning(
                "polymarket_match_upsert_failed",
                error=str(exc),
                polymarket=cand.polymarket_market_id,
                canonical=cand.canonical_event_id,
            )

    elapsed_sec = (datetime.now(UTC) - started).total_seconds()
    logger.info(
        "polymarket_match_done",
        polymarket_count=len(polymarket_infos),
        canonical_count=len(canonical_infos),
        candidates_total=len(candidates),
        auto_matched_upserted=n_upserted,
        elapsed_sec=round(elapsed_sec, 2),
    )
    return {
        "status": "ok",
        "polymarket_count": len(polymarket_infos),
        "canonical_count": len(canonical_infos),
        "candidates": len(candidates),
        "auto_matched": n_upserted,
    }
