"""Fix-4 (latency-аудит №3): live-override цены для D38-скана.

D38-скан читал только markets.current_price — 15-минутный снапшот market_sync,
при том что скан бежит каждые 5 минут (2-3 прохода на одной stale-цене).
Зона (YES 0.80-0.95 / NO 0.05-0.15) и entry_price сигнала целиком зависят от
этой цены. Фикс — тот же live-override, что в d3_scan.py: get_live_yes_price
(WS-кэш c fail-closed epoch-гейтом) замещает db-цену, при отсутствии/протухании
кэша — честный fallback на db-цену (поведение не меняется).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


def _row(market_id, price, *, event_id="evt-1"):
    return {
        "market_id": market_id,
        "condition_id": f"cond-{market_id}",
        "category": "Sports",
        "question": "Team A wins?",
        "current_price": price,
        "volume_24h": 600.0,
        "end_date": datetime.now(UTC) - timedelta(hours=2),
        "clob_token_ids": [f"yes_{market_id}", f"no_{market_id}"],
        "lag_hours": 2.0,
        "event_id": event_id,
        "event_title": "Event",
    }


def _ws_cache(token_key: str, midpoint: float):
    """Валидный state-кэш (контракт 2026-07-03): эпоха совпадает + heartbeat свежий."""

    async def _fake_cache_get(key):
        if key == token_key:
            return {"midpoint": midpoint, "epoch": 42.0}
        if key == "ws:conn_epoch":
            return {"epoch": 42.0}
        if key == "ws:last_message_at":
            return {"ts": time.time()}
        return None

    return AsyncMock(side_effect=_fake_cache_get)


@pytest.mark.asyncio
async def test_d38_scan_uses_live_price_when_ws_cache_hit():
    """Live WS midpoint замещает db-цену: сигнал несёт 0.90, не stale 0.85."""
    from app.jobs.d38_scan import run_d38_scan

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d38_scan.fetch", AsyncMock(return_value=[_row("m0", 0.85)])),
        patch("app.jobs.d38_scan.fetch_open_event_ids", AsyncMock(return_value=set())),
        patch("app.jobs.d38_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.services.live_prices.cache_get", _ws_cache("microstructure:yes_m0", 0.90)),
    ):
        result = await run_d38_scan()

    assert result["live_priced"] == 1
    assert result["inserted"] == 1
    inserted_signal = repo_mock.insert.call_args[0][0]
    assert inserted_signal.current_price == 0.90


@pytest.mark.asyncio
async def test_d38_scan_live_price_out_of_zone_kills_stale_signal():
    """Смысл фикса: db-цена 0.85 (в зоне), live уже 0.70 (вне) → сигнала НЕТ."""
    from app.jobs.d38_scan import run_d38_scan

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d38_scan.fetch", AsyncMock(return_value=[_row("m0", 0.85)])),
        patch("app.jobs.d38_scan.fetch_open_event_ids", AsyncMock(return_value=set())),
        patch("app.jobs.d38_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.services.live_prices.cache_get", _ws_cache("microstructure:yes_m0", 0.70)),
    ):
        result = await run_d38_scan()

    assert result["live_priced"] == 1
    assert result["signals"] == 0
    repo_mock.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_d38_scan_falls_back_to_db_price_without_ws_cache():
    """Нет WS-кэша → db-цена, поведение прежнее (fail-closed fallback)."""
    from app.jobs.d38_scan import run_d38_scan

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d38_scan.fetch", AsyncMock(return_value=[_row("m0", 0.85)])),
        patch("app.jobs.d38_scan.fetch_open_event_ids", AsyncMock(return_value=set())),
        patch("app.jobs.d38_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.services.live_prices.cache_get", AsyncMock(return_value=None)),
    ):
        result = await run_d38_scan()

    assert result["live_priced"] == 0
    assert result["inserted"] == 1
    inserted_signal = repo_mock.insert.call_args[0][0]
    assert inserted_signal.current_price == 0.85
