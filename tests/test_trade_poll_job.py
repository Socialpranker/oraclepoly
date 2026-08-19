"""
Tests for Trade Poll Cron Job

Tests the run_trade_poll() function that:
1. Fetches recent trades from Polymarket Data API
2. Filters whale trades (>= $5,000 USD)
3. Normalizes trades to event format
4. Resolves condition_id -> market_id
5. Deduplicates against existing transactions
6. Enqueues new events to Redis
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# --- Sample trade data (reused from test_polymarket_poller_service.py) ---

SAMPLE_TRADE_WHALE_1 = {
    "proxyWallet": "0x21C24A79A0C2F07B2ED34E30D42DFFA3D0B66749",
    "side": "BUY",
    "asset": "44354963465119544295399417785198158803145797243066127494618768418658371112570",
    "conditionId": "0xcond_aaa",
    "size": 10000,
    "price": 0.75,
    "timestamp": 1771051025,
    "title": "Will BTC hit $100k?",
    "slug": "btc-100k",
    "icon": "",
    "eventSlug": "btc-100k",
    "outcome": "Yes",
    "outcomeIndex": 0,
    "name": "WhaleTrader1",
    "pseudonym": "Big-Dolphin",
    "bio": "",
    "profileImage": "",
    "profileImageOptimized": "",
    "transactionHash": "0xwhale_hash_1",
}

SAMPLE_TRADE_WHALE_2 = {
    "proxyWallet": "0xANOTHERWHALE",
    "side": "SELL",
    "asset": "9999999999",
    "conditionId": "0xcond_bbb",
    "size": 8000,
    "price": 0.80,
    "timestamp": 1771051050,
    "title": "Will ETH hit $10k?",
    "slug": "eth-10k",
    "icon": "",
    "eventSlug": "eth-10k",
    "outcome": "No",
    "outcomeIndex": 1,
    "name": "WhaleTrader2",
    "pseudonym": "Mega-Whale",
    "bio": "",
    "profileImage": "",
    "profileImageOptimized": "",
    "transactionHash": "0xwhale_hash_2",
}

SAMPLE_TRADE_SMALL = {
    "proxyWallet": "0xSmallFish",
    "side": "BUY",
    "asset": "1111",
    "conditionId": "0xcond_ccc",
    "size": 10,
    "price": 0.50,
    "timestamp": 1771051000,
    "title": "Small market",
    "slug": "small",
    "icon": "",
    "eventSlug": "small",
    "outcome": "Yes",
    "outcomeIndex": 0,
    "name": "SmallFish",
    "pseudonym": "Tiny",
    "bio": "",
    "profileImage": "",
    "profileImageOptimized": "",
    "transactionHash": "0xsmall_hash",
}


# =============================================================================
# Helpers
# =============================================================================


def _make_mock_poller(page1_trades, page2_trades):
    """Create a mock PolymarketPollerService with controlled fetch results."""
    mock_poller = AsyncMock()

    # fetch_trades_since (timestamp cursor) — page1 first, page2 second
    call_count = {"n": 0}

    async def fetch_since_side_effect(after_ts=None, limit=500):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return page1_trades
        elif call_count["n"] == 2:
            return page2_trades
        return []

    mock_poller.fetch_trades_since = AsyncMock(side_effect=fetch_since_side_effect)

    # Use the real filter and normalize from PolymarketPollerService
    from app.services.polymarket_poller_service import PolymarketPollerService

    real_poller = PolymarketPollerService()
    mock_poller.filter_whale_trades = real_poller.filter_whale_trades
    mock_poller.normalize_trade = real_poller.normalize_trade
    mock_poller.resolve_condition_to_market = AsyncMock(return_value=None)

    return mock_poller


def _make_mock_fetch(existing_tx_hashes=None):
    """Create a mock fetch function for dedup queries.

    Returns an AsyncMock that simulates:
        rows = await fetch("SELECT tx_hash FROM transactions WHERE tx_hash = ANY($1)", batch)
    Each row is a dict-like object with "tx_hash" key.
    """
    existing = existing_tx_hashes or []

    # Создаём mock asyncpg.Record для каждого существующего хеша
    records = []
    for h in existing:
        rec = MagicMock()
        rec.__getitem__ = lambda self, key, hash_val=h: hash_val if key == "tx_hash" else None
        rec.get = lambda key, default=None, hash_val=h: hash_val if key == "tx_hash" else default
        records.append(rec)

    async def mock_fetch(query, *args):
        if "tx_hash" in query:
            # Фильтруем по batch (args[0])
            batch = args[0] if args else []
            return [r for r, h in zip(records, existing) if h in batch]
        return []

    return AsyncMock(side_effect=mock_fetch)


# =============================================================================
# Test 1: Happy path — trades fetched, whales found, deduped, sent
# =============================================================================


@pytest.mark.asyncio
async def test_happy_path_fetches_filters_dedupes_sends():
    """Full happy path: fetch -> filter -> normalize -> resolve -> dedup -> enqueue."""
    mock_poller = _make_mock_poller(
        page1_trades=[SAMPLE_TRADE_WHALE_1, SAMPLE_TRADE_SMALL, SAMPLE_TRADE_WHALE_2],
        page2_trades=[],
    )

    # Resolve condition_id -> market_id
    async def resolve_side_effect(condition_id):
        mapping = {
            "0xcond_aaa": "market-uuid-aaa",
            "0xcond_bbb": "market-uuid-bbb",
        }
        return mapping.get(condition_id)

    mock_poller.resolve_condition_to_market = AsyncMock(side_effect=resolve_side_effect)

    mock_fetch = _make_mock_fetch(existing_tx_hashes=[])  # No duplicates
    enqueued = []

    async def mock_enqueue(queue, data):
        enqueued.append((queue, data))
        return True

    with (
        patch("app.jobs.trade_poll.get_poller_service", return_value=mock_poller),
        patch("app.jobs.trade_poll.fetch", mock_fetch),
        patch("app.jobs.trade_poll.enqueue", side_effect=mock_enqueue),
        patch("app.jobs.trade_poll.cache_get", AsyncMock(return_value=None)),
        patch("app.jobs.trade_poll.cache_set", AsyncMock()),
    ):
        from app.jobs.trade_poll import run_trade_poll

        result = await run_trade_poll()

    # 3 trades fetched (all in page1, page2 empty)
    assert result["fetched"] == 3
    # 2 whale trades (whale_1 and whale_2; small is < $5000)
    assert result["whales"] == 2
    # 0 deduped (none existed)
    assert result["deduped"] == 0
    # 2 events sent
    assert result["sent"] == 2

    # Verify enqueue was called 2 times
    assert len(enqueued) == 2


# =============================================================================
# Test 2: Deduplication — some tx_hashes already exist, those are skipped
# =============================================================================


@pytest.mark.asyncio
async def test_deduplication_skips_existing_transactions():
    """Trades with tx_hashes already in DB are skipped."""
    mock_poller = _make_mock_poller(
        page1_trades=[SAMPLE_TRADE_WHALE_1, SAMPLE_TRADE_WHALE_2],
        page2_trades=[],
    )

    # whale_hash_1 already exists in DB
    mock_fetch = _make_mock_fetch(existing_tx_hashes=["0xwhale_hash_1"])
    enqueued = []

    async def mock_enqueue(queue, data):
        enqueued.append((queue, data))
        return True

    with (
        patch("app.jobs.trade_poll.get_poller_service", return_value=mock_poller),
        patch("app.jobs.trade_poll.fetch", mock_fetch),
        patch("app.jobs.trade_poll.enqueue", side_effect=mock_enqueue),
        patch("app.jobs.trade_poll.cache_get", AsyncMock(return_value=None)),
        patch("app.jobs.trade_poll.cache_set", AsyncMock()),
    ):
        from app.jobs.trade_poll import run_trade_poll

        result = await run_trade_poll()

    assert result["fetched"] == 2
    assert result["whales"] == 2
    assert result["deduped"] == 1  # whale_hash_1 was deduped
    assert result["sent"] == 1  # Only whale_hash_2 sent

    # Verify only 1 event enqueued
    assert len(enqueued) == 1
    assert enqueued[0][1]["tx_hash"] == "0xwhale_hash_2"


# =============================================================================
# Test 3: No whales — all trades below threshold, nothing sent
# =============================================================================


@pytest.mark.asyncio
async def test_no_whales_nothing_sent():
    """When all trades are below $5000, no events are enqueued."""
    mock_poller = _make_mock_poller(
        page1_trades=[SAMPLE_TRADE_SMALL],
        page2_trades=[],
    )

    mock_fetch = _make_mock_fetch()
    mock_enqueue = AsyncMock(return_value=True)

    with (
        patch("app.jobs.trade_poll.get_poller_service", return_value=mock_poller),
        patch("app.jobs.trade_poll.fetch", mock_fetch),
        patch("app.jobs.trade_poll.enqueue", mock_enqueue),
    ):
        from app.jobs.trade_poll import run_trade_poll

        result = await run_trade_poll()

    assert result["fetched"] == 1
    assert result["whales"] == 0
    assert result["deduped"] == 0
    assert result["sent"] == 0

    # enqueue should NOT be called
    mock_enqueue.assert_not_called()


# =============================================================================
# Test 4: Empty fetch — API returns no trades
# =============================================================================


@pytest.mark.asyncio
async def test_empty_fetch_returns_zeros():
    """When API returns no trades, result is all zeros."""
    mock_poller = _make_mock_poller(
        page1_trades=[],
        page2_trades=[],
    )

    mock_fetch = _make_mock_fetch()
    mock_enqueue = AsyncMock(return_value=True)

    with (
        patch("app.jobs.trade_poll.get_poller_service", return_value=mock_poller),
        patch("app.jobs.trade_poll.fetch", mock_fetch),
        patch("app.jobs.trade_poll.enqueue", mock_enqueue),
    ):
        from app.jobs.trade_poll import run_trade_poll

        result = await run_trade_poll()

    assert result["fetched"] == 0
    assert result["whales"] == 0
    assert result["deduped"] == 0
    assert result["sent"] == 0

    mock_enqueue.assert_not_called()


# =============================================================================
# Test 5: Market resolution — condition_id -> market_id set on events
# =============================================================================


@pytest.mark.asyncio
async def test_market_resolution_sets_market_id():
    """Verify condition_id is resolved to market_id and set on events."""
    mock_poller = _make_mock_poller(
        page1_trades=[SAMPLE_TRADE_WHALE_1, SAMPLE_TRADE_WHALE_2],
        page2_trades=[],
    )

    # Only cond_aaa resolves, cond_bbb does not
    async def resolve_side_effect(condition_id):
        if condition_id == "0xcond_aaa":
            return "market-uuid-aaa"
        return None

    mock_poller.resolve_condition_to_market = AsyncMock(side_effect=resolve_side_effect)

    mock_fetch = _make_mock_fetch(existing_tx_hashes=[])
    enqueued = []

    async def mock_enqueue(queue, data):
        enqueued.append((queue, data))
        return True

    with (
        patch("app.jobs.trade_poll.get_poller_service", return_value=mock_poller),
        patch("app.jobs.trade_poll.fetch", mock_fetch),
        patch("app.jobs.trade_poll.enqueue", side_effect=mock_enqueue),
        patch("app.jobs.trade_poll.cache_get", AsyncMock(return_value=None)),
        patch("app.jobs.trade_poll.cache_set", AsyncMock()),
    ):
        from app.jobs.trade_poll import run_trade_poll

        result = await run_trade_poll()

    assert result["sent"] == 2

    # Check that market_id was set on each event correctly
    events_by_hash = {d["tx_hash"]: d for _, d in enqueued}

    assert events_by_hash["0xwhale_hash_1"]["market_id"] == "market-uuid-aaa"
    assert events_by_hash["0xwhale_hash_2"]["market_id"] is None  # Not resolved


# =============================================================================
# Test 6: All duplicates — everything already in DB
# =============================================================================


@pytest.mark.asyncio
async def test_all_duplicates_nothing_sent():
    """When all whale tx_hashes already exist, nothing is enqueued."""
    mock_poller = _make_mock_poller(
        page1_trades=[SAMPLE_TRADE_WHALE_1, SAMPLE_TRADE_WHALE_2],
        page2_trades=[],
    )

    mock_fetch = _make_mock_fetch(existing_tx_hashes=["0xwhale_hash_1", "0xwhale_hash_2"])
    mock_enqueue = AsyncMock(return_value=True)

    with (
        patch("app.jobs.trade_poll.get_poller_service", return_value=mock_poller),
        patch("app.jobs.trade_poll.fetch", mock_fetch),
        patch("app.jobs.trade_poll.enqueue", mock_enqueue),
    ):
        from app.jobs.trade_poll import run_trade_poll

        result = await run_trade_poll()

    assert result["fetched"] == 2
    assert result["whales"] == 2
    assert result["deduped"] == 2
    assert result["sent"] == 0

    mock_enqueue.assert_not_called()


# =============================================================================
# Test 7: Verify event format matches oracle/transaction.received
# =============================================================================


@pytest.mark.asyncio
async def test_event_format_matches_expected_schema():
    """Verify enqueued events have proper structure."""
    mock_poller = _make_mock_poller(
        page1_trades=[SAMPLE_TRADE_WHALE_1],
        page2_trades=[],
    )

    mock_fetch = _make_mock_fetch(existing_tx_hashes=[])
    enqueued = []

    async def mock_enqueue(queue, data):
        enqueued.append((queue, data))
        return True

    with (
        patch("app.jobs.trade_poll.get_poller_service", return_value=mock_poller),
        patch("app.jobs.trade_poll.fetch", mock_fetch),
        patch("app.jobs.trade_poll.enqueue", side_effect=mock_enqueue),
        patch("app.jobs.trade_poll.cache_get", AsyncMock(return_value=None)),
        patch("app.jobs.trade_poll.cache_set", AsyncMock()),
    ):
        from app.jobs.trade_poll import run_trade_poll

        await run_trade_poll()

    assert len(enqueued) == 1
    queue, data = enqueued[0]

    # Verify queue name
    assert queue == "oracle:queue:transactions"

    # Verify data has all required fields for the transaction processor
    assert "tx_hash" in data
    assert "from_address" in data
    assert "amount" in data
    assert "direction" in data
    assert "is_whale" in data
    assert data["is_whale"] is True
    assert data["source"] == "polymarket_api"
