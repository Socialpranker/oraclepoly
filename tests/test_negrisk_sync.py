"""Тесты NegRisk sync job."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.negrisk_sync import _extract_metadata, _is_other_market, run_negrisk_sync


def test_is_other_market_by_group_title():
    assert _is_other_market({"groupItemTitle": "Other"}) is True
    assert _is_other_market({"groupItemTitle": "OTHER"}) is True
    assert _is_other_market({"groupItemTitle": "Trump"}) is False


def test_is_other_market_by_question():
    assert _is_other_market({"question": "None of the above"}) is True
    assert _is_other_market({"question": "Will some Other person win?"}) is True
    assert _is_other_market({"question": "Will Trump win?"}) is False


def test_is_other_market_by_slug():
    assert _is_other_market({"slug": "will-none-of-the-above-win"}) is True
    assert _is_other_market({"slug": "trump-2024"}) is False


def test_extract_metadata_neg_risk_event():
    event = {
        "id": "evt-123",
        "negRisk": True,
        "markets": [
            {"groupItemTitle": "Trump"},
            {"groupItemTitle": "Biden"},
            {"groupItemTitle": "Other"},
        ],
    }
    result = _extract_metadata(event)
    assert result == {
        "polymarket_id": "evt-123",
        "neg_risk": True,
        "neg_risk_other": True,
        "n_yes_winners": 1,
    }


def test_extract_metadata_neg_risk_no_catch_all():
    event = {
        "id": "evt-456",
        "negRisk": True,
        "markets": [
            {"groupItemTitle": "Trump"},
            {"groupItemTitle": "Biden"},
        ],
    }
    result = _extract_metadata(event)
    assert result["neg_risk"] is True
    assert result["neg_risk_other"] is False
    assert result["n_yes_winners"] == 1


def test_extract_metadata_non_neg_risk():
    event = {
        "id": "evt-789",
        "negRisk": False,
        "markets": [{"groupItemTitle": "Other"}],  # игнорируем когда не neg_risk
    }
    result = _extract_metadata(event)
    assert result == {
        "polymarket_id": "evt-789",
        "neg_risk": False,
        "neg_risk_other": False,
        "n_yes_winners": 1,
    }


def test_extract_metadata_no_id():
    assert _extract_metadata({"negRisk": True}) is None


def test_extract_metadata_handles_missing_negrisk():
    event = {"id": "evt-x", "markets": []}
    result = _extract_metadata(event)
    assert result["neg_risk"] is False


@pytest.mark.asyncio
async def test_run_negrisk_sync_pagination_and_persist():
    """Полный happy-path: 2 страницы, batch UPDATE, count check."""
    page1 = [
        {
            "id": f"evt-{i}",
            "negRisk": (i % 2 == 0),
            "markets": [{"groupItemTitle": "A"}, {"groupItemTitle": "B"}],
        }
        for i in range(100)
    ]
    page2 = [
        {
            "id": "evt-100",
            "negRisk": True,
            "markets": [{"groupItemTitle": "Other"}, {"groupItemTitle": "X"}],
        }
    ]

    with (
        patch("app.jobs.negrisk_sync.get_polymarket_service") as mock_poly,
        patch("app.jobs.negrisk_sync.execute", new_callable=AsyncMock) as mock_execute,
        patch("app.jobs.negrisk_sync.fetch", new_callable=AsyncMock) as mock_fetch,
    ):
        mock_service = AsyncMock()
        mock_service.fetch_active_events = AsyncMock(side_effect=[page1, page2, []])
        mock_poly.return_value = mock_service
        mock_fetch.return_value = [{"n": 51}]

        result = await run_negrisk_sync()

    assert result["fetched"] == 101
    assert result["extracted"] == 101
    assert result["neg_risk_active"] == 51
    mock_execute.assert_called_once()
    args = mock_execute.call_args[0]
    assert len(args[1]) == 101
    assert "UPDATE events" in args[0]


@pytest.mark.asyncio
async def test_run_negrisk_sync_empty():
    with patch("app.jobs.negrisk_sync.get_polymarket_service") as mock_poly:
        mock_service = AsyncMock()
        mock_service.fetch_active_events = AsyncMock(return_value=[])
        mock_poly.return_value = mock_service

        result = await run_negrisk_sync()

    assert result == {"fetched": 0, "extracted": 0, "neg_risk_active": 0}


@pytest.mark.asyncio
async def test_run_negrisk_sync_partial_page_stops_pagination():
    """Если страница вернула < PAGE_LIMIT — пагинация останавливается."""
    short_page = [
        {"id": f"evt-{i}", "negRisk": False, "markets": []} for i in range(10)
    ]
    with (
        patch("app.jobs.negrisk_sync.get_polymarket_service") as mock_poly,
        patch("app.jobs.negrisk_sync.execute", new_callable=AsyncMock),
        patch("app.jobs.negrisk_sync.fetch", new_callable=AsyncMock, return_value=[{"n": 0}]),
    ):
        mock_service = AsyncMock()
        mock_service.fetch_active_events = AsyncMock(return_value=short_page)
        mock_poly.return_value = mock_service

        result = await run_negrisk_sync()

    assert result["fetched"] == 10
    assert mock_service.fetch_active_events.call_count == 1
