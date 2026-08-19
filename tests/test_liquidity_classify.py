"""Тесты для classify_liquidity (pure logic)."""

from app.core.liquidity import classify_liquidity


def test_thin_when_volume_below_1k():
    result = classify_liquidity(volume_24h=500.0)
    assert result["tier"] == "thin"
    assert result["volume_24h"] == 500.0
    assert result["best_book_depth"] is None


def test_thin_when_volume_none():
    result = classify_liquidity(volume_24h=None)
    assert result["tier"] == "thin"
    assert result["volume_24h"] == 0.0


def test_low_when_volume_1k_to_10k():
    assert classify_liquidity(volume_24h=5000.0)["tier"] == "low"
    assert classify_liquidity(volume_24h=1000.0)["tier"] == "low"


def test_ok_when_volume_10k_to_100k():
    assert classify_liquidity(volume_24h=50000.0)["tier"] == "ok"
    assert classify_liquidity(volume_24h=10000.0)["tier"] == "ok"


def test_deep_when_volume_above_100k():
    assert classify_liquidity(volume_24h=500000.0)["tier"] == "deep"


def test_thin_overrides_when_best_level_too_small():
    result = classify_liquidity(volume_24h=50000.0, best_bid_size=10.0, best_ask_size=20.0)
    assert result["tier"] == "thin"
    assert result["best_book_depth"] == 30.0


def test_book_depth_passes_when_sufficient():
    result = classify_liquidity(volume_24h=50000.0, best_bid_size=100.0, best_ask_size=80.0)
    assert result["tier"] == "ok"
    assert result["best_book_depth"] == 180.0


def test_partial_book_data_treated_as_unknown():
    result = classify_liquidity(volume_24h=50000.0, best_bid_size=100.0, best_ask_size=None)
    assert result["tier"] == "ok"
    assert result["best_book_depth"] is None
