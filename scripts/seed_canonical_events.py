"""Seed canonical_events + venue_market_mappings из YAML.

Idempotent: re-running обновляет существующие events (по slug) и mappings
(по PK canonical_event_id+venue+venue_market_id).

Usage:
    python scripts/seed_canonical_events.py
    python scripts/seed_canonical_events.py --file data/canonical_events_seed.yaml --dry-run

Phase 1: manual mapping. market_id поля — placeholder'ы для большинства
events (реальные id появятся через embedding-matcher когда Kalshi
credentials активируют pipeline).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import click
import yaml

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import structlog

from app.repositories.canonical_event_repository import get_canonical_event_repository
from app.services.database import close_db, init_db

logger = structlog.get_logger()


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


async def seed_events(yaml_path: Path, dry_run: bool = False) -> dict[str, int]:
    """Seed canonical_events + venue_market_mappings из YAML.

    Returns dict с counts: events_processed, mappings_processed.
    """
    with yaml_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    events = data.get("events") or []
    if not events:
        logger.warning("seed_canonical_events_empty_yaml", path=str(yaml_path))
        return {"events_processed": 0, "mappings_processed": 0}

    if dry_run:
        for ev in events:
            for field in ("slug", "title", "category"):
                if field not in ev:
                    raise ValueError(f"Missing required field '{field}' в event: {ev}")
            for v in ev.get("venues", []):
                for field in ("venue", "market_id"):
                    if field not in v:
                        raise ValueError(f"Missing '{field}' в venue mapping: {v}")
        logger.info("dry_run_validated", events=len(events))
        return {
            "events_processed": len(events),
            "mappings_processed": sum(len(e.get("venues", [])) for e in events),
        }

    repo = get_canonical_event_repository()
    n_events = n_mappings = 0

    for ev in events:
        canonical_id = await repo.upsert_event(
            slug=ev["slug"],
            title=ev["title"],
            category=ev["category"],
            description=ev.get("description"),
            resolution_criterion=ev.get("resolution_criterion"),
            resolution_source=ev.get("resolution_source"),
            expected_resolves_at=_parse_iso(ev.get("expected_resolves_at")),
            notes=ev.get("notes"),
        )
        n_events += 1

        for v in ev.get("venues", []):
            await repo.upsert_mapping(
                canonical_event_id=canonical_id,
                venue=v["venue"],
                venue_market_id=v["market_id"],
                venue_market_url=v.get("url"),
                side_semantics=v.get("side", "direct"),
                confidence_score=float(v.get("confidence_score", 1.0)),
                matched_by=v.get("matched_by", "manual"),
                notes=v.get("notes"),
            )
            n_mappings += 1

        logger.info(
            "canonical_event_seeded",
            slug=ev["slug"],
            id=canonical_id,
            n_venues=len(ev.get("venues", [])),
        )

    logger.info(
        "seed_canonical_events_done",
        events_processed=n_events,
        mappings_processed=n_mappings,
    )
    return {"events_processed": n_events, "mappings_processed": n_mappings}


@click.command()
@click.option(
    "--file",
    "yaml_file",
    default="migrations/seed/canonical_events_seed.yaml",
    show_default=True,
    help="YAML файл с seed data.",
)
@click.option("--dry-run", is_flag=True, help="Validate YAML без записи в БД.")
def main(yaml_file: str, dry_run: bool) -> None:
    """Seed canonical_events + venue_market_mappings."""
    yaml_path = Path(yaml_file)
    if not yaml_path.exists():
        click.echo(f"❌ YAML file not found: {yaml_path}", err=True)
        sys.exit(1)

    async def _run() -> None:
        if not dry_run:
            await init_db()
        try:
            result = await seed_events(yaml_path, dry_run=dry_run)
            click.echo(f"✓ Events processed: {result['events_processed']}")
            click.echo(f"✓ Mappings processed: {result['mappings_processed']}")
        finally:
            if not dry_run:
                await close_db()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
