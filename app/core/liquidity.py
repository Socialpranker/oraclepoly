"""Liquidity classification — pure logic, без I/O.

Tiers основаны на аудите 2026-04-28 (memory/thin_book_audit_2026-04-28.md):
- thin: <$1K volume или <$50 на best level (LIVE fill rate ~10%)
- low:  $1K-$10K (LIVE fill rate ~30%)
- ok:   $10K-$100K
- deep: >$100K
"""

from __future__ import annotations

THIN_VOLUME_THRESHOLD: float = 1000.0
LOW_VOLUME_THRESHOLD: float = 10000.0
OK_VOLUME_THRESHOLD: float = 100000.0
MIN_BEST_LEVEL_DEPTH_USD: float = 50.0


def classify_liquidity(
    volume_24h: float | None,
    best_bid_size: float | None = None,
    best_ask_size: float | None = None,
) -> dict:
    """Классифицировать ликвидность маркета.

    Returns: {"tier": str, "volume_24h": float, "best_book_depth": float | None}
    """
    vol = float(volume_24h or 0.0)

    book_depth: float | None = None
    if best_bid_size is not None and best_ask_size is not None:
        book_depth = float(best_bid_size) + float(best_ask_size)

    if vol < THIN_VOLUME_THRESHOLD:
        tier = "thin"
    elif vol < LOW_VOLUME_THRESHOLD:
        tier = "low"
    elif vol < OK_VOLUME_THRESHOLD:
        tier = "ok"
    else:
        tier = "deep"

    if book_depth is not None and book_depth < MIN_BEST_LEVEL_DEPTH_USD:
        tier = "thin"

    return {
        "tier": tier,
        "volume_24h": vol,
        "best_book_depth": book_depth,
    }
