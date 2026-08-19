"""D14 scan job — unit tests (live-price fix, audit fix #4)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_d14_scan_uses_live_price_when_ws_cache_hit():
    """Live WS midpoint замещает 15-минутный db_price при валидном кэше."""
    import app.jobs.d14_scan as d14_scan_mod

    row = {
        "market_id": "m0",
        "condition_id": "cond-m0",
        "category": "Crypto",
        "question": "q?",
        "current_price": 0.60,  # db_price — в зоне, но устаревшая
        "volume_24h": 2000.0,
        "end_date": None,
        "clob_token_ids": ["yes_tok_m0", "no_tok_m0"],
        "hours_to_resolve": 24.0,
        "event_id": "evt-1",
    }

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    async def _fake_cache_get(key):
        if key == "microstructure:yes_tok_m0":
            return {"midpoint": 0.55, "epoch": 42.0}  # тоже в зоне, но иное значение
        if key == "ws:conn_epoch":
            return {"epoch": 42.0}
        if key == "ws:last_message_at":
            return {"ts": time.time()}
        return None

    with (
        patch.object(d14_scan_mod, "fetch", AsyncMock(return_value=[row])),
        patch.object(d14_scan_mod, "fetch_open_event_ids", AsyncMock(return_value=set())),
        patch.object(
            d14_scan_mod,
            "_fetch_wallet_quality",
            AsyncMock(
                return_value={
                    "cond-m0": {
                        "n_takers_24h_before": 5,
                        "smart_money_taker_share": 0.0,
                        "taker_pnl_weighted_usd_24h": 100.0,
                        "taker_roi_weighted_24h": 20.0,
                        "buy_imbalance_asof": 0.2,
                    }
                }
            ),
        ),
        patch.object(d14_scan_mod, "get_detector_signal_repository", return_value=repo_mock),
        patch("app.services.live_prices.cache_get", AsyncMock(side_effect=_fake_cache_get)),
    ):
        result = await d14_scan_mod.run_d14_scan()

    assert result["live_priced"] == 1
    inserted_signal = repo_mock.insert.call_args[0][0]
    assert inserted_signal.current_price == pytest.approx(0.55)
