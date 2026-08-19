"""Tests for event-based market sync."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_run_market_sync_uses_events_api():
    """run_market_sync fetches events and processes nested markets."""
    mock_event = {
        "id": "event-1",
        "title": "Test Event",
        "slug": "test-event",
        "description": "A test",
        "volume": 100000,
        "volume24hr": 5000,
        "liquidity": 20000,
        "startDate": None,
        "endDate": "2026-12-31T23:59:59Z",
        "active": True,
        "closed": False,
        "tags": [{"label": "Politics"}],
        "competitive": 0.5,
        "markets": [
            {
                "conditionId": "mkt-1",
                "question": "Will X happen?",
                "description": "",
                "outcomePrices": "[0.65, 0.35]",
                "volume24hr": 1000,
                "endDate": "2026-12-31T23:59:59Z",
                "closed": False,
                "icon": None,
                "clobTokenIds": [],
                "tokens": [],
            }
        ],
    }

    mock_market_row = {"id": "uuid-1"}
    mock_event_row = {"id": "event-uuid-1", "polymarket_id": "event-1"}

    with (
        patch("app.jobs.market_sync.get_polymarket_service") as mock_poly,
        patch("app.jobs.market_sync.fetchrow", new_callable=AsyncMock, return_value=mock_market_row),
        patch("app.repositories.event_repository.fetchrow", new_callable=AsyncMock, return_value=mock_event_row),
        patch("app.repositories.event_repository.record_to_dict", return_value=mock_event_row),
        patch("app.repositories.event_repository.execute", new_callable=AsyncMock, return_value="UPDATE 0"),
    ):
        mock_service = AsyncMock()
        mock_service.fetch_active_events = AsyncMock(
            side_effect=[[mock_event], []]  # Page 1 returns 1 event, page 2 empty → stop
        )
        mock_service.fetch_closed_events = AsyncMock(return_value=[])
        mock_poly.return_value = mock_service

        from app.jobs.market_sync import run_market_sync

        result = await run_market_sync()

    assert result["events_synced"] >= 1
    assert result["fetch_failed"] is False


@pytest.mark.asyncio
async def test_run_market_sync_empty_events():
    """run_market_sync returns fetch_failed when no events returned."""
    with (
        patch("app.jobs.market_sync.get_polymarket_service") as mock_poly,
    ):
        mock_service = AsyncMock()
        mock_service.fetch_active_events = AsyncMock(return_value=[])
        mock_poly.return_value = mock_service

        from app.jobs.market_sync import run_market_sync

        result = await run_market_sync()

    assert result["events_synced"] == 0
    assert result["fetch_failed"] is True


def test_parse_iso_datetime_with_z_suffix():
    """_parse_iso_datetime конвертирует строку с Z-суффиксом в datetime."""
    from app.jobs.market_sync import _parse_iso_datetime

    result = _parse_iso_datetime("2026-12-31T23:59:59Z")
    assert isinstance(result, datetime)
    assert result == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)


def test_parse_iso_datetime_with_offset():
    """_parse_iso_datetime конвертирует строку с +00:00 offset."""
    from app.jobs.market_sync import _parse_iso_datetime

    result = _parse_iso_datetime("2025-08-07T20:45:08.401678+00:00")
    assert isinstance(result, datetime)
    assert result.year == 2025
    assert result.month == 8


def test_parse_iso_datetime_none():
    """_parse_iso_datetime возвращает None для None."""
    from app.jobs.market_sync import _parse_iso_datetime

    assert _parse_iso_datetime(None) is None


def test_parse_iso_datetime_already_datetime():
    """_parse_iso_datetime пропускает datetime объекты без изменений."""
    from app.jobs.market_sync import _parse_iso_datetime

    dt = datetime(2026, 1, 1, tzinfo=UTC)
    assert _parse_iso_datetime(dt) is dt


@pytest.mark.asyncio
async def test_process_event_passes_datetime_to_asyncpg():
    """process_event должен передавать datetime объекты, не строки, в asyncpg."""
    from app.jobs.market_sync import process_event

    captured_values = {}

    async def capture_upsert(self, event_data):
        captured_values.update(event_data)
        return {"id": "event-uuid", "polymarket_id": "12345"}

    event_data = {
        "id": "12345",
        "title": "Test Event",
        "slug": "test-event",
        "description": "Test",
        "volume": 1000,
        "volume24hr": 100,
        "liquidity": 500,
        "startDate": "2025-08-07T20:45:08.401678Z",
        "endDate": "2026-12-31T23:59:59Z",
        "active": True,
        "closed": False,
        "tags": [],
        "competitive": 0.5,
        "markets": [],
    }

    with patch(
        "app.repositories.event_repository.EventRepository.upsert_event",
        capture_upsert,
    ):
        await process_event(event_data)

    # start_date и end_date должны быть datetime, не строками
    assert isinstance(captured_values["start_date"], datetime), (
        f"start_date: ожидался datetime, получен {type(captured_values['start_date'])}"
    )
    assert isinstance(captured_values["end_date"], datetime), (
        f"end_date: ожидался datetime, получен {type(captured_values['end_date'])}"
    )
    assert isinstance(captured_values["updated_at"], datetime), (
        f"updated_at: ожидался datetime, получен {type(captured_values['updated_at'])}"
    )


@pytest.mark.asyncio
async def test_process_market_passes_datetime_end_date():
    """process_market должен передавать datetime для end_date в asyncpg."""
    captured_args = {}

    async def capture_fetchrow(query, *args):
        if "INSERT INTO markets" in query:
            captured_args["args"] = args
            return {"id": "market-uuid"}
        return None

    market_data = {
        "conditionId": "cond-1",
        "question": "Will it rain?",
        "description": "",
        "outcomePrices": "[0.6, 0.4]",
        "volume24hr": 500,
        "endDate": "2026-12-31T23:59:59Z",
        "closed": False,
        "icon": None,
        "slug": "will-it-rain",
        "clobTokenIds": [],
        "tokens": [],
    }

    with patch("app.jobs.market_sync.fetchrow", side_effect=capture_fetchrow):
        from app.jobs.market_sync import process_market

        await process_market(market_data)

    # Без event_id используется ветка с 15 params: $9 = end_date (index 8)
    end_date_arg = captured_args["args"][8]  # 0-indexed, $9 is index 8
    assert isinstance(end_date_arg, datetime), f"end_date: ожидался datetime, получен {type(end_date_arg)}"


@pytest.mark.asyncio
async def test_process_event_syncs_all_markets():
    """process_event синхронизирует все вложенные рынки события."""
    mock_event_row = {"id": "event-uuid", "polymarket_id": "evt-1"}
    mock_market_row = {"id": "market-uuid"}

    event_data = {
        "id": "evt-1",
        "title": "Multi-market event",
        "slug": "multi",
        "description": "Test",
        "volume": 1000,
        "volume24hr": 100,
        "liquidity": 500,
        "startDate": None,
        "endDate": None,
        "active": True,
        "closed": False,
        "tags": [],
        "competitive": 0,
        "markets": [
            {
                "conditionId": f"mkt-{i}",
                "question": f"Question {i}?",
                "description": "",
                "outcomePrices": "[0.5, 0.5]",
                "volume24hr": 100,
                "endDate": None,
                "closed": False,
                "icon": None,
                "slug": f"q-{i}",
                "clobTokenIds": [],
                "tokens": [],
            }
            for i in range(5)
        ],
    }

    with (
        patch(
            "app.repositories.event_repository.EventRepository.upsert_event",
            AsyncMock(return_value=mock_event_row),
        ),
        patch(
            "app.jobs.market_sync.fetchrow",
            AsyncMock(return_value=mock_market_row),
        ),
    ):
        from app.jobs.market_sync import process_event

        result = await process_event(event_data)

    assert result["markets_synced"] == 5
    assert result["markets_total"] == 5


@pytest.mark.asyncio
async def test_process_market_returns_synced_status():
    """process_market возвращает status=synced при успешном upsert."""

    async def mock_fetchrow(query, *args):
        if "INSERT INTO markets" in query:
            return {"id": "market-uuid"}
        return None

    market_data = {
        "conditionId": "cond-1",
        "question": "Test question?",
        "description": "Test description",
        "outcomePrices": "[0.5, 0.5]",
        "volume24hr": 100,
        "endDate": None,
        "closed": False,
        "icon": None,
        "slug": "test",
        "clobTokenIds": [],
        "tokens": [],
    }

    with patch("app.jobs.market_sync.fetchrow", side_effect=mock_fetchrow):
        from app.jobs.market_sync import process_market

        result = await process_market(market_data)

    assert result["status"] == "synced"
    assert result["id"] == "market-uuid"


def test_infer_category_resolve_does_not_match_sol():
    """Слово `resolve` не должно матчить keyword `sol` (substring trap)."""
    from app.jobs.market_sync import infer_category

    q = "Will the highest temperature in Mexico City be above 25C?"
    desc = "This market will resolve based on temperature data."
    # 'sol' inside 'resolve' must NOT classify as Crypto
    assert infer_category(q, desc) != "Crypto"


def test_infer_category_real_solana_matches_crypto():
    """Реальное упоминание Solana должно матчить Crypto."""
    from app.jobs.market_sync import infer_category

    assert infer_category("Will Solana reach $300 in 2026?", "") == "Crypto"


def test_infer_category_btc_with_word_boundary():
    """`BTC` matches с word boundary, но не как substring (e.g. `aBTCb`)."""
    from app.jobs.market_sync import infer_category

    assert infer_category("Will BTC hit $100k?", "") == "Crypto"


def test_infer_category_lawsuit_does_not_match_law():
    """`lawsuit` не должно матчить Politics keyword `law`."""
    from app.jobs.market_sync import infer_category

    q = "Will the lawsuit against Apple settle in 2026?"
    desc = ""
    assert infer_category(q, desc) != "Politics"


def test_infer_category_warmth_does_not_match_war():
    """`warmth/towards` не должны матчить keyword `war` (substring trap)."""
    from app.jobs.market_sync import infer_category

    # text with politics keyword + warmth — `war` inside `warmth` must NOT boost world-economy
    # so that politics keyword wins
    result = infer_category("Will the president sign the bill amid warmth?", "")
    assert result == "Politics", f"president should win, got {result}"


def test_infer_category_from_tags_skips_operational_labels():
    """Operational/geo tags (Recurring, Mexico) не должны определять категорию."""
    from app.jobs.market_sync import infer_category_from_tags

    tags = [
        {"label": "Recurring"},
        {"label": "Hide From New"},
        {"label": "Mexico"},
        {"label": "Daily Temperature"},
    ]
    # Should fall back to keyword inference (which won't find Crypto on weather)
    result = infer_category_from_tags(tags, "Highest temperature in Mexico City?", "")
    assert result != "Crypto"


def test_infer_category_from_tags_weather_maps_to_world_economy():
    """Tag `Weather` маппится в World & Economy через alias."""
    from app.jobs.market_sync import infer_category_from_tags

    tags = [{"label": "Weather"}, {"label": "Recurring"}]
    assert infer_category_from_tags(tags, "Will it rain?", "") == "World & Economy"


def test_infer_category_from_tags_esports_maps_to_sports():
    """Tag `Esports` маппится в Sports через alias."""
    from app.jobs.market_sync import infer_category_from_tags

    tags = [{"label": "Esports"}]
    assert infer_category_from_tags(tags, "Will Team A win?", "") == "Sports"


def test_infer_category_from_tags_real_crypto_event():
    """Real Crypto event: первый distinguishing tag — Crypto/Bitcoin."""
    from app.jobs.market_sync import infer_category_from_tags

    tags = [
        {"label": "Finance"},  # alias to World & Economy
        {"label": "Business"},  # not in alias
        {"label": "Crypto"},  # alias to Crypto
    ]
    # First match wins — Finance maps first
    assert infer_category_from_tags(tags, "BTC price?", "") == "World & Economy"


def test_infer_category_from_tags_priorityordered_for_crypto_first():
    """Если Crypto-tag идёт первым, он выигрывает."""
    from app.jobs.market_sync import infer_category_from_tags

    tags = [
        {"label": "Crypto"},
        {"label": "Finance"},
    ]
    assert infer_category_from_tags(tags, "BTC price?", "") == "Crypto"


def test_infer_category_expanded_aliases_movies():
    """`Movies` тэг должен мапиться в Culture."""
    from app.jobs.market_sync import infer_category_from_tags

    assert infer_category_from_tags([{"label": "Movies"}], "Will X win?", "") == "Culture"


def test_infer_category_expanded_aliases_reality_tv():
    """`Reality TV` (Bachelorette etc) → Culture."""
    from app.jobs.market_sync import infer_category_from_tags

    assert infer_category_from_tags([{"label": "Reality TV"}], "Will X win?", "") == "Culture"


def test_infer_category_expanded_aliases_iran():
    """Geopolitical entity tags → World & Economy."""
    from app.jobs.market_sync import infer_category_from_tags

    assert infer_category_from_tags([{"label": "Iran"}], "Iran sanctions?", "") == "World & Economy"


def test_infer_category_expanded_aliases_stocks():
    """`Stocks` / `Equities` → World & Economy."""
    from app.jobs.market_sync import infer_category_from_tags

    assert infer_category_from_tags([{"label": "Stocks"}], "S&P 500?", "") == "World & Economy"


def test_infer_category_expanded_aliases_csgo():
    """eSports games → Sports."""
    from app.jobs.market_sync import infer_category_from_tags

    assert infer_category_from_tags([{"label": "counter strike 2"}], "Will Team A?", "") == "Sports"


@pytest.mark.asyncio
async def test_process_event_passes_event_category_to_markets():
    """Markets должны получать _event_category из event tags."""
    from app.jobs.market_sync import process_event

    captured_market_data: list[dict] = []

    async def capture_market(market_data):
        captured_market_data.append(dict(market_data))
        return {"status": "synced", "id": "mkt-uuid"}

    event_data = {
        "id": "evt-1",
        "title": "Bachelorette Season 22",
        "slug": "bachelorette",
        "description": "TV show",
        "volume": 1000,
        "volume24hr": 100,
        "liquidity": 500,
        "startDate": None,
        "endDate": None,
        "active": True,
        "closed": False,
        "tags": [{"label": "Reality TV"}, {"label": "Celebrities"}],
        "competitive": 0,
        "markets": [
            {
                "conditionId": "mkt-1",
                "question": "Will Richard win?",
                "description": "This market will resolve to the winner.",
                "outcomePrices": "[0.5, 0.5]",
                "volume24hr": 100,
                "endDate": None,
                "closed": False,
                "icon": None,
                "slug": "q-1",
                "clobTokenIds": [],
                "tokens": [],
            }
        ],
    }

    with (
        patch(
            "app.repositories.event_repository.EventRepository.upsert_event",
            AsyncMock(return_value={"id": "evt-uuid", "polymarket_id": "evt-1"}),
        ),
        patch("app.jobs.market_sync.process_market", new=capture_market),
    ):
        await process_event(event_data)

    assert len(captured_market_data) == 1
    # Reality TV → Culture (via alias)
    # Without inheritance, "resolve" would have triggered Crypto via 'sol' substring
    # (now fixed by word-boundary regex), but explicit inheritance is the durable fix.
    assert captured_market_data[0]["_event_category"] == "Culture"
