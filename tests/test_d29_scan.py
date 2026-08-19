"""D29 scan job — unit tests (live-price fix, audit fix #4)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _row(market_id, yes_price, tokens):
    return {
        "market_id": market_id,
        "condition_id": f"cond-{market_id}",
        "category": "Politics",
        "yes_price": yes_price,
        "volume_24h": 500.0,
        "end_date": datetime.now(UTC) + timedelta(days=10),
        "clob_token_ids": tokens,
        "event_internal_id": "uuid-event",
        "event_id": "evt-1",
        "event_title": "Some event",
    }


@pytest.mark.asyncio
async def test_d29_scan_aggregates_sum_yes_from_live_prices():
    """sum_yes (arb-триггер) должен считаться по live-ценам, не по db_price снапшоту.

    db-снапшот не триггерит (0.30+0.30+0.30=0.90 < EMIT_SUM_THRESHOLD 1.10),
    live-цены поднимают sum до 1.20 > threshold — сигнал эмитится только если
    агрегат посчитан по live, а не по db_price.
    """
    import app.jobs.d29_scan as d29_scan_mod

    rows = [
        _row("m0", 0.30, ["yes_tok_m0", "no_tok_m0"]),
        _row("m1", 0.30, ["yes_tok_m1", "no_tok_m1"]),
        _row("m2", 0.30, ["yes_tok_m2", "no_tok_m2"]),  # db sum = 0.90 — ниже threshold
    ]

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    async def _fake_cache_get(key):
        # Живые цены поднимают sum_yes до 1.20 (> EMIT_SUM_THRESHOLD=1.10)
        live_by_token = {
            "microstructure:yes_tok_m0": 0.50,
            "microstructure:yes_tok_m1": 0.40,
            "microstructure:yes_tok_m2": 0.30,
        }
        if key in live_by_token:
            return {"midpoint": live_by_token[key], "epoch": 42.0}
        if key == "ws:conn_epoch":
            return {"epoch": 42.0}
        if key == "ws:last_message_at":
            return {"ts": time.time()}
        return None

    with (
        patch.object(d29_scan_mod, "get_settings", return_value=SimpleNamespace(d29_basket_enabled=True)),
        patch.object(d29_scan_mod, "fetch", AsyncMock(return_value=rows)),
        patch.object(d29_scan_mod, "fetch_open_event_ids", AsyncMock(return_value=set())),
        patch.object(d29_scan_mod, "get_detector_signal_repository", return_value=repo_mock),
        patch("app.services.live_prices.cache_get", AsyncMock(side_effect=_fake_cache_get)),
    ):
        result = await d29_scan_mod.run_d29_scan()

    assert result["live_priced"] == 3
    assert result["emitted"] == 1
    repo_mock.insert.assert_awaited_once()
