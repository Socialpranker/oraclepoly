"""Тесты expiry-гейта proxy_health_check.

Инцидент 2026-07-13: IPRoyal Proxy Monitor слал «Прокси-сессия истекла Nч назад»
ежечасно 3 суток, хотя прокси ФАКТИЧЕСКИ жив (health-job proxy_alive=true, geoblock
обходится). Причина: expiry-алерт опирался ТОЛЬКО на устаревшую дату PROXY_EXPIRES_AT
в .env, игнорируя реальное состояние. Фикс: не слать expiry-алерт, если прокси
считается живым (реальный TCP/Redis-чек побеждает вручную вбитую дату).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.jobs.proxy_health_check import _check_session_expiry, _should_alert_expiry


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_check_session_expiry_past_returns_alert():
    """Дата в прошлом → строка алерта (сама детекция не меняется)."""
    past = datetime.now(UTC) - timedelta(hours=78)
    msg = _check_session_expiry(_iso(past))
    assert msg is not None
    assert "истекла" in msg


def test_check_session_expiry_future_returns_none():
    """Дата далеко в будущем → нет алерта."""
    future = datetime.now(UTC) + timedelta(days=5)
    assert _check_session_expiry(_iso(future)) is None


def test_should_alert_expiry_suppressed_when_proxy_alive():
    """Дата истекла, НО прокси жив (реальный чек) → алерт подавлен."""
    past = datetime.now(UTC) - timedelta(hours=78)
    assert _should_alert_expiry(_iso(past), proxy_alive=True) is None


def test_should_alert_expiry_fires_when_proxy_dead():
    """Дата истекла И прокси мёртв → алерт (реальная проблема)."""
    past = datetime.now(UTC) - timedelta(hours=78)
    msg = _should_alert_expiry(_iso(past), proxy_alive=False)
    assert msg is not None
    assert "истекла" in msg


def test_should_alert_expiry_none_when_not_expired():
    """Не истекла → нет алерта независимо от alive."""
    future = datetime.now(UTC) + timedelta(days=5)
    assert _should_alert_expiry(_iso(future), proxy_alive=False) is None
