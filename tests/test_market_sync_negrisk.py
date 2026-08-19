"""negRisk-поля попадают в event_record из market_sync.process_event (audit: -30 мин задержки D3).

market_sync (*/3 мин) уже скачивает те же Gamma-события, что и negrisk_sync (раньше */30 мин).
Парсинг делегирован app.jobs.negrisk_sync._extract_metadata — единственный источник правды,
чтобы market-sync и weekly negrisk-репарация не разъехались.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.market_sync import process_event


@pytest.mark.asyncio
async def test_process_event_writes_neg_risk_true_with_other_market():
    """negRisk=true + catch-all market → neg_risk=True, neg_risk_other=True, n_yes_winners=1."""
    event_data = {
        "id": "evt-negrisk-1",
        "title": "Who will win?",
        "slug": "who-will-win",
        "description": "Test",
        "volume": 1000,
        "volume24hr": 100,
        "liquidity": 500,
        "startDate": None,
        "endDate": None,
        "active": True,
        "closed": False,
        "tags": [],
        "competitive": 0.5,
        "negRisk": True,
        "markets": [
            {"groupItemTitle": "Trump"},
            {"groupItemTitle": "Biden"},
            {"groupItemTitle": "Other"},
        ],
    }

    captured_values = {}

    async def capture_upsert(self, event_data):
        captured_values.update(event_data)
        return {"id": "event-uuid", "polymarket_id": "evt-negrisk-1"}

    with patch(
        "app.repositories.event_repository.EventRepository.upsert_event",
        capture_upsert,
    ):
        await process_event(event_data)

    assert captured_values["neg_risk"] is True
    assert captured_values["neg_risk_other"] is True
    assert captured_values["n_yes_winners"] == 1


@pytest.mark.asyncio
async def test_process_event_writes_neg_risk_false_by_default():
    """Событие без negRisk-поля → neg_risk=False, neg_risk_other=False, n_yes_winners=1 (defaults)."""
    event_data = {
        "id": "evt-no-negrisk",
        "title": "Simple binary market",
        "slug": "simple-binary",
        "description": "Test",
        "volume": 1000,
        "volume24hr": 100,
        "liquidity": 500,
        "startDate": None,
        "endDate": None,
        "active": True,
        "closed": False,
        "tags": [],
        "competitive": 0.5,
        "markets": [],
    }

    captured_values = {}

    async def capture_upsert(self, event_data):
        captured_values.update(event_data)
        return {"id": "event-uuid", "polymarket_id": "evt-no-negrisk"}

    with patch(
        "app.repositories.event_repository.EventRepository.upsert_event",
        capture_upsert,
    ):
        await process_event(event_data)

    assert captured_values["neg_risk"] is False
    assert captured_values["neg_risk_other"] is False
    assert captured_values["n_yes_winners"] == 1


@pytest.mark.asyncio
async def test_process_event_missing_id_never_calls_negrisk_extractor():
    """Событие без id падает в error-ветку раньше парсинга — regression guard на порядок веток."""
    event_data = {"title": "No id here"}

    with patch(
        "app.jobs.market_sync._extract_negrisk_metadata",
        new=AsyncMock(),
    ) as mock_extract:
        result = await process_event(event_data)

    assert result == {"status": "error", "reason": "missing_event_id"}
    mock_extract.assert_not_called()
