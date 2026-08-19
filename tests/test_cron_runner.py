"""
Cron Runner Tests

Проверяет что cron_runner инициализирует DB pool и Redis перед запуском job.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_cron_runner_initializes_db_and_redis():
    """run_job() должен вызвать init_db/init_redis перед job и close_db/close_redis после."""
    from scripts.cron_runner import run_job

    mock_init_db = AsyncMock()
    mock_close_db = AsyncMock()
    mock_init_redis = AsyncMock()
    mock_close_redis = AsyncMock()
    mock_job = AsyncMock(return_value={"ok": True})

    with (
        patch("app.services.database.init_db", mock_init_db),
        patch("app.services.database.close_db", mock_close_db),
        patch("app.core.cache.init_redis", mock_init_redis),
        patch("app.core.cache.close_redis", mock_close_redis),
        patch("app.core.config.get_settings") as mock_settings,
        patch("scripts.cron_runner.COMMANDS", {"test-job": ("fake.module", "fake_func")}),
        patch("importlib.import_module") as mock_import,
    ):
        mock_settings.return_value.redis_url = "redis://localhost:6379/0"
        mock_module = mock_import.return_value
        mock_module.fake_func = mock_job

        result = await run_job("test-job")

    assert result == {"ok": True}
    mock_init_db.assert_awaited_once_with(min_size=1, max_size=3)
    mock_init_redis.assert_awaited_once_with("redis://localhost:6379/0")
    mock_close_db.assert_awaited_once()
    mock_close_redis.assert_awaited_once()


@pytest.mark.asyncio
async def test_cron_runner_closes_on_error():
    """close_db/close_redis должны вызываться даже при ошибке в job."""
    from scripts.cron_runner import run_job

    mock_close_db = AsyncMock()
    mock_close_redis = AsyncMock()
    mock_job = AsyncMock(side_effect=RuntimeError("job failed"))

    with (
        patch("app.services.database.init_db", AsyncMock()),
        patch("app.services.database.close_db", mock_close_db),
        patch("app.core.cache.init_redis", AsyncMock()),
        patch("app.core.cache.close_redis", mock_close_redis),
        patch("app.core.config.get_settings") as mock_settings,
        patch("scripts.cron_runner.COMMANDS", {"test-job": ("fake.module", "fake_func")}),
        patch("importlib.import_module") as mock_import,
    ):
        mock_settings.return_value.redis_url = "redis://localhost:6379/0"
        mock_module = mock_import.return_value
        mock_module.fake_func = mock_job

        with pytest.raises(RuntimeError, match="job failed"):
            await run_job("test-job")

    mock_close_db.assert_awaited_once()
    mock_close_redis.assert_awaited_once()


@pytest.mark.asyncio
async def test_should_alert_first_failure_in_window_suppressed():
    """Первый фейл за окно: SET NX вернул truthy (метка создана) → молчим."""
    from scripts.cron_runner import _should_alert_for_failure

    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)  # NX поставил метку — её не было

    with patch("app.core.cache.get_redis", AsyncMock(return_value=mock_redis)):
        should_alert = await _should_alert_for_failure("manifold-scrape")

    assert should_alert is False
    mock_redis.set.assert_awaited_once_with("cron:fail_seen:manifold-scrape", "1", nx=True, ex=2700)


@pytest.mark.asyncio
async def test_should_alert_repeated_failure_in_window():
    """Повторный фейл в окне: SET NX вернул None (метка уже была) → алертим."""
    from scripts.cron_runner import _should_alert_for_failure

    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=None)  # NX не сработал — метка уже есть

    with patch("app.core.cache.get_redis", AsyncMock(return_value=mock_redis)):
        should_alert = await _should_alert_for_failure("manifold-scrape")

    assert should_alert is True


@pytest.mark.asyncio
async def test_should_alert_fail_open_when_redis_unavailable():
    """Redis недоступен (None или исключение) → fail-open, алертим."""
    from scripts.cron_runner import _should_alert_for_failure

    # Случай 1: get_redis вернул None
    with patch("app.core.cache.get_redis", AsyncMock(return_value=None)):
        assert await _should_alert_for_failure("any-job") is True

    # Случай 2: get_redis кинул исключение
    with patch("app.core.cache.get_redis", AsyncMock(side_effect=ConnectionError("redis down"))):
        assert await _should_alert_for_failure("any-job") is True
