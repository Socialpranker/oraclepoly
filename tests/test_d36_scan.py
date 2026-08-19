"""D36 scan job — unit tests (live-price fix, audit fix #4)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_d36_scan_uses_live_price_when_ws_cache_hit():
    """Live WS midpoint замещает 15-минутный db_price при валидном кэше."""
    import app.jobs.d36_scan as d36_scan_mod

    row = {
        "market_id": "m0",
        "condition_id": "cond-m0",
        "category": "Politics",
        "question": "q?",
        "current_price": 0.50,  # db_price — в зоне [0.30, 0.85]
        "volume_24h": 2000.0,
        "end_date": None,
        "clob_token_ids": ["yes_tok_m0", "no_tok_m0"],
        "hours_to_resolve": 24.0,
        "event_id": "evt-1",
    }

    book_metrics = {
        "obi": 0.5,
        "spread": 0.01,
        "total_depth_usd": 10000.0,
        "bid_depth_usd": 7500.0,
        "ask_depth_usd": 2500.0,
        "best_bid": 0.44,
        "best_ask": 0.45,
        "mid_price": 0.445,
    }

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    async def _fake_cache_get(key):
        if key == "microstructure:yes_tok_m0":
            return {"midpoint": 0.45, "epoch": 42.0}
        if key == "ws:conn_epoch":
            return {"epoch": 42.0}
        if key == "ws:last_message_at":
            return {"ts": time.time()}
        return None

    async def _fake_fetch_book_for_market(market, semaphore):
        return market, book_metrics

    with (
        patch.object(d36_scan_mod, "fetch", AsyncMock(return_value=[row])),
        patch.object(d36_scan_mod, "_fetch_book_for_market", _fake_fetch_book_for_market),
        patch.object(d36_scan_mod, "fetch_open_event_ids", AsyncMock(return_value=set())),
        patch.object(d36_scan_mod, "get_detector_signal_repository", return_value=repo_mock),
        patch("app.services.live_prices.cache_get", AsyncMock(side_effect=_fake_cache_get)),
    ):
        result = await d36_scan_mod.run_d36_scan()

    assert result["live_priced"] == 1
    inserted_signal = repo_mock.insert.call_args[0][0]
    assert inserted_signal.current_price == pytest.approx(0.45)
