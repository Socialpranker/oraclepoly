"""D26 scan job — unit tests (live-price fix, audit fix #4)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_d26_scan_uses_live_price_when_ws_cache_hit():
    """Live WS midpoint замещает 15-минутный db_price при валидном кэше."""
    import app.jobs.d26_scan as d26_scan_mod

    row = {
        "market_id": "m0",
        "condition_id": "cond-m0",
        "category": "Politics",
        "question": "q?",
        "current_price": 0.10,  # db_price — в extreme-low зоне
        "volume_24h": 6000.0,
        "end_date": None,
        "clob_token_ids": ["yes_tok_m0", "no_tok_m0"],
        "hours_to_resolve": 24.0,
        "event_id": "evt-1",
    }

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    async def _fake_cache_get(key):
        if key == "microstructure:yes_tok_m0":
            return {"midpoint": 0.08, "epoch": 42.0}  # тоже extreme-low, но иное значение
        if key == "ws:conn_epoch":
            return {"epoch": 42.0}
        if key == "ws:last_message_at":
            return {"ts": time.time()}
        return None

    with (
        patch.object(d26_scan_mod, "fetch", AsyncMock(return_value=[row])),
        patch.object(d26_scan_mod, "_load_fade_whale_set", AsyncMock(return_value={"whale1"})),
        patch.object(d26_scan_mod, "fetch_open_event_ids", AsyncMock(return_value=set())),
        patch.object(
            d26_scan_mod,
            "_aggregate_fade_whale_pushes",
            AsyncMock(return_value={"cond-m0": {"push_usd": 4000.0, "drift_pp": 0.02}}),
        ),
        patch.object(d26_scan_mod, "get_detector_signal_repository", return_value=repo_mock),
        patch("app.services.live_prices.cache_get", AsyncMock(side_effect=_fake_cache_get)),
    ):
        result = await d26_scan_mod.run_d26_scan()

    assert result["live_priced"] == 1
    inserted_signal = repo_mock.insert.call_args[0][0]
    assert inserted_signal.current_price == pytest.approx(0.08)
