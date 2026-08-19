"""Canonical event generator — auto-populate canonical_events from Polymarket events.

Раньше canonical_events наполнялась ТОЛЬКО ручным seed (Phase 1, 20 событий в
migrations/seed/canonical_events_seed.yaml) → потолок Manifold lead-lag вкладки:
матчер находил ≤8 пар, т.к. матчить было не с чем (canonical_count=20).

Этот джоб закрывает Phase 2.2: берёт уже сгруппированные Polymarket events
(таблица `events`, FK markets.event_id) и для каждого ликвидного события создаёт
canonical_event 1:1. Polymarket-сторону venue_market_mappings ставим НАПРЯМУЮ из
FK (matched_by='exact') — связь event→markets известна точно, эмбеддинги для
своей же площадки избыточны. Manifold-сторону по-прежнему подтянет
manifold_canonical_match (cosine, каждые 4ч).

Workflow:
1. Fetch active events с ликвидным активным рынком (volume_24h >= min_volume)
2. Для каждого: upsert canonical_event (slug/title/category/end_date)
3. Upsert polymarket venue_market_mapping → самый ликвидный market события

Idempotent: re-run обновляет существующие (ON CONFLICT slug / composite PK).
Не trade-критичен, side-effect только запись в 2 таблицы.

Cron: every 6 hours (новые ликвидные события → новые canonical автоматически).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from app.matchers.polymarket_canonical_matcher import map_polymarket_category
from app.repositories.canonical_event_repository import get_canonical_event_repository
from app.services.database import fetch

logger = structlog.get_logger()


# Default liquidity floor — события дешевле редко имеют Manifold-аналог
# (Manifold тоньше Polymarket). Параметризуемо через run(..., min_volume=).
DEFAULT_MIN_VOLUME_24H = 50_000.0

# Slug-префикс для событий без собственного slug (fallback на polymarket_id).
_SLUG_FALLBACK_PREFIX = "pm-event"


# Events с активным ликвидным рынком + их самый ликвидный market (для mapping).
# DISTINCT ON выбирает топ-1 market по volume_24h на event (несёт основной сигнал
# и вероятнее всего сматчится с manifold-аналогом; mv ожидает 1 poly-рынок/пару).
_EVENTS_SQL = """
SELECT DISTINCT ON (e.id)
       e.id::text          AS event_id,
       e.slug              AS event_slug,
       e.polymarket_id     AS polymarket_id,
       e.title             AS title,
       e.description       AS description,
       e.category          AS category,
       e.end_date          AS end_date,
       m.id::text          AS market_id,
       m.condition_id      AS condition_id,
       m.slug              AS market_slug,
       COALESCE(m.volume_24h, 0) AS market_volume_24h
  FROM events e
  JOIN markets m ON m.event_id = e.id
 WHERE e.is_active = true
   AND m.is_active = true
   AND e.title IS NOT NULL
   AND COALESCE(m.volume_24h, 0) >= $1
 ORDER BY e.id, COALESCE(m.volume_24h, 0) DESC NULLS LAST
"""


def build_canonical_slug(event_slug: str | None, polymarket_id: str) -> str:
    """Canonical slug из event.slug (preferred) или fallback на polymarket_id.

    Polymarket event slug уже url-safe и стабилен. Fallback гарантирует
    уникальность для редких событий без slug.
    """
    slug = (event_slug or "").strip()
    if slug:
        return slug
    return f"{_SLUG_FALLBACK_PREFIX}-{polymarket_id}"


def build_polymarket_url(condition_id: str | None, market_slug: str | None) -> str | None:
    """Direct Polymarket link для debugging (slug-based, fallback condition_id)."""
    slug = (market_slug or "").strip()
    if slug:
        return f"https://polymarket.com/market/{slug}"
    cid = (condition_id or "").strip()
    if cid:
        return f"https://polymarket.com/market/{cid}"
    return None


def _coerce_resolves_at(end_date: Any) -> datetime | None:
    """events.end_date → expected_resolves_at (passthrough datetime, parse ISO str)."""
    if end_date is None or isinstance(end_date, datetime):
        return end_date
    if isinstance(end_date, str):
        try:
            return datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return None


async def run_canonical_event_generator(
    *,
    min_volume: float = DEFAULT_MIN_VOLUME_24H,
) -> dict[str, Any]:
    """Generate canonical_events from active liquid Polymarket events.

    Args:
        min_volume: liquidity floor (markets.volume_24h) для включения события.

    Returns:
        Status dict с counts: events_processed, canonical_upserted, mappings_upserted.
    """
    started = datetime.now()  # noqa: DTZ005 — relative elapsed only, не сериализуется
    rows = await fetch(_EVENTS_SQL, float(min_volume))

    if not rows:
        logger.info("canonical_event_generator_no_events", min_volume=min_volume)
        return {
            "status": "ok",
            "events_processed": 0,
            "canonical_upserted": 0,
            "mappings_upserted": 0,
        }

    repo = get_canonical_event_repository()
    n_canonical = 0
    n_mappings = 0

    for row in rows:
        data = dict(row)
        title = (data.get("title") or "").strip()
        if not title:
            continue

        polymarket_id = str(data.get("polymarket_id") or "")
        slug = build_canonical_slug(data.get("event_slug"), polymarket_id)
        category = map_polymarket_category(str(data.get("category") or ""))

        try:
            canonical_id = await repo.upsert_event(
                slug=slug,
                title=title,
                category=category,
                description=(data.get("description") or None),
                resolution_source="Polymarket UMA resolution",
                expected_resolves_at=_coerce_resolves_at(data.get("end_date")),
                notes="auto-generated from Polymarket event",
            )
            n_canonical += 1
        except Exception as exc:
            logger.warning("canonical_event_upsert_failed", slug=slug, error=str(exc))
            continue

        market_id = str(data.get("market_id") or "")
        if not market_id:
            continue
        try:
            await repo.upsert_mapping(
                canonical_event_id=canonical_id,
                venue="polymarket",
                venue_market_id=market_id,
                venue_market_url=build_polymarket_url(data.get("condition_id"), data.get("market_slug")),
                confidence_score=1.0,
                matched_by="exact",
                notes="FK markets.event_id → canonical (top-volume market)",
            )
            n_mappings += 1
        except Exception as exc:
            logger.warning(
                "canonical_event_mapping_failed",
                slug=slug,
                market_id=market_id,
                error=str(exc),
            )

    elapsed_sec = (datetime.now() - started).total_seconds()  # noqa: DTZ005
    logger.info(
        "canonical_event_generator_done",
        events_processed=len(rows),
        canonical_upserted=n_canonical,
        mappings_upserted=n_mappings,
        min_volume=min_volume,
        elapsed_sec=round(elapsed_sec, 2),
    )
    return {
        "status": "ok",
        "events_processed": len(rows),
        "canonical_upserted": n_canonical,
        "mappings_upserted": n_mappings,
    }
