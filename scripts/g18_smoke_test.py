"""G18 smoke test — vbedrennaya проверка logic без pytest.

Запускается через docker exec oraclepoly-backend python scripts/g18_smoke_test.py
"""

from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    from unittest.mock import patch

    from app.services.g18_bin_calibration import (
        WHITELIST_CATEGORIES_EXCLUDE,
        WHITELIST_DETECTORS,
        _price_bin,
        bin_lookup,
        invalidate_cache,
    )

    print("=== Test 1: _price_bin floors correctly ===")
    cases = [(0.0, 0.0), (0.09, 0.0), (0.10, 0.1), (0.55, 0.5), (0.97, 0.9), (0.999, 0.9)]
    for inp, expected in cases:
        got = _price_bin(inp)
        assert got == expected, f"_price_bin({inp}) = {got}, expected {expected}"
        print(f"  OK: _price_bin({inp}) = {got}")

    detector = next(iter(WHITELIST_DETECTORS))
    excluded_cat = next(iter(WHITELIST_CATEGORIES_EXCLUDE))

    print("\n=== Test 2: skips detector not in whitelist ===")
    p, src = await bin_lookup("D_random_unknown", "Politics", 0.55)
    assert p is None and src == "g18_skip_detector", f"{p=} {src=}"
    print("  OK: not whitelisted → g18_skip_detector")

    print(f"\n=== Test 3: skips excluded category ({excluded_cat}) ===")
    p, src = await bin_lookup(detector, excluded_cat, 0.55)
    assert p is None and src == "g18_skip_category", f"{p=} {src=}"
    print("  OK: excluded category → g18_skip_category")

    print("\n=== Test 4: skips invalid price ===")
    for invalid_price in [0.0, 1.0, -0.1, 1.5]:
        p, src = await bin_lookup(detector, "Politics", invalid_price)
        assert p is None and src == "g18_no_data", f"{invalid_price}: {p=} {src=}"
        print(f"  OK: price={invalid_price} → g18_no_data")

    print("\n=== Test 5: returns WR when bin found ===")

    async def mock_load_bins_with_data():
        return {(detector, "Politics", 0.5): {"n": 100, "wr": 0.42}}

    invalidate_cache()
    with patch("app.services.g18_bin_calibration._load_bins", side_effect=mock_load_bins_with_data):
        p, src = await bin_lookup(detector, "Politics", 0.55)
        assert p == 0.42 and src == "g18_bin", f"{p=} {src=}"
        print(f"  OK: found bin → p={p}, src={src}")

    print("\n=== Test 6: no_data when bin not in lookup ===")
    invalidate_cache()
    with patch("app.services.g18_bin_calibration._load_bins", side_effect=mock_load_bins_with_data):
        p, src = await bin_lookup(detector, "Politics", 0.95)
        assert p is None and src == "g18_no_data", f"{p=} {src=}"
        print(f"  OK: missing bin → src={src}")

    print("\n=== Test 7: None category maps to 'unknown' ===")

    async def mock_load_bins_unknown():
        return {(detector, "unknown", 0.5): {"n": 80, "wr": 0.55}}

    invalidate_cache()
    with patch("app.services.g18_bin_calibration._load_bins", side_effect=mock_load_bins_unknown):
        p, src = await bin_lookup(detector, None, 0.55)
        assert p == 0.55 and src == "g18_bin", f"{p=} {src=}"
        print(f"  OK: None category → p={p}")

    invalidate_cache()
    print("\n[OK] All G18 smoke tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
