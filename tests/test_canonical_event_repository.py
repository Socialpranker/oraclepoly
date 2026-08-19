"""CanonicalEventRepository — unit tests с mocked DB."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.repositories.canonical_event_repository import (
    CanonicalEventRepository,
    get_canonical_event_repository,
)


@pytest.fixture
def repo():
    return CanonicalEventRepository()


def test_factory_returns_instance():
    assert isinstance(get_canonical_event_repository(), CanonicalEventRepository)


@pytest.mark.asyncio
async def test_upsert_event_returns_uuid_str(repo):
    fake_uuid = "550e8400-e29b-41d4-a716-446655440000"
    with patch(
        "app.repositories.canonical_event_repository.fetchrow",
        new=AsyncMock(return_value={"id": fake_uuid}),
    ) as mock_fr:
        result = await repo.upsert_event(
            slug="2026-super-bowl-winner",
            title="2026 Super Bowl Winner",
            category="sports",
        )
    assert result == fake_uuid
    mock_fr.assert_called_once()
    args = mock_fr.call_args
    assert args.args[1] == "2026-super-bowl-winner"
    assert args.args[2] == "2026 Super Bowl Winner"
    assert args.args[3] == "sports"


@pytest.mark.asyncio
async def test_upsert_event_passes_optional_fields(repo):
    fake_uuid = "550e8400-e29b-41d4-a716-446655440000"
    expected_resolves = datetime(2026, 2, 8, 23, 0, tzinfo=UTC)
    with patch(
        "app.repositories.canonical_event_repository.fetchrow",
        new=AsyncMock(return_value={"id": fake_uuid}),
    ) as mock_fr:
        await repo.upsert_event(
            slug="fed-march-2026",
            title="Fed March 2026 Rate Decision",
            category="economics",
            description="FOMC March 18-19 meeting",
            resolution_criterion="effective federal funds rate change",
            resolution_source="Federal Reserve press release",
            expected_resolves_at=expected_resolves,
            notes="Macro event — paired Polymarket↔Kalshi",
        )
    args = mock_fr.call_args.args
    # args[0] = SQL, args[1:] = params
    assert args[4] == "FOMC March 18-19 meeting"
    assert args[5] == "effective federal funds rate change"
    assert args[6] == "Federal Reserve press release"
    assert args[7] == expected_resolves
    assert args[8] == "Macro event — paired Polymarket↔Kalshi"


@pytest.mark.asyncio
async def test_upsert_mapping_calls_execute(repo):
    with patch(
        "app.repositories.canonical_event_repository.execute",
        new=AsyncMock(return_value="INSERT 0 1"),
    ) as mock_ex:
        await repo.upsert_mapping(
            canonical_event_id="550e8400-e29b-41d4-a716-446655440000",
            venue="polymarket",
            venue_market_id="0xabc123",
            venue_market_url="https://polymarket.com/event/example",
            side_semantics="direct",
            confidence_score=1.0,
            matched_by="manual",
        )
    args = mock_ex.call_args.args
    assert args[1] == "550e8400-e29b-41d4-a716-446655440000"
    assert args[2] == "polymarket"
    assert args[3] == "0xabc123"
    assert args[5] == "direct"
    assert args[6] == 1.0
    assert args[7] == "manual"


@pytest.mark.asyncio
async def test_upsert_mapping_inverse_side(repo):
    with patch(
        "app.repositories.canonical_event_repository.execute",
        new=AsyncMock(return_value="INSERT 0 1"),
    ) as mock_ex:
        await repo.upsert_mapping(
            canonical_event_id="550e8400-e29b-41d4-a716-446655440000",
            venue="kalshi",
            venue_market_id="PRES-2024-NOV-DEM",
            side_semantics="inverse",  # Kalshi YES = canonical "Dem wins", canonical = "Trump wins"
            confidence_score=0.95,
            matched_by="manual",
        )
    args = mock_ex.call_args.args
    assert args[5] == "inverse"
    assert args[6] == 0.95


@pytest.mark.asyncio
async def test_get_by_slug_found(repo):
    fake_row = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "slug": "fed-march-2026",
        "title": "Fed March 2026",
        "category": "economics",
        "status": "active",
    }
    with patch(
        "app.repositories.canonical_event_repository.fetchrow",
        new=AsyncMock(return_value=fake_row),
    ):
        result = await repo.get_by_slug("fed-march-2026")
    assert result == fake_row


@pytest.mark.asyncio
async def test_get_by_slug_not_found_returns_none(repo):
    with patch(
        "app.repositories.canonical_event_repository.fetchrow",
        new=AsyncMock(return_value=None),
    ):
        result = await repo.get_by_slug("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_get_mappings_returns_list(repo):
    fake_rows = [
        {
            "venue": "polymarket", "venue_market_id": "0xabc", "venue_market_url": None,
            "side_semantics": "direct", "confidence_score": 1.0, "matched_by": "manual",
        },
        {
            "venue": "kalshi", "venue_market_id": "TICKER", "venue_market_url": None,
            "side_semantics": "direct", "confidence_score": 1.0, "matched_by": "manual",
        },
    ]
    with patch(
        "app.repositories.canonical_event_repository.fetch",
        new=AsyncMock(return_value=fake_rows),
    ):
        result = await repo.get_mappings("550e8400-e29b-41d4-a716-446655440000")
    assert len(result) == 2
    assert result[0]["venue"] == "polymarket"
    assert result[1]["venue"] == "kalshi"


@pytest.mark.asyncio
async def test_find_paired_markets_no_category(repo):
    fake_pairs = [
        {
            "canonical_event_id": "550e8400-e29b-41d4-a716-446655440000",
            "slug": "fed-march-2026",
            "title": "Fed March 2026",
            "category": "economics",
            "venue_a_market_id": "0xabc",
            "venue_a_side": "direct",
            "venue_b_market_id": "FED-MAR-2026-CUT",
            "venue_b_side": "direct",
            "is_inverse": False,
        }
    ]
    with patch(
        "app.repositories.canonical_event_repository.fetch",
        new=AsyncMock(return_value=fake_pairs),
    ) as mock_fetch:
        result = await repo.find_paired_markets("polymarket", "kalshi")
    assert len(result) == 1
    assert result[0]["is_inverse"] is False
    # Verify only 2 params (no category filter)
    call_args = mock_fetch.call_args.args
    assert len(call_args) == 3  # SQL + 2 params
    assert call_args[1] == "polymarket"
    assert call_args[2] == "kalshi"


@pytest.mark.asyncio
async def test_find_paired_markets_with_category(repo):
    with patch(
        "app.repositories.canonical_event_repository.fetch",
        new=AsyncMock(return_value=[]),
    ) as mock_fetch:
        await repo.find_paired_markets("polymarket", "kalshi", category="sports")
    call_args = mock_fetch.call_args.args
    assert len(call_args) == 4  # SQL + 3 params
    assert call_args[3] == "sports"
    # SQL should contain category filter
    assert "ce.category = $3" in call_args[0]


@pytest.mark.asyncio
async def test_find_paired_markets_inverse_flag(repo):
    fake_pairs = [
        {
            "canonical_event_id": "abc", "slug": "test", "title": "T", "category": "politics",
            "venue_a_market_id": "0xpoly", "venue_a_side": "direct",
            "venue_b_market_id": "KALSHI", "venue_b_side": "inverse",
            "is_inverse": True,
        }
    ]
    with patch(
        "app.repositories.canonical_event_repository.fetch",
        new=AsyncMock(return_value=fake_pairs),
    ):
        result = await repo.find_paired_markets("polymarket", "kalshi")
    assert result[0]["is_inverse"] is True


@pytest.mark.asyncio
async def test_expire_mapping(repo):
    with patch(
        "app.repositories.canonical_event_repository.execute",
        new=AsyncMock(return_value="UPDATE 1"),
    ) as mock_ex:
        await repo.expire_mapping(
            canonical_event_id="550e8400-e29b-41d4-a716-446655440000",
            venue="polymarket",
            venue_market_id="0xabc",
        )
    args = mock_ex.call_args.args
    assert "expired_at = now()" in args[0]
    assert args[1] == "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.asyncio
async def test_resolve_event(repo):
    with patch(
        "app.repositories.canonical_event_repository.execute",
        new=AsyncMock(return_value="UPDATE 1"),
    ) as mock_ex:
        await repo.resolve_event(slug="trump-2024-president", outcome="YES")
    args = mock_ex.call_args.args
    assert "status = 'resolved'" in args[0]
    assert args[1] == "trump-2024-president"
    assert args[2] == "YES"
