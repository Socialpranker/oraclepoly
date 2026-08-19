"""
Polymarket Trade Poller Tests

Tests for the PolymarketPollerService service:
- fetch_recent_trades (mock httpx response)
- filter_whale_trades with mix of small and large trades
- normalize_trade field mapping
- resolve_condition_to_market (mock DB, found and not-found cases)
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# --- Sample trade data ---

SAMPLE_TRADE_WHALE = {
    "proxyWallet": "0x21C24A79A0C2F07B2ED34E30D42DFFA3D0B66749",
    "side": "BUY",
    "asset": "44354963465119544295399417785198158803145797243066127494618768418658371112570",
    "conditionId": "0xbe350c1ca78e598b65c397eb393881dd75679f21a8b55d178042329fae8d115d",
    "size": 10000,
    "price": 0.75,
    "timestamp": 1771051025,
    "title": "Will BTC hit $100k by March 2026?",
    "slug": "btc-100k-march-2026",
    "icon": "https://example.com/icon.png",
    "eventSlug": "btc-100k-march-2026",
    "outcome": "Yes",
    "outcomeIndex": 0,
    "name": "WhaleTrader",
    "pseudonym": "Big-Dolphin",
    "bio": "",
    "profileImage": "",
    "profileImageOptimized": "",
    "transactionHash": "0xa010184a682b034c350b5cf24ab8871621083d8cdaa35a1149b2b069ad158ce3",
}

SAMPLE_TRADE_SMALL = {
    "proxyWallet": "0xSmallFish123",
    "side": "SELL",
    "asset": "99999999999999999",
    "conditionId": "0xabc123",
    "size": 10,
    "price": 0.50,
    "timestamp": 1771051000,
    "title": "Some small market",
    "slug": "small-market",
    "icon": "",
    "eventSlug": "small-market",
    "outcome": "No",
    "outcomeIndex": 1,
    "name": "SmallFish",
    "pseudonym": "Tiny-Minnow",
    "bio": "",
    "profileImage": "",
    "profileImageOptimized": "",
    "transactionHash": "0xsmallhash123",
}

SAMPLE_TRADE_EXACT_THRESHOLD = {
    "proxyWallet": "0xExactThreshold",
    "side": "BUY",
    "asset": "1111111",
    "conditionId": "0xthreshold",
    "size": 5000,
    "price": 1.0,
    "timestamp": 1771051050,
    "title": "Exact threshold market",
    "slug": "exact-threshold",
    "icon": "",
    "eventSlug": "exact-threshold",
    "outcome": "Yes",
    "outcomeIndex": 0,
    "name": "ThresholdTrader",
    "pseudonym": "Edge-Case",
    "bio": "",
    "profileImage": "",
    "profileImageOptimized": "",
    "transactionHash": "0xthresholdhash",
}


# =============================================================================
# Test: fetch_recent_trades
# =============================================================================


@pytest.mark.asyncio
async def test_fetch_recent_trades_success():
    """Test successful fetch of trades from Polymarket API."""
    mock_response = MagicMock()
    mock_response.json.return_value = [SAMPLE_TRADE_WHALE, SAMPLE_TRADE_SMALL]
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.polymarket_poller_service.httpx.AsyncClient", return_value=mock_client):
        from app.services.polymarket_poller_service import PolymarketPollerService

        poller = PolymarketPollerService()
        trades = await poller.fetch_recent_trades(limit=100, offset=0)

    assert len(trades) == 2
    assert trades[0]["transactionHash"] == SAMPLE_TRADE_WHALE["transactionHash"]
    mock_client.get.assert_called_once_with(
        "https://data-api.polymarket.com/trades",
        params={"limit": 100, "offset": 0, "filterType": "CASH", "filterAmount": 1000.0},
    )


@pytest.mark.asyncio
async def test_fetch_recent_trades_empty():
    """Test fetch returns empty list when API returns empty."""
    mock_response = MagicMock()
    mock_response.json.return_value = []
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.polymarket_poller_service.httpx.AsyncClient", return_value=mock_client):
        from app.services.polymarket_poller_service import PolymarketPollerService

        poller = PolymarketPollerService()
        trades = await poller.fetch_recent_trades()

    assert trades == []


@pytest.mark.asyncio
async def test_fetch_recent_trades_circuit_breaker():
    """Test that circuit breaker error returns empty list gracefully."""
    import pybreaker

    with patch("app.services.polymarket_poller_service.httpx.AsyncClient") as mock_client_class:
        mock_client_class.side_effect = pybreaker.CircuitBreakerError(pybreaker.CircuitBreaker())

        from app.services.polymarket_poller_service import PolymarketPollerService

        poller = PolymarketPollerService()
        trades = await poller.fetch_recent_trades()

    assert trades == []


@pytest.mark.asyncio
async def test_fetch_recent_trades_http_error():
    """Test that HTTP errors are handled gracefully."""
    import httpx

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Internal Server Error",
        request=MagicMock(),
        response=mock_response,
    )
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.polymarket_poller_service.httpx.AsyncClient", return_value=mock_client):
        # Disable retry for test speed
        with patch("app.services.polymarket_poller_service.http_retry", lambda f: f):
            from app.services.polymarket_poller_service import PolymarketPollerService

            poller = PolymarketPollerService()
            trades = await poller.fetch_recent_trades()

    assert trades == []


# =============================================================================
# Test: filter_whale_trades
# =============================================================================


def test_filter_whale_trades_default_threshold():
    """Test filtering with default $1000 minimum."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    trades = [SAMPLE_TRADE_WHALE, SAMPLE_TRADE_SMALL, SAMPLE_TRADE_EXACT_THRESHOLD]

    result = poller.filter_whale_trades(trades)

    # Whale: 10000 * 0.75 = $7500 >= $1000 -> included
    # Small: 10 * 0.50 = $5 < $1000 -> excluded
    # Exact: 5000 * 1.0 = $5000 >= $1000 -> included
    assert len(result) == 2
    assert result[0]["transactionHash"] == SAMPLE_TRADE_WHALE["transactionHash"]
    assert result[1]["transactionHash"] == SAMPLE_TRADE_EXACT_THRESHOLD["transactionHash"]


