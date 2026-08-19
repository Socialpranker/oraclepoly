"""Tests for EventRepository."""

from unittest.mock import AsyncMock, patch

import pytest

from app.repositories.event_repository import EventRepository


@pytest.mark.asyncio
async def test_upsert_event_calls_database():
    """upsert_event calls fetchrow with correct SQL and returns the row."""
    mock_row = {"id": "uuid-1", "polymarket_id": "evt-1", "title": "Test Event", "category": "Politics"}

    with patch("app.repositories.event_repository.fetchrow", new_callable=AsyncMock, return_value=mock_row):
        repo = EventRepository()
        result = await repo.upsert_event(
            {
                "polymarket_id": "evt-1",
                "title": "Test Event",
                "category": "Politics",
            }
        )

    assert result["id"] == "uuid-1"


@pytest.mark.asyncio
async def test_get_active_events():
    """get_active_events returns filtered, ordered results."""
    mock_rows = [
        {"id": "uuid-1", "title": "Event 1"},
        {"id": "uuid-2", "title": "Event 2"},
    ]

    with patch("app.repositories.event_repository.fetch", new_callable=AsyncMock, return_value=mock_rows):
        repo = EventRepository()
        results = await repo.get_active_events(limit=50)

    assert len(results) == 2


@pytest.mark.asyncio
async def test_get_active_events_with_category():
    """get_active_events filters by category when provided."""
    mock_rows = [
        {"id": "uuid-1", "title": "Event 1", "category": "Politics"},
    ]

    with patch("app.repositories.event_repository.fetch", new_callable=AsyncMock, return_value=mock_rows):
        repo = EventRepository()
        results = await repo.get_active_events(category="Politics", limit=50)

    assert len(results) == 1


@pytest.mark.asyncio
async def test_get_event_by_slug():
    """get_event_by_slug returns single event or None."""
    mock_row = {"id": "uuid-1", "slug": "test-event", "title": "Test"}

    with patch("app.repositories.event_repository.fetchrow", new_callable=AsyncMock, return_value=mock_row):
        repo = EventRepository()
        result = await repo.get_event_by_slug("test-event")

    assert result is not None
    assert result["slug"] == "test-event"


@pytest.mark.asyncio
async def test_get_event_by_slug_not_found():
    """get_event_by_slug returns None when not found."""
    with patch("app.repositories.event_repository.fetchrow", new_callable=AsyncMock, return_value=None):
        repo = EventRepository()
        result = await repo.get_event_by_slug("nonexistent")

    assert result is None


@pytest.mark.asyncio
async def test_deactivate_stale_events():
    """deactivate_stale_events marks events not in seen_ids as inactive."""
    # execute returns "UPDATE 1" meaning 1 row was updated
    with patch("app.repositories.event_repository.execute", new_callable=AsyncMock, return_value="UPDATE 1"):
        repo = EventRepository()
        count = repo.deactivate_stale_events(seen_polymarket_ids=["evt-1", "evt-2"])
        count = await count

    assert count == 1  # 1 stale event deactivated
