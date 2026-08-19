"""D13 scan job — unit tests."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest


def test_wallet_lookup_sql_uses_lower():
    """_WALLET_PERF_LOOKUP_SQL должен матчить lowercase wallets против mixed-case таблицы."""
    from app.jobs.d13_scan import _WALLET_PERF_LOOKUP_SQL
    assert "LOWER(wallet)" in _WALLET_PERF_LOOKUP_SQL, (
        "_WALLET_PERF_LOOKUP_SQL должен использовать LOWER(wallet) = ANY($1), "
        "иначе lowercase wallets из API не матчатся с mixed-case в таблице"
    )


@pytest.mark.asyncio
async def test_d13_scan_uses_live_price_when_ws_cache_hit():
    """Live WS midpoint замещает 15-минутный db_price при валидном кэше (audit fix #4)."""
    import app.jobs.d13_scan as d13_scan_mod

    row = {
        "market_id": "m0",
        "condition_id": "cond-m0",
        "category": "Politics",
        "question": "q?",
        "current_price": 0.90,  # db_price — в зоне, но устаревшая
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
            return {"midpoint": 0.87, "epoch": 42.0}  # тоже в зоне, но иное значение
        if key == "ws:conn_epoch":
            return {"epoch": 42.0}
        if key == "ws:last_message_at":
            return {"ts": time.time()}
        return None

    with (
        patch.object(d13_scan_mod, "D13_EMIT_DISABLED", False),
        patch.object(d13_scan_mod, "fetch", AsyncMock(return_value=[row])),
        patch.object(
            d13_scan_mod,
            "fetch_open_event_ids",
            AsyncMock(return_value=set()),
        ),
        patch.object(
            d13_scan_mod,
            "_fetch_wallet_quality",
            AsyncMock(
                return_value={
                    "cond-m0": {
                        "n_takers_24h_before": 5,
                        "smart_money_taker_share": 0.0,
                    }
                }
            ),
        ),
        patch.object(d13_scan_mod, "get_detector_signal_repository", return_value=repo_mock),
        patch("app.services.live_prices.cache_get", AsyncMock(side_effect=_fake_cache_get)),
    ):
        result = await d13_scan_mod.run_d13_scan()

    assert result["live_priced"] == 1
    inserted_signal = repo_mock.insert.call_args[0][0]
    assert inserted_signal.current_price == pytest.approx(0.87)