def test_filter_whale_trades_custom_threshold():
    """Test filtering with custom threshold."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    trades = [SAMPLE_TRADE_WHALE, SAMPLE_TRADE_SMALL]

    # Lower threshold: even small fish pass
    result = poller.filter_whale_trades(trades, min_usd=1)
    assert len(result) == 2

    # Very high threshold: nobody passes
    result = poller.filter_whale_trades(trades, min_usd=100000)
    assert len(result) == 0


def test_filter_whale_trades_empty_list():
    """Test filtering empty trade list."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    result = poller.filter_whale_trades([])
    assert result == []


def test_filter_whale_trades_usd_calculation():
    """Test that USD value is correctly computed as size * price."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()

    trade_just_below = {
        **SAMPLE_TRADE_WHALE,
        "size": 6666,
        "price": 0.75,
    }
    # 6666 * 0.75 = 4999.50 < 5000
    result = poller.filter_whale_trades([trade_just_below], min_usd=5000)
    assert len(result) == 0

    trade_just_above = {
        **SAMPLE_TRADE_WHALE,
        "size": 6667,
        "price": 0.75,
    }
    # 6667 * 0.75 = 5000.25 >= 5000
    result = poller.filter_whale_trades([trade_just_above], min_usd=5000)
    assert len(result) == 1


# =============================================================================
# Test: normalize_trade
# =============================================================================


def test_normalize_trade_field_mapping():
    """Test that normalize_trade correctly maps all fields."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    result = poller.normalize_trade(SAMPLE_TRADE_WHALE)

    # Check each mapped field
    assert result["tx_hash"] == "0xa010184a682b034c350b5cf24ab8871621083d8cdaa35a1149b2b069ad158ce3"
    assert result["log_index"] == 0
    assert result["from_address"] == "0x21c24a79a0c2f07b2ed34e30d42dffa3d0b66749"  # lowered
    assert result["to_address"] == ""
    assert result["direction"] == "buy"  # BUY -> buy
    assert result["asset"] == "USDC"
    assert result["raw_contract_address"] is None
    assert result["block_number"] == 0
    assert result["r2_archive_key"] is None
    assert result["market_id"] is None
    assert result["is_whale"] is True
    assert result["source"] == "polymarket_api"
    assert result["condition_id"] == "0xbe350c1ca78e598b65c397eb393881dd75679f21a8b55d178042329fae8d115d"


