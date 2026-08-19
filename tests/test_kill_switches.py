"""Тесты PR8 kill-switches: redis flag, daily PnL limit, bucket disable."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_redis_kill_flag_blocks_when_set():
    from app.services.execution_safety import check_redis_kill_flag

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value="1")

    with patch("app.core.cache.get_redis", new=AsyncMock(return_value=mock_redis)):
        ok, reason = await check_redis_kill_flag()
    assert ok is False
    assert "redis_flag_set" in reason


@pytest.mark.asyncio
async def test_redis_kill_flag_passes_when_not_set():
    from app.services.execution_safety import check_redis_kill_flag

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    with patch("app.core.cache.get_redis", new=AsyncMock(return_value=mock_redis)):
        ok, reason = await check_redis_kill_flag()
    assert ok is True


@pytest.mark.asyncio
async def test_redis_kill_flag_passes_when_redis_down():
    from app.services.execution_safety import check_redis_kill_flag

    with patch("app.core.cache.get_redis", new=AsyncMock(return_value=None)):
        ok, reason = await check_redis_kill_flag()
    # Production: Redis down → не блокируем (env kill_switch остаётся first line of defense)
    assert ok is True


@pytest.mark.asyncio
async def test_daily_pnl_limit_blocks_at_threshold():
    from app.services.execution_safety import check_daily_pnl_limit

    # bankroll $1000, threshold = -3% = -$30. Today PnL = -$50 → block.
    with patch(
        "app.services.database.fetchrow",
        new=AsyncMock(return_value={"today_pnl": -50.0}),
    ):
        ok, reason = await check_daily_pnl_limit(1000.0)
    assert ok is False
    assert "daily_loss_limit" in reason


@pytest.mark.asyncio
async def test_daily_pnl_limit_passes_when_within_threshold():
    from app.services.execution_safety import check_daily_pnl_limit

    # Today PnL = -$10, threshold = -$30 → pass
    with patch(
        "app.services.database.fetchrow",
        new=AsyncMock(return_value={"today_pnl": -10.0}),
    ):
        ok, reason = await check_daily_pnl_limit(1000.0)
    assert ok is True


@pytest.mark.asyncio
async def test_daily_pnl_limit_passes_when_no_data():
    from app.services.execution_safety import check_daily_pnl_limit

    with patch(
        "app.services.database.fetchrow",
        new=AsyncMock(return_value={"today_pnl": 0}),
    ):
        ok, reason = await check_daily_pnl_limit(1000.0)
    assert ok is True


@pytest.mark.asyncio
async def test_bucket_disabled_blocks():
    from app.services.execution_safety import check_bucket_disabled

    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(return_value=True)

    with (
        patch("app.core.cache.get_redis", new=AsyncMock(return_value=mock_redis)),
        patch(
            "app.services.execution_safety.fetchrow",
            new=AsyncMock(return_value={"category": "Politics", "current_price": 0.30}),
        ),
    ):
        ok, reason = await check_bucket_disabled("D5_long_horizon_extreme_fade", "mkt-1")
    assert ok is False
    assert "bucket_disabled" in reason
    assert "D5_long_horizon_extreme_fade:Politics:lt40" in reason


@pytest.mark.asyncio
async def test_bucket_disabled_passes_when_not_in_set():
    from app.services.execution_safety import check_bucket_disabled

    mock_redis = AsyncMock()
    mock_redis.sismember = AsyncMock(return_value=False)

    with (
        patch("app.core.cache.get_redis", new=AsyncMock(return_value=mock_redis)),
        patch(
            "app.services.execution_safety.fetchrow",
            new=AsyncMock(return_value={"category": "Sports", "current_price": 0.50}),
        ),
    ):
        ok, reason = await check_bucket_disabled("D4_small_market_fade", "mkt-2")
    assert ok is True


@pytest.mark.asyncio
async def test_bucket_disabled_passes_when_market_unknown():
    from app.services.execution_safety import check_bucket_disabled

    mock_redis = AsyncMock()

    with (
        patch("app.core.cache.get_redis", new=AsyncMock(return_value=mock_redis)),
        patch("app.services.execution_safety.fetchrow", new=AsyncMock(return_value=None)),
    ):
        ok, reason = await check_bucket_disabled("D4", "unknown-mkt")
    # Без market data — не блокируем (детерминированно)
    assert ok is True


@pytest.mark.asyncio
async def test_bucket_zone_classification():
    """Проверяем что zone parsing работает для всех price ranges."""
    from app.services.execution_safety import check_bucket_disabled

    mock_redis = AsyncMock()
    captured_keys = []

    async def capture_sismember(set_name, key):
        captured_keys.append(key)
        return False

    mock_redis.sismember = capture_sismember

    test_cases = [
        (0.20, "lt40"),
        (0.50, "40-60"),
        (0.70, "60-80"),
        (0.90, "80-95"),
        (0.97, "gte95_blocked"),
    ]

    for price, expected_zone in test_cases:
        captured_keys.clear()
        with (
            patch("app.core.cache.get_redis", new=AsyncMock(return_value=mock_redis)),
            patch(
                "app.services.execution_safety.fetchrow",
                new=AsyncMock(return_value={"category": "Sports", "current_price": price}),
            ),
        ):
            await check_bucket_disabled("D4", "mkt")
        assert captured_keys[0].endswith(expected_zone), (
            f"price={price}: expected zone {expected_zone}, got key {captured_keys[0]}"
        )
