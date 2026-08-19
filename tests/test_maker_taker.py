"""Тесты Maker/Taker split."""


def test_normalize_trade_includes_maker_taker():
    """normalize_trade должен включать maker_address и taker_address."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    service = PolymarketPollerService.__new__(PolymarketPollerService)

    raw_trade = {
        "transactionHash": "0xabc",
        "proxyWallet": "0xwallet",
        "size": "1000",
        "price": "0.65",
        "side": "BUY",
        "timestamp": 1710000000,
        "conditionId": "0xcond",
        "outcome": "YES",
        "title": "Test market",
        "slug": "test-market",
        "maker_address": "0xmaker123",
        "taker_address": "0xtaker456",
    }

    result = service.normalize_trade(raw_trade)
    assert result["maker_address"] == "0xmaker123"
    assert result["taker_address"] == "0xtaker456"


def test_normalize_trade_missing_maker_taker():
    """Если maker/taker нет в raw trade — должен быть None."""
    from app.services.polymarket_poller_service import PolymarketPollerService

    service = PolymarketPollerService.__new__(PolymarketPollerService)

    raw_trade = {
        "transactionHash": "0xdef",
        "proxyWallet": "0xwallet2",
        "size": "500",
        "price": "0.50",
        "side": "SELL",
        "timestamp": 1710000000,
        "conditionId": "0xcond2",
        "outcome": "NO",
        "title": "Test 2",
        "slug": "test-2",
    }

    result = service.normalize_trade(raw_trade)
    assert result["maker_address"] is None
    assert result["taker_address"] is None