def test_normalize_trade_amount_calculation():
    """Test that amount is correctly computed as size * price, quantized to 2 decimals."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    result = poller.normalize_trade(SAMPLE_TRADE_WHALE)

    # 10000 * 0.75 = 7500.00
    assert result["amount"] == "7500.00"


def test_normalize_trade_amount_quantization():
    """Test amount quantization for fractional values."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    trade = {
        **SAMPLE_TRADE_WHALE,
        "size": 52,
        "price": 0.8132076923076923,
    }
    result = poller.normalize_trade(trade)

    # 52 * 0.8132076923076923 = 42.2868... -> "42.29"
    assert result["amount"] == "42.29"


def test_normalize_trade_sell_direction():
    """Test SELL side is lowered correctly."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    result = poller.normalize_trade(SAMPLE_TRADE_SMALL)

    assert result["direction"] == "sell"


def test_normalize_trade_timestamp_conversion():
    """Test that unix timestamp is converted to ISO format."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    result = poller.normalize_trade(SAMPLE_TRADE_WHALE)

    # Verify it's a valid ISO format datetime string
    parsed = datetime.fromisoformat(result["block_timestamp"])
    assert parsed.year >= 2024
    # Verify it corresponds to the original unix timestamp
    expected = datetime.fromtimestamp(1771051025, tz=UTC)
    assert result["block_timestamp"] == expected.isoformat()


def test_normalize_trade_proxy_wallet_lowered():
    """Test that proxyWallet is lowered in from_address."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    trade = {**SAMPLE_TRADE_WHALE, "proxyWallet": "0xABCDEF123456"}
    result = poller.normalize_trade(trade)

    assert result["from_address"] == "0xabcdef123456"


def test_normalize_trade_preserves_enrichment_fields():
    """Test that normalize_trade preserves outcome, price, market_title, market_slug."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    result = poller.normalize_trade(SAMPLE_TRADE_WHALE)

    assert result["outcome"] == "Yes"
    assert result["price"] == "0.75"
    assert result["market_title"] == "Will BTC hit $100k by March 2026?"
    assert result["market_slug"] == "btc-100k-march-2026"


