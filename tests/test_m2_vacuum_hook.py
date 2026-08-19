"""Tests for M2 vacuum WSS hook."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.detectors.m2_vacuum_hook import _m2_depletion_check


def test_depletion_check_asks_depleted_returns_ask():
    # depth_ask << depth_bid → 'ask'
    assert _m2_depletion_check({"depth_bid": 1000.0, "depth_ask": 100.0}) == "ask"


def test_depletion_check_bids_depleted_returns_bid():
    assert _m2_depletion_check({"depth_bid": 100.0, "depth_ask": 1000.0}) == "bid"


def test_depletion_check_balanced_returns_none():
    # ratio 0.5 >= 0.30 → None
    assert _m2_depletion_check({"depth_bid": 1000.0, "depth_ask": 500.0}) is None


def test_depletion_check_zero_depth_returns_none():
    assert _m2_depletion_check({"depth_bid": 0.0, "depth_ask": 0.0}) is None


def test_depletion_check_missing_keys_returns_none():
    assert _m2_depletion_check({}) is None


# ── 3.4 _m2_emit_async (моки Redis/DB/repo) ──


@pytest.mark.asyncio
async def test_emit_async_throttled_skips_insert():
    """SETNX вернул False (дубль) → repo.insert НЕ вызван."""
    from app.detectors import m2_vacuum_hook as hook

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=False)  # throttle hit
    repo_insert = AsyncMock(return_value=True)
    with (
        patch.object(hook, "get_redis", new=AsyncMock(return_value=fake_redis)),
        patch("app.repositories.detector_signal_repository.DetectorSignalRepository.insert", repo_insert),
        patch.object(hook, "fetchrow", new=AsyncMock()),
    ):
        await hook._m2_emit_async("mkt1", "tok1", {"depth_bid": 1000.0, "depth_ask": 100.0, "midpoint": 0.5}, "ask")
    repo_insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_emit_async_inserts_signal_with_whale_flag():
    """Happy path: throttle OK, whale-флаг True → insert вызван, evidence.whale_in_window=True."""
    from app.detectors import m2_vacuum_hook as hook

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)  # throttle acquired
    row = {
        "polymarket_id": "0xpoly", "condition_id": "0xcond", "clob_token_ids": ["t1", "t2"],
        "category": "Sports", "current_price": 0.5, "event_id": "ev1",
    }
    captured = {}

    async def _capture_insert(self, signal):
        captured["signal"] = signal
        return True

    with (
        patch.object(hook, "get_redis", new=AsyncMock(return_value=fake_redis)),
        patch.object(hook, "fetchrow", new=AsyncMock(return_value=row)),
        patch.object(hook, "cache_get", new=AsyncMock(return_value="1")),  # whale seen
        patch("app.repositories.detector_signal_repository.DetectorSignalRepository.insert", _capture_insert),
        patch("app.services.snapshot_recorder.record_event", new=AsyncMock()),
    ):
        await hook._m2_emit_async(
            "mkt1", "tok1",
            {"depth_bid": 1000.0, "depth_ask": 100.0, "midpoint": 0.5},
            "ask",
        )
    assert "signal" in captured
    assert captured["signal"].evidence["whale_in_window"] is True
    assert captured["signal"].side == "YES"
    # detector_signals.market_id конвенция = markets.polymarket_id (0x-condition_id),
    # НЕ внутренний UUID markets.id — иначе resolve-JOIN не матчится и сигнал censored
    assert captured["signal"].market_id == "0xpoly"


@pytest.mark.asyncio
async def test_emit_async_no_polymarket_id_skips_insert():
    """markets.polymarket_id NULL → сигнал не эмитим (был бы вечно censored)."""
    from app.detectors import m2_vacuum_hook as hook

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)
    row = {
        "polymarket_id": None, "condition_id": "0xcond", "clob_token_ids": ["t1", "t2"],
        "category": "Sports", "current_price": 0.5, "event_id": "ev1",
    }
    repo_insert = AsyncMock(return_value=True)
    with (
        patch.object(hook, "get_redis", new=AsyncMock(return_value=fake_redis)),
        patch.object(hook, "fetchrow", new=AsyncMock(return_value=row)),
        patch.object(hook, "cache_get", new=AsyncMock(return_value=None)),
        patch("app.repositories.detector_signal_repository.DetectorSignalRepository.insert", repo_insert),
    ):
        await hook._m2_emit_async(
            "mkt1", "tok1",
            {"depth_bid": 1000.0, "depth_ask": 100.0, "midpoint": 0.5},
            "ask",
        )
    repo_insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_emit_async_exception_not_propagated():
    """Исключение внутри → проглатывается (WSS не падает)."""
    from app.detectors import m2_vacuum_hook as hook

    with patch.object(hook, "fetchrow", new=AsyncMock(side_effect=RuntimeError("db down"))):
        # не должно бросить
        await hook._m2_emit_async("mkt1", "tok1", {"depth_bid": 1000.0, "depth_ask": 100.0, "midpoint": 0.5}, "ask")


# ── 4.2 Feature-flag m2_vacuum_enabled ──


def test_m2_vacuum_disabled_by_default():
    from app.core.config import get_settings
    from app.detectors.m2_vacuum_hook import _m2_enabled

    get_settings.cache_clear()  # lru_cache
    assert _m2_enabled() is False


# ── 5.4 WSS-интеграция guard ──


@pytest.mark.asyncio
async def test_hook_disabled_no_task(monkeypatch):
    """_m2_enabled False → emit не планируется (поведенческая проверка флага)."""
    from app.detectors import m2_vacuum_hook as hook

    monkeypatch.setattr(hook, "_m2_enabled", lambda: False)
    assert hook._m2_enabled() is False


# ── 6.3 Whale-флаг round-trip ──


@pytest.mark.asyncio
async def test_whale_in_window_reads_flag():
    from app.detectors import m2_vacuum_hook as hook

    with patch.object(hook, "cache_get", new=AsyncMock(return_value="1")):
        assert await hook._whale_in_window("mkt1") is True
    with patch.object(hook, "cache_get", new=AsyncMock(return_value=None)):
        assert await hook._whale_in_window("mkt1") is False


@pytest.mark.asyncio
async def test_emit_async_fetches_event_polymarket_id_not_uuid():
    """detector_signals.event_id конвенция = events.polymarket_id (эталон d38_scan.py).

    Регрессия 2026-07-10 (сиквел market_id-бага): SELECT брал markets.event_id
    (внутренний UUID FK на events.id) → event-кластеризация аналитики не
    группировала строки M2. SQL обязан джойнить events и отдавать
    e.polymarket_id AS event_id; NULL допустим (event_id nullable).
    """
    from app.detectors import m2_vacuum_hook as hook

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)
    row = {
        "polymarket_id": "0xpoly", "condition_id": "0xcond", "clob_token_ids": ["t1", "t2"],
        "category": "Sports", "current_price": 0.5, "event_id": "ev-poly-1",
    }
    fetchrow_mock = AsyncMock(return_value=row)
    captured = {}

    async def fake_insert(self, signal):
        captured["signal"] = signal
        return True

    with (
        patch.object(hook, "get_redis", new=AsyncMock(return_value=fake_redis)),
        patch.object(hook, "fetchrow", new=fetchrow_mock),
        patch.object(hook, "cache_get", new=AsyncMock(return_value=None)),
        patch("app.repositories.detector_signal_repository.DetectorSignalRepository.insert", fake_insert),
        patch("app.services.snapshot_recorder.record_event", new=AsyncMock()),
    ):
        await hook._m2_emit_async(
            "mkt1", "tok1",
            {"depth_bid": 1000.0, "depth_ask": 100.0, "midpoint": 0.5},
            "ask",
        )

    sql = " ".join(fetchrow_mock.call_args.args[0].split())
    assert "e.polymarket_id AS event_id" in sql
    assert "LEFT JOIN events e ON e.id = m.event_id" in sql
    assert captured["signal"].event_id == "ev-poly-1"
