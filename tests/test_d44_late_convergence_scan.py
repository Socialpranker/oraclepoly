"""D44 late_convergence_scan job — unit tests (live-price fix, audit fix #4)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_d44_scan_uses_live_price_when_ws_cache_hit():
    """Live WS midpoint замещает 15-минутный db_price при валидном кэше."""
    import app.jobs.late_convergence_scan as d44_scan_mod

    row = {
        "market_id": "m0",
        "condition_id": "cond-m0",
        "category": "crypto",
        "question": "q?",
        "current_price": 0.97,  # db_price — в YES band [0.96, 0.98]
        "volume_24h": 20000.0,
        "end_date": datetime.now(UTC) + timedelta(hours=24),
        "clob_token_ids": ["yes_tok_m0", "no_tok_m0"],
        "liquidity": 10000.0,
        "event_id": "evt-1",
        "event_title": "Some event",
    }

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    async def _fake_cache_get(key):
        if key == "microstructure:yes_tok_m0":
            return {"midpoint": 0.965, "epoch": 42.0}  # тоже в band, но иное значение
        if key == "ws:conn_epoch":
            return {"epoch": 42.0}
        if key == "ws:last_message_at":
            return {"ts": time.time()}
        return None

    with (
        patch.object(d44_scan_mod, "fetch", AsyncMock(return_value=[row])),
        patch.object(d44_scan_mod, "fetch_open_event_ids", AsyncMock(return_value=set())),
        patch.object(d44_scan_mod, "get_detector_signal_repository", return_value=repo_mock),
        patch("app.services.live_prices.cache_get", AsyncMock(side_effect=_fake_cache_get)),
    ):
        result = await d44_scan_mod.run_late_convergence_scan()

    assert result["live_priced"] == 1
    inserted_signal = repo_mock.insert.call_args[0][0]
    assert inserted_signal.current_price == pytest.approx(0.965)
