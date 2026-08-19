"""G18 integration test — берёт real D5/D10b/D14 signal из БД и прогоняет через _calibrate.

Verifies G18 lookup активен в production code path.
"""

from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    sys.path.insert(0, "/app")
    from app.services.database import close_db, fetchrow, init_db
    from app.services.detector_signal_service import _calibrate
    from app.services.g18_bin_calibration import WHITELIST_DETECTORS, invalidate_cache

    await init_db()
    invalidate_cache()
    try:
        for detector in WHITELIST_DETECTORS:
            sql = """
                SELECT ds.id, ds.detector, ds.confidence AS raw_p, ds.current_price,
                       ds.market_id, m.category
                FROM detector_signals ds
                LEFT JOIN markets m ON m.polymarket_id = ds.market_id
                WHERE ds.detector = $1
                ORDER BY ds.generated_at DESC
                LIMIT 1
            """
            r = await fetchrow(sql, detector)
            if r is None:
                print(f"\n[skip] No signals at all for {detector}")
                continue

            row = dict(r)
            raw_p = float(row["raw_p"])
            print(f"\n=== {detector} ===")
            print(f"  raw_p={raw_p:.3f}, current_price={row['current_price']}, category={row['category']!r}")

            p, src = await _calibrate(raw_p, detector, cals={}, row=row, lgb_cals={})
            print(f"  After _calibrate: p={p:.3f}, source={src}")

            if src == "g18_bin":
                print(f"  G18 WIN: raw {raw_p:.3f} → calibrated {p:.3f} (delta {p - raw_p:+.3f})")
            elif src == "g18_no_data":
                print("  G18 skip: bin not found in lookup")
            elif src == "g18_skip_category":
                print("  G18 skip: category in exclude list")
            elif src == "raw":
                print("  Fell through to raw_p (no Beta calibrator since cals={})")

    finally:
        await close_db()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
