"""Kalshi ↔ Polymarket matcher job — Phase 1.1 (every 4 hours).

Workflow:
1. Fetch active markets из обеих платформ (filter Phase 1 cohort: Sports/Economics/Politics)
2. Generate embeddings через EmbeddingService.embed_batch (OpenAI text-embedding-3-small)
3. Pure matcher → MatchCandidate list
4. Upsert в market_pair_matches (auto_matched=true для ≥0.85, false для 0.70-0.85)
5. Expire pairs где один из маркетов более не active

Cost note: OpenAI text-embedding-3-small ~$0.02 / 1M tokens. Active markets ~10K total
~50 tokens each → ~500K tokens / run = $0.01. Безопасно crontab каждые 4 часа.

Source: research/kalshi-integration-2026-05/prd.md Phase 1.1
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.embeddings import get_embedding_service
from app.matchers.kalshi_polymarket_matcher import (
    AUTO_MATCH_THRESHOLD,
    FLAG_THRESHOLD,
    TOP_K,
    MarketInfo,
    match_markets,
    normalize_kalshi_category,
    normalize_polymarket_category,
)
from app.services.database import execute, fetch

logger = structlog.get_logger()


# ─── SQL ─────────────────────────────────────────────────────────────────────

# Min volume filter: $5K per platform (из PRD)
MIN_VOLUME_USD = 5000.0

# Conservative cap чтобы не embed всё подряд — top по volume
MAX_MARKETS_PER_PLATFORM = 800

# Approximate $1 / Kalshi contract; we don't have USD volume column,
# но contracts × средняя цена ≈ USD volume. Используем contracts × 0.5 как proxy.
KALSHI_CONTRACTS_TO_USD = 0.5


_POLYMARKET_SQL = """
SELECT polymarket_id, question, description, category,
       current_price, COALESCE(volume_24h, 0) AS volume_24h
  FROM public.markets
 WHERE is_active = true
   AND resolved = false
   AND category = ANY($1::text[])
   AND COALESCE(volume_24h, 0) >= $2
 ORDER BY volume_24h DESC NULLS LAST
 LIMIT $3
"""


_KALSHI_SQL = """
SELECT ticker, title, subtitle, category,
       yes_ask_cents, yes_bid_cents, volume_24h_contracts
  FROM public.kalshi_markets
 WHERE is_active = true
   AND category = ANY($1::text[])
   AND COALESCE(volume_24h_contracts, 0) >= $2
 ORDER BY volume_24h_contracts DESC NULLS LAST
 LIMIT $3
"""


_UPSERT_MATCH_SQL = """
INSERT INTO public.market_pair_matches (
    polymarket_market_id, kalshi_ticker, similarity_score,
    auto_matched, inverse_side, notes, created_at, expired_at
)
VALUES ($1, $2, $3, $4, $5, $6, now(), NULL)
ON CONFLICT (polymarket_market_id, kalshi_ticker) DO UPDATE SET
    similarity_score = EXCLUDED.similarity_score,
    auto_matched     = EXCLUDED.auto_matched,
    inverse_side     = EXCLUDED.inverse_side,
    notes            = EXCLUDED.notes,
    expired_at       = NULL
"""


# Expire pairs где хотя бы один маркет не active
_EXPIRE_STALE_SQL = """
UPDATE public.market_pair_matches mpm
   SET expired_at = now()
 WHERE expired_at IS NULL
   AND (
        NOT EXISTS (
            SELECT 1 FROM public.markets m
             WHERE m.polymarket_id = mpm.polymarket_market_id
               AND m.is_active = true
               AND m.resolved = false
        )
     OR NOT EXISTS (
            SELECT 1 FROM public.kalshi_markets k
             WHERE k.ticker = mpm.kalshi_ticker
               AND k.is_active = true
        )
   )
