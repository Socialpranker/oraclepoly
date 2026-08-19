"""Tests for app.core.peak_bank — Phase 1 Stage 2.

Peak bankroll tracking через Redis. risk_engine использует peak_bank для
drawdown reduce: если current bank ≤ 80% peak → halve stakes.

Storage: Redis key 'bot:risk:peak_bank_usd' (float). Stateless: read +
GREATEST update в одну транзакцию.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.peak_bank import (
    LIVE_PEAK_BANK_KEY,
    PEAK_BANK_KEY,
    get_peak_bank,
    update_peak_if_higher,
)


@pytest.mark.asyncio
async def test_get_peak_bank_when_unset(monkeypatch):
    """Если ключ не установлен → None (caller использует current bank как seed)."""
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.core.peak_bank.get_redis", AsyncMock(return_value=fake_redis)
    )
    result = await get_peak_bank()
    assert result is None


@pytest.mark.asyncio
async def test_get_peak_bank_returns_float(monkeypatch):
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=b"1234.56")
    monkeypatch.setattr(
        "app.core.peak_bank.get_redis", AsyncMock(return_value=fake_redis)
    )
    result = await get_peak_bank()
    assert result == 1234.56


@pytest.mark.asyncio
async def test_get_peak_bank_redis_down_returns_none(monkeypatch):
    """Redis недоступен → None (conservative: caller использует fallback)."""
    monkeypatch.setattr("app.core.peak_bank.get_redis", AsyncMock(return_value=None))
    result = await get_peak_bank()
    assert result is None


@pytest.mark.asyncio
async def test_update_peak_writes_when_higher(monkeypatch):
    """current=$1500, stored peak=$1000 → пишем $1500."""
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=b"1000.00")
    fake_redis.set = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.core.peak_bank.get_redis", AsyncMock(return_value=fake_redis)
    )
    new_peak = await update_peak_if_higher(1500.0)
    assert new_peak == 1500.0
    fake_redis.set.assert_called_once()
    # Key, value
    args = fake_redis.set.call_args
    assert args[0][0] == PEAK_BANK_KEY
    assert float(args[0][1]) == 1500.0


@pytest.mark.asyncio
async def test_update_peak_noop_when_lower(monkeypatch):
    """current=$800, stored peak=$1000 → не пишем."""
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=b"1000.00")
    fake_redis.set = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.core.peak_bank.get_redis", AsyncMock(return_value=fake_redis)
    )
    new_peak = await update_peak_if_higher(800.0)
    assert new_peak == 1000.0
    fake_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_update_peak_seeds_when_unset(monkeypatch):
    """Stored peak=None → пишем current как initial seed."""
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=None)
    fake_redis.set = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.core.peak_bank.get_redis", AsyncMock(return_value=fake_redis)
    )
    new_peak = await update_peak_if_higher(500.0)
    assert new_peak == 500.0
    fake_redis.set.assert_called_once()


@pytest.mark.asyncio
async def test_update_peak_redis_down_returns_current(monkeypatch):
    """Redis недоступен → возвращаем current (caller использует как fallback peak)."""
    monkeypatch.setattr("app.core.peak_bank.get_redis", AsyncMock(return_value=None))
    new_peak = await update_peak_if_higher(700.0)
    assert new_peak == 700.0


@pytest.mark.asyncio
async def test_update_peak_negative_current_ignored(monkeypatch):
    """current <= 0 (corrupt) → не пишем, не крашим."""
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=b"1000.00")
    fake_redis.set = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.core.peak_bank.get_redis", AsyncMock(return_value=fake_redis)
    )
    new_peak = await update_peak_if_higher(-100.0)
    assert new_peak == 1000.0
    fake_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_get_peak_bank_uses_custom_key(monkeypatch):
    """key=LIVE_PEAK_BANK_KEY → читает отдельный Redis-ключ, не дефолтный."""
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=b"800.0")
    monkeypatch.setattr(
        "app.core.peak_bank.get_redis", AsyncMock(return_value=fake_redis)
    )
    result = await get_peak_bank(key=LIVE_PEAK_BANK_KEY)
    assert result == 800.0
    fake_redis.get.assert_called_once_with(LIVE_PEAK_BANK_KEY)


@pytest.mark.asyncio
async def test_update_peak_writes_custom_key_when_higher(monkeypatch):
    """key=LIVE_PEAK_BANK_KEY → GREATEST-запись идёт в отдельный ключ,
    не задевая дефолтный bot:risk:peak_bank_usd (daemon-exposure)."""
    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=b"400.0")
    fake_redis.set = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.core.peak_bank.get_redis", AsyncMock(return_value=fake_redis)
    )
    new_peak = await update_peak_if_higher(500.0, key=LIVE_PEAK_BANK_KEY)
    assert new_peak == 500.0
    fake_redis.get.assert_called_once_with(LIVE_PEAK_BANK_KEY)
    args = fake_redis.set.call_args
    assert args[0][0] == LIVE_PEAK_BANK_KEY
    assert float(args[0][1]) == 500.0


def test_live_peak_bank_key_distinct_from_default():
    """Отдельная константа — не алиас на старый ключ (иначе задача D бессмысленна)."""
    assert LIVE_PEAK_BANK_KEY != PEAK_BANK_KEY
    assert LIVE_PEAK_BANK_KEY == "bot:risk:live_peak_bank_usd"