def test_normalize_trade_enrichment_fields_missing():
    """Test that missing enrichment fields default to empty string."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    trade_minimal = {
        "proxyWallet": "0xABC",
        "side": "BUY",
        "size": 100,
        "price": 0.5,
        "timestamp": 1771051025,
        "transactionHash": "0xhash",
        "conditionId": "0xcond",
    }
    result = poller.normalize_trade(trade_minimal)

    assert result["outcome"] == ""
    assert result["price"] == "0.50"
    assert result["market_title"] == ""
    assert result["market_slug"] == ""


# =============================================================================
# Test: resolve_condition_to_market
# =============================================================================


@pytest.mark.asyncio
async def test_resolve_condition_to_market_found():
    """Test resolving a conditionId to a market UUID when found in DB."""
    with patch(
        "app.services.polymarket_poller_service.fetchval", new_callable=AsyncMock, return_value="market-uuid-123"
    ):
        from app.services.polymarket_poller_service import PolymarketPollerService

        poller = PolymarketPollerService()
        result = await poller.resolve_condition_to_market(
            "0xbe350c1ca78e598b65c397eb393881dd75679f21a8b55d178042329fae8d115d"
        )

    assert result == "market-uuid-123"


@pytest.mark.asyncio
async def test_resolve_condition_to_market_not_found():
    """Test resolving a conditionId when no market matches."""
    with patch("app.services.polymarket_poller_service.fetchval", new_callable=AsyncMock, return_value=None):
        from app.services.polymarket_poller_service import PolymarketPollerService

        poller = PolymarketPollerService()
        result = await poller.resolve_condition_to_market("0xnonexistent")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_condition_to_market_empty_input():
    """Test resolving with empty condition_id returns None."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    result = await poller.resolve_condition_to_market("")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_condition_to_market_none_input():
    """Test resolving with None condition_id returns None."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    poller = PolymarketPollerService()
    result = await poller.resolve_condition_to_market(None)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_condition_to_market_db_error():
    """Test graceful handling when database query fails."""
    with patch(
        "app.services.polymarket_poller_service.fetchval",
        new_callable=AsyncMock,
        side_effect=Exception("DB connection failed"),
    ):
        from app.services.polymarket_poller_service import PolymarketPollerService

        poller = PolymarketPollerService()
        result = await poller.resolve_condition_to_market("0xsome_condition")

    assert result is None


# =============================================================================
# Test: get_poller_service singleton
# =============================================================================


def test_get_poller_service_singleton():
    """Test that get_poller_service returns a singleton instance."""
    from app.services.polymarket_poller_service import get_poller_service

    poller1 = get_poller_service()
    poller2 = get_poller_service()
    assert poller1 is poller2


# =============================================================================
# Test: fetch_order_books_rest (batch POST /books — WS bootstrap, Fix-0)
# =============================================================================


@pytest.mark.asyncio
async def test_fetch_order_books_rest_success():
    """POST /books с телом [{"token_id": ...}] возвращает список книг."""
    books = [
        {"asset_id": "tok_1", "bids": [{"price": "0.45", "size": "100"}], "asks": [{"price": "0.46", "size": "50"}]},
        {"asset_id": "tok_2", "bids": [], "asks": [{"price": "0.9", "size": "10"}]},
    ]
    mock_response = MagicMock()
    mock_response.json.return_value = books
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.polymarket_poller_service.httpx.AsyncClient", return_value=mock_client):
        from app.services.polymarket_poller_service import PolymarketPollerService

        poller = PolymarketPollerService()
        result = await poller.fetch_order_books_rest(["tok_1", "tok_2"])

    assert result == books
    mock_client.post.assert_called_once_with(
        "https://clob.polymarket.com/books",
        json=[{"token_id": "tok_1"}, {"token_id": "tok_2"}],
    )


@pytest.mark.asyncio
async def test_fetch_order_books_rest_empty_input_no_http():
    """Пустой список токенов — сразу [], без HTTP-запроса."""
    with patch("app.services.polymarket_poller_service.httpx.AsyncClient") as mock_client_class:
        from app.services.polymarket_poller_service import PolymarketPollerService

        poller = PolymarketPollerService()
        result = await poller.fetch_order_books_rest([])

    assert result == []
    mock_client_class.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_order_books_rest_error_returns_empty():
    """Ошибка HTTP — [], исключение не пропагируется (fire-and-forget посев)."""
    mock_client = AsyncMock()
    mock_client.post.side_effect = Exception("boom")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.polymarket_poller_service.httpx.AsyncClient", return_value=mock_client):
        from app.services.polymarket_poller_service import PolymarketPollerService

        poller = PolymarketPollerService()
        result = await poller.fetch_order_books_rest(["tok_1"])

    assert result == []
