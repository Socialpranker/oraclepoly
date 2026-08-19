"""Unit tests for G18 bin calibration lookup.

Тестируем чисто bin_lookup() logic:
- whitelist gating
- category exclude
- price binning (floor to 0.1)
- min observations threshold
- no_data fallback
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.g18_bin_calibration import (
    WHITELIST_CATEGORIES_EXCLUDE,
    WHITELIST_DETECTORS,
    _price_bin,
    bin_lookup,
    invalidate_cache,
)


def test_price_bin_floors_to_decimal():
    assert _price_bin(0.0) == 0.0
    assert _price_bin(0.09) == 0.0
    assert _price_bin(0.10) == 0.1
    assert _price_bin(0.55) == 0.5
    assert _price_bin(0.97) == 0.9
    assert _price_bin(0.999) == 0.9


@pytest.mark.asyncio
async def test_bin_lookup_skips_detector_not_in_whitelist():
    p, src = await bin_lookup("D_random_unknown", "Politics", 0.55)
    assert p is None
    assert src == "g18_skip_detector"


@pytest.mark.asyncio
async def test_bin_lookup_skips_excluded_category():
    # Pick any whitelisted detector
    detector = next(iter(WHITELIST_DETECTORS))
    excluded_cat = next(iter(WHITELIST_CATEGORIES_EXCLUDE))
    p, src = await bin_lookup(detector, excluded_cat, 0.55)
    assert p is None
    assert src == "g18_skip_category"


@pytest.mark.asyncio
async def test_bin_lookup_skips_invalid_price():
    detector = next(iter(WHITELIST_DETECTORS))
    p, src = await bin_lookup(detector, "Politics", 0.0)
    assert p is None
    assert src == "g18_no_data"
    p, src = await bin_lookup(detector, "Politics", 1.0)
    assert p is None
    assert src == "g18_no_data"


@pytest.mark.asyncio
async def test_bin_lookup_returns_wr_when_found():
    """Mock _load_bins to return a known bin."""
    detector = next(iter(WHITELIST_DETECTORS))

    async def mock_load_bins():
        return {
            (detector, "Politics", 0.5): {"n": 100, "wr": 0.42},
        }

    invalidate_cache()
    with patch("app.services.g18_bin_calibration._load_bins", side_effect=mock_load_bins):
        p, src = await bin_lookup(detector, "Politics", 0.55)
        assert p == 0.42
        assert src == "g18_bin"


@pytest.mark.asyncio
async def test_bin_lookup_no_data_when_bin_missing():
    detector = next(iter(WHITELIST_DETECTORS))

    async def mock_load_bins():
        return {
            (detector, "Politics", 0.5): {"n": 100, "wr": 0.42},
        }

    invalidate_cache()
    with patch("app.services.g18_bin_calibration._load_bins", side_effect=mock_load_bins):
        # Looking for a bin that doesn't exist
        p, src = await bin_lookup(detector, "Politics", 0.95)
        assert p is None
        assert src == "g18_no_data"


@pytest.mark.asyncio
async def test_bin_lookup_unknown_category_falls_through():
    detector = next(iter(WHITELIST_DETECTORS))

    async def mock_load_bins():
        return {
            (detector, "unknown", 0.5): {"n": 80, "wr": 0.55},
        }

    invalidate_cache()
    with patch("app.services.g18_bin_calibration._load_bins", side_effect=mock_load_bins):
        # When category is None, lookup uses 'unknown'
        p, src = await bin_lookup(detector, None, 0.55)
        assert p == 0.55
        assert src == "g18_bin"
