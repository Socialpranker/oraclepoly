"""Tests для app.jobs.polymarket_api_health и app.core.retry host detection."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.retry import _is_polymarket_host, _is_retryable_http_error
from app.jobs.polymarket_api_health import (
    ALERT_RATIO,
    API_ERROR_ALERT_DEBOUNCE_KEY,
    API_ERROR_ALERT_DEBOUNCE_SEC,
    MIN_ERRORS_ABS,
    WS_ALERT_DEBOUNCE_KEY,
    WS_ALERT_DEBOUNCE_SEC,
    WS_STALE_THRESHOLD_SEC,
    _check_ws_freshness,
    _format_alert,
    _hour_bucket,
)


def test_is_polymarket_host_data_api():
    assert _is_polymarket_host("https://data-api.polymarket.com/trades?market=0x123")


def test_is_polymarket_host_gamma():
    assert _is_polymarket_host("https://gamma-api.polymarket.com/markets")


def test_is_polymarket_host_clob():
    assert _is_polymarket_host("https://clob.polymarket.com/book")


def test_is_polymarket_host_rejects_other():
    assert not _is_polymarket_host("https://api.openai.com/v1/chat")
    assert not _is_polymarket_host("https://manifold.markets/api")


def test_is_polymarket_host_safe_on_malformed():
    """Garbage input → False, не throw."""
    assert not _is_polymarket_host("not-a-url")
    assert not _is_polymarket_host("")


def test_retryable_429_polymarket_still_retryable():
    """429 от Polymarket — retry + counter (side-effect tested separately)."""
    request = httpx.Request("GET", "https://data-api.polymarket.com/trades")
    response = httpx.Response(429, request=request)
    exc = httpx.HTTPStatusError("rate limited", request=request, response=response)
    assert _is_retryable_http_error(exc) is True


def test_retryable_500_polymarket():
    request = httpx.Request("GET", "https://gamma-api.polymarket.com/markets")
    response = httpx.Response(503, request=request)
    exc = httpx.HTTPStatusError("svc unavailable", request=request, response=response)
    assert _is_retryable_http_error(exc) is True


def test_not_retryable_404():
    """404 — не retry (это не transient)."""
    request = httpx.Request("GET", "https://data-api.polymarket.com/trades")
    response = httpx.Response(404, request=request)
    exc = httpx.HTTPStatusError("not found", request=request, response=response)
    assert _is_retryable_http_error(exc) is False


def test_hour_bucket_format():
    """YYYYMMDDHH — fixed-width, sortable lexicographically."""
    from datetime import UTC, datetime

    dt = datetime(2026, 5, 24, 16, 35, 12, tzinfo=UTC)
    assert _hour_bucket(dt) == "2026052416"


def test_format_alert_contains_breakdown_and_baseline():
    """Alert message включает per-status breakdown, baseline, threshold."""
    msg = _format_alert(
        last_hour_errors={429: 312, 500: 18},
        baseline_avg=15.4,
        n_baseline_hours=72,
        threshold_used=46.2,
    )
    assert "<b>312</b>" in msg
    assert "<b>18</b>" in msg
    assert "HTTP 429" in msg
    assert "HTTP 500" in msg
    assert "15.4" in msg
    assert "46.2" in msg
    assert "72h" in msg


def test_thresholds_reasonable():
    """Sanity check на constants — чтобы случайно не сделать spam-config."""
    assert MIN_ERRORS_ABS >= 10  # < 10 = noise
    assert MIN_ERRORS_ABS <= 100  # > 100 = пропустим real degradation
    assert ALERT_RATIO >= 2.0  # < 2x — слишком чутко
    assert ALERT_RATIO <= 10.0  # > 10x — пропустим medium-severity


# === WS freshness (audit second-tier: WS ping/heartbeat-алерт) ===


def test_ws_freshness_constants_reasonable():
    assert WS_STALE_THRESHOLD_SEC == 120
    assert WS_ALERT_DEBOUNCE_SEC == 1800
    assert WS_ALERT_DEBOUNCE_KEY == "alert:ws_stale"


@pytest.mark.asyncio
async def test_check_ws_freshness_no_heartbeat_yet(monkeypatch):
    """Ключ отсутствует (WS ещё ни разу не писал) → no_data, без алёрта."""
    import app.jobs.polymarket_api_health as mod

    monkeypatch.setattr(mod, "cache_get", AsyncMock(return_value=None))
    notify_mock = AsyncMock()
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    result = await _check_ws_freshness()

    assert result == {"status": "no_data"}
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_ws_freshness_fresh_no_alert(monkeypatch):
    """Последнее сообщение недавно (< 120с) → ok, без алёрта."""
    import app.jobs.polymarket_api_health as mod

    monkeypatch.setattr(mod, "cache_get", AsyncMock(return_value={"ts": time.time() - 30}))
    notify_mock = AsyncMock()
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    result = await _check_ws_freshness()

    assert result["status"] == "ok"
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_ws_freshness_stale_sends_alert(monkeypatch):
    """Молчание > 120с и debounce свободен → TG-алерт с текстом про WS."""
    import app.jobs.polymarket_api_health as mod

    monkeypatch.setattr(mod, "cache_get", AsyncMock(return_value={"ts": time.time() - 200}))
    monkeypatch.setattr(mod, "debounce_acquire", AsyncMock(return_value=True))
    notify_mock = AsyncMock()
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    result = await _check_ws_freshness()

    assert result["status"] == "stale_alerted"
    notify_mock.assert_awaited_once()
    text = notify_mock.await_args.args[0]
    assert "Polymarket WS молчит" in text
    assert ">2 мин" in text


@pytest.mark.asyncio
async def test_check_ws_freshness_stale_but_debounced_no_alert(monkeypatch):
    """Молчание > 120с, но debounce уже занят (алёрт слался < 30 мин назад) →
    статус stale_debounced, повторный алёрт НЕ шлётся."""
    import app.jobs.polymarket_api_health as mod

    monkeypatch.setattr(mod, "cache_get", AsyncMock(return_value={"ts": time.time() - 500}))
    monkeypatch.setattr(mod, "debounce_acquire", AsyncMock(return_value=False))
    notify_mock = AsyncMock()
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    result = await _check_ws_freshness()

    assert result["status"] == "stale_debounced"
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_ws_freshness_debounce_uses_correct_key_and_ttl(monkeypatch):
    import app.jobs.polymarket_api_health as mod

    monkeypatch.setattr(mod, "cache_get", AsyncMock(return_value={"ts": time.time() - 300}))
    debounce_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(mod, "debounce_acquire", debounce_mock)
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", AsyncMock())

    await _check_ws_freshness()

    debounce_mock.assert_awaited_once_with(WS_ALERT_DEBOUNCE_KEY, WS_ALERT_DEBOUNCE_SEC)


@pytest.mark.asyncio
async def test_run_polymarket_api_health_check_includes_ws_freshness(monkeypatch):
    """run_polymarket_api_health_check прокидывает ws_freshness в результат,
    независимо от error-rate ветки (ok или alert)."""
    import app.jobs.polymarket_api_health as mod

    class _FakeRedis:
        async def scan_iter(self, match, count):
            return
            yield  # pragma: no cover — делает генератор пустым async iterator

        async def mget(self, *keys):
            return []

    monkeypatch.setattr(mod, "get_redis", AsyncMock(return_value=_FakeRedis()))
    monkeypatch.setattr(mod, "cache_get", AsyncMock(return_value={"ts": time.time() - 30}))

    result = await mod.run_polymarket_api_health_check()

    assert result["status"] == "ok"
    assert result["ws_freshness"]["status"] == "ok"


@pytest.mark.asyncio
async def test_run_polymarket_api_health_check_ws_check_failure_does_not_crash(monkeypatch):
    """Если _check_ws_freshness падает (Redis flap) — error-rate мониторинг
    всё равно отрабатывает, ws_freshness помечается check_failed."""
    import app.jobs.polymarket_api_health as mod

    class _FakeRedis:
        async def scan_iter(self, match, count):
            return
            yield  # pragma: no cover

        async def mget(self, *keys):
            return []

    monkeypatch.setattr(mod, "get_redis", AsyncMock(return_value=_FakeRedis()))
    monkeypatch.setattr(mod, "cache_get", AsyncMock(side_effect=RuntimeError("redis flap")))

    result = await mod.run_polymarket_api_health_check()

    assert result["status"] == "ok"
    assert result["ws_freshness"] == {"status": "check_failed"}


# === Error-rate alert debounce ===


class _FakeRedisAlert:
    """Первый scan_iter (last-hour bucket) отдаёт errors >= MIN_ERRORS_ABS,
    все последующие (baseline window) — пусто."""

    def __init__(self):
        self.call_count = 0

    async def scan_iter(self, match, count):
        self.call_count += 1
        keys = [b"polymarket:api:errors:x:429"] if self.call_count == 1 else []
        for k in keys:
            yield k

    async def mget(self, *keys):
        return [b"50"] if self.call_count == 1 else []


def test_error_alert_debounce_constants_reasonable():
    assert API_ERROR_ALERT_DEBOUNCE_SEC == 3 * 3600
    assert API_ERROR_ALERT_DEBOUNCE_KEY == "alert:api_errors_degraded"


@pytest.mark.asyncio
async def test_run_polymarket_api_health_check_alert_sends_when_debounce_free(monkeypatch):
    """Error-rate выше threshold, debounce свободен → TG-алерт уходит."""
    import app.jobs.polymarket_api_health as mod

    monkeypatch.setattr(mod, "get_redis", AsyncMock(return_value=_FakeRedisAlert()))
    monkeypatch.setattr(mod, "cache_get", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "debounce_acquire", AsyncMock(return_value=True))
    monkeypatch.setattr(mod, "get_settings", lambda: type("S", (), {"admin_telegram_ids": [123]})())
    send_mock = AsyncMock()
    monkeypatch.setattr("app.api.telegram.send_telegram_message", send_mock)

    result = await mod.run_polymarket_api_health_check()

    assert result["status"] == "alert"
    send_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_polymarket_api_health_check_alert_debounced_skips_send(monkeypatch):
    """Error-rate выше threshold, но debounce занят (алёрт слался < 3ч назад) →
    статус alert_debounced, повторный TG-алёрт НЕ шлётся."""
    import app.jobs.polymarket_api_health as mod

    monkeypatch.setattr(mod, "get_redis", AsyncMock(return_value=_FakeRedisAlert()))
    monkeypatch.setattr(mod, "cache_get", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "debounce_acquire", AsyncMock(return_value=False))
    monkeypatch.setattr(mod, "get_settings", lambda: type("S", (), {"admin_telegram_ids": [123]})())
    send_mock = AsyncMock()
    monkeypatch.setattr("app.api.telegram.send_telegram_message", send_mock)

    result = await mod.run_polymarket_api_health_check()

    assert result["status"] == "alert_debounced"
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_polymarket_api_health_check_alert_debounce_uses_correct_key_and_ttl(monkeypatch):
    import app.jobs.polymarket_api_health as mod

    monkeypatch.setattr(mod, "get_redis", AsyncMock(return_value=_FakeRedisAlert()))
    monkeypatch.setattr(mod, "cache_get", AsyncMock(return_value=None))
    debounce_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(mod, "debounce_acquire", debounce_mock)
    monkeypatch.setattr(mod, "get_settings", lambda: type("S", (), {"admin_telegram_ids": [123]})())
    monkeypatch.setattr("app.api.telegram.send_telegram_message", AsyncMock())

    await mod.run_polymarket_api_health_check()

    debounce_mock.assert_awaited_once_with(API_ERROR_ALERT_DEBOUNCE_KEY, API_ERROR_ALERT_DEBOUNCE_SEC)
