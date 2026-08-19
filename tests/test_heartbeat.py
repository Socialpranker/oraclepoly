"""Heart-beat dead-man для main process — Phase 1 Stage 5 (Step 5.1).

Симметрия с risk_daemon: main пишет heartbeat, проверяет daemon heartbeat.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.heartbeat import (
    check_daemon_alive,
    set_kill_if_daemon_dead,
    write_main_heartbeat,
)


def _fake_settings(**overrides):
    defaults = {
        "risk_daemon_heartbeat_interval_sec": 30,
        "risk_daemon_main_stale_threshold_sec": 300,
        "risk_daemon_check_interval_sec": 30,
    }
    defaults.update(overrides)
    return type("S", (), defaults)()


# --- write_main_heartbeat ------------------------------------------------


@pytest.mark.asyncio
async def test_write_main_heartbeat_sets_key_with_ttl(monkeypatch):
    """setex с TTL=stale_threshold."""
    mock_redis = AsyncMock()
    monkeypatch.setattr("app.core.heartbeat.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.core.heartbeat.get_settings", lambda: _fake_settings())

    await write_main_heartbeat()

    mock_redis.setex.assert_called_once()
    args = mock_redis.setex.call_args[0]
    assert args[0] == "bot:risk_heartbeat"
    assert args[1] == 300  # TTL = stale threshold


@pytest.mark.asyncio
async def test_write_main_heartbeat_silent_when_redis_down(monkeypatch):
    """Redis недоступен → silent no-op."""
    monkeypatch.setattr("app.core.heartbeat.get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr("app.core.heartbeat.get_settings", lambda: _fake_settings())
    # Не должно крашить
    await write_main_heartbeat()


# --- check_daemon_alive --------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_alive_when_fresh(monkeypatch):
    mock_redis = AsyncMock()
    fresh = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    mock_redis.get = AsyncMock(return_value=fresh)
    monkeypatch.setattr("app.core.heartbeat.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.core.heartbeat.get_settings", lambda: _fake_settings())

    ok, _ = await check_daemon_alive()
    assert ok


@pytest.mark.asyncio
async def test_daemon_dead_when_stale_6min(monkeypatch):
    mock_redis = AsyncMock()
    stale = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    mock_redis.get = AsyncMock(return_value=stale)
    monkeypatch.setattr("app.core.heartbeat.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.core.heartbeat.get_settings", lambda: _fake_settings())

    ok, reason = await check_daemon_alive()
    assert not ok
    assert "dead" in reason


@pytest.mark.asyncio
async def test_daemon_dead_when_heartbeat_missing(monkeypatch):
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    monkeypatch.setattr("app.core.heartbeat.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.core.heartbeat.get_settings", lambda: _fake_settings())

    ok, reason = await check_daemon_alive()
    assert not ok
    assert "missing" in reason


@pytest.mark.asyncio
async def test_daemon_alive_when_redis_down(monkeypatch):
    """Conservative: без Redis ничего не знаем → не блокируем."""
    monkeypatch.setattr("app.core.heartbeat.get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr("app.core.heartbeat.get_settings", lambda: _fake_settings())

    ok, _ = await check_daemon_alive()
    assert ok


# --- set_kill_if_daemon_dead ---------------------------------------------


@pytest.mark.asyncio
async def test_set_kill_when_daemon_dead(monkeypatch):
    """daemon stale → SET kill_switch + TG-алерт админам."""
    mock_redis = AsyncMock()
    stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    # 1st get: daemon heartbeat; 2nd get: existing kill_switch (None)
    mock_redis.get = AsyncMock(side_effect=[stale, None])
    monkeypatch.setattr("app.core.heartbeat.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.core.heartbeat.get_settings", lambda: _fake_settings())
    mock_notify = AsyncMock()
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", mock_notify)

    set_kill = await set_kill_if_daemon_dead()

    assert set_kill is True
    mock_redis.set.assert_called_once()
    args = mock_redis.set.call_args[0]
    assert args[0] == "bot:kill_switch"
    assert "main_self_check" in args[1]
    mock_notify.assert_called_once()
    assert "risk_daemon мёртв" in mock_notify.call_args[0][0]


@pytest.mark.asyncio
async def test_set_kill_skipped_when_already_set(monkeypatch):
    """existing kill_switch → preserve first cause, не перетираем, без повторного алерта."""
    mock_redis = AsyncMock()
    stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    mock_redis.get = AsyncMock(side_effect=[stale, b"already_set:earlier_reason"])
    monkeypatch.setattr("app.core.heartbeat.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.core.heartbeat.get_settings", lambda: _fake_settings())
    mock_notify = AsyncMock()
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", mock_notify)

    set_kill = await set_kill_if_daemon_dead()

    assert set_kill is False
    mock_redis.set.assert_not_called()
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_set_kill_noop_when_daemon_alive(monkeypatch):
    """daemon fresh → no kill action."""
    mock_redis = AsyncMock()
    fresh = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    mock_redis.get = AsyncMock(return_value=fresh)
    monkeypatch.setattr("app.core.heartbeat.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("app.core.heartbeat.get_settings", lambda: _fake_settings())

    set_kill = await set_kill_if_daemon_dead()

    assert set_kill is False
    mock_redis.set.assert_not_called()
