"""Глобальная конфигурация pytest — тестовые env vars."""

import os


def pytest_configure(config):
    """Установить тестовые env vars ДО импорта приложения."""
    _test_env = {
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "REDIS_URL": "redis://localhost:6379/1",
        "TELEGRAM_BOT_TOKEN": "test-token",
        "ADMIN_TELEGRAM_IDS": "[]",
        "ADMIN_API_KEY": "test-admin-key",
        "POLYMARKET_API_KEY": "test-poly-key",
        "JWT_SECRET_KEY": "test-jwt-secret-key-32chars-or-more-for-validator",
    }
    for key, value in _test_env.items():
        os.environ.setdefault(key, value)