"""


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _polymarket_text(question: str, description: str | None) -> str:
    """Concat question + description для embedding (более информативно)."""
    if not description:
        return question
    desc_trim = description[:300]  # description часто длинный, ограничиваем
    return f"{question}\n\n{desc_trim}"


def _kalshi_text(title: str, subtitle: str | None) -> str:
    """Concat title + subtitle для embedding."""
    if not subtitle:
        return title
    return f"{title}\n\n{subtitle}"


def _cents_to_float(cents: int | None) -> float | None:
    """Kalshi price: cents 1-99 → float 0.01..0.99. Returns None если cents is None."""
    if cents is None:
        return None
    return cents / 100.0


def _build_polymarket_marketinfo(row: dict[str, Any]) -> MarketInfo | None:
    """Convert DB row to MarketInfo. Returns None если category вне cohort."""
    cohort = normalize_polymarket_category(row.get("category"))
    if cohort is None:
        return None
    question = row.get("question") or ""
    if not question:
        return None
    yes_price = float(row["current_price"]) if row.get("current_price") is not None else None
    return MarketInfo(
        id=str(row["polymarket_id"]),
        text=_polymarket_text(question, row.get("description")),
        cohort=cohort,
        yes_price=yes_price,
        volume_usd=float(row.get("volume_24h") or 0),
    )


def _build_kalshi_marketinfo(row: dict[str, Any]) -> MarketInfo | None:
    """Convert DB row to MarketInfo. Returns None если category вне cohort."""
    cohort = normalize_kalshi_category(row.get("category"))
    if cohort is None:
        return None
    title = row.get("title") or ""
    if not title:
        return None
    yes_ask = _cents_to_float(row.get("yes_ask_cents"))
    yes_bid = _cents_to_float(row.get("yes_bid_cents"))
    # Use mid price если оба есть, иначе ask, иначе bid
    if yes_ask is not None and yes_bid is not None:
        yes_price: float | None = (yes_ask + yes_bid) / 2
    else:
        yes_price = yes_ask or yes_bid
    volume_contracts = float(row.get("volume_24h_contracts") or 0)
    return MarketInfo(
        id=str(row["ticker"]),
        text=_kalshi_text(title, row.get("subtitle")),
        cohort=cohort,
        yes_price=yes_price,
        volume_usd=volume_contracts * KALSHI_CONTRACTS_TO_USD,
    )


# ─── Job entry ───────────────────────────────────────────────────────────────


async def run_kalshi_polymarket_match() -> dict[str, Any]:
    """Pull both sides, embed, match, persist. Cron каждые 4 часа.

    Skip если no Kalshi markets in DB (Kalshi sync ещё не запускался).
    """
    started = datetime.now(UTC)
    cohort_categories_poly = list(["Sports", "Politics", "World & Economy"])
    cohort_categories_kalshi = list(["Sports", "Politics", "Economics"])

    # Min volume for Kalshi: контракты × средняя цена 0.5 ≈ USD = 5000 → 10000 contracts
    kalshi_min_contracts = int(MIN_VOLUME_USD / KALSHI_CONTRACTS_TO_USD)

    poly_rows = await fetch(_POLYMARKET_SQL, cohort_categories_poly, MIN_VOLUME_USD, MAX_MARKETS_PER_PLATFORM)
    kalshi_rows = await fetch(_KALSHI_SQL, cohort_categories_kalshi, kalshi_min_contracts, MAX_MARKETS_PER_PLATFORM)

    if not kalshi_rows:
        logger.info("kalshi_match_skipped_no_kalshi_markets", poly_count=len(poly_rows))
        return {"status": "skipped", "reason": "no_kalshi_markets", "poly_count": len(poly_rows)}

    poly_infos: list[MarketInfo] = []
    for row in poly_rows:
        info = _build_polymarket_marketinfo(dict(row))
        if info is not None:
            poly_infos.append(info)

    kalshi_infos: list[MarketInfo] = []
    for row in kalshi_rows:
        info = _build_kalshi_marketinfo(dict(row))
        if info is not None:
            kalshi_infos.append(info)

    if not poly_infos or not kalshi_infos:
        logger.info(
            "kalshi_match_skipped_empty_cohort",
            poly_in_cohort=len(poly_infos),
            kalshi_in_cohort=len(kalshi_infos),
        )
        return {
            "status": "skipped",
            "reason": "empty_cohort",
            "poly_in_cohort": len(poly_infos),
            "kalshi_in_cohort": len(kalshi_infos),
        }

    # Embed (batch — efficient + cheap)
    embed_svc = get_embedding_service()
    try:
        poly_embeddings = await embed_svc.embed_batch([m.text for m in poly_infos])
        kalshi_embeddings = await embed_svc.embed_batch([m.text for m in kalshi_infos])
    except Exception as exc:
        logger.error("kalshi_match_embed_failed", error=str(exc), error_type=type(exc).__name__)
        return {"status": "error", "reason": "embed_failed", "error": str(exc)}

    candidates = match_markets(
        poly_infos,
        kalshi_infos,
        poly_embeddings,
        kalshi_embeddings,
        auto_match_threshold=AUTO_MATCH_THRESHOLD,
        flag_threshold=FLAG_THRESHOLD,
        top_k=TOP_K,
    )

    # Persist matches
    n_inserted = n_auto = n_flagged = n_inverse = 0
    for c in candidates:
        try:
            notes = ""
            if c.inverse_side:
                notes = "inverse: poly_yes + kalshi_yes ≈ 1.0"
                n_inverse += 1
            await execute(
                _UPSERT_MATCH_SQL,
                c.polymarket_id,
                c.kalshi_ticker,
                c.similarity_score,
                c.auto_matched,
                c.inverse_side,
                notes,
            )
            n_inserted += 1
            if c.auto_matched:
                n_auto += 1
            else:
                n_flagged += 1
        except Exception as exc:
            logger.warning("kalshi_match_upsert_failed", poly=c.polymarket_id, kalshi=c.kalshi_ticker, error=str(exc))

    # Expire stale pairs
    expired = 0
    try:
        result = await execute(_EXPIRE_STALE_SQL)
        if isinstance(result, str) and result.startswith("UPDATE "):
            expired = int(result.split()[1])
    except Exception as exc:
        logger.warning("kalshi_match_expire_failed", error=str(exc))

    elapsed = (datetime.now(UTC) - started).total_seconds()
    logger.info(
        "kalshi_match_completed",
        poly_count=len(poly_infos),
        kalshi_count=len(kalshi_infos),
        candidates=len(candidates),
        auto_matched=n_auto,
        flagged=n_flagged,
        inverse=n_inverse,
        expired=expired,
        elapsed_sec=round(elapsed, 2),
    )
    return {
        "status": "ok",
        "poly_count": len(poly_infos),
        "kalshi_count": len(kalshi_infos),
        "candidates": len(candidates),
        "auto_matched": n_auto,
        "flagged": n_flagged,
        "inverse": n_inverse,
        "expired": expired,
        "elapsed_sec": round(elapsed, 2),
    }
