"""Тесты мониторинга доступности сайта.

Инцидент 2026-07-29: хостовый nginx не поднялся после рестарта, 80/443 никто не
слушал, Telegram Mini App лежал 6 суток. Все существующие проверки смотрели внутрь
docker'а (там всё healthy) и промолчали.

Тесты фиксируют два свойства:
1. Джоба краснеет ровно на том, что было сломано (обрыв соединения, не-200, пустой HTML).
2. Алерт edge-triggered — одно сообщение на поломку, а не на каждом тике.
"""

from __future__ import annotations

import httpx
import pytest

from app.jobs.site_availability_monitor import _alert_on_edge, run_site_availability_monitor

FAIL = "🔴 упало"
OK = "✅ поднялось"


class FakeRedis:
    """Минимальный Redis: помнит значения и записанные TTL."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        self.ttls[key] = ex


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.core.cache.get_redis", _get_redis)
    return fake


@pytest.fixture
def sent(monkeypatch):
    """Перехват отправленных админу сообщений."""
    messages: list[str] = []

    async def _send(text: str) -> None:
        messages.append(text)

    monkeypatch.setattr("app.tasks.notifications.send_admin_message", _send)
    return messages


def _mock_transport(monkeypatch, handler) -> None:
    """Подменить сеть у httpx.AsyncClient, сохранив реальный код джобы."""
    real_init = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)


def _healthy(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/health":
        return httpx.Response(200, text="ok")
    return httpx.Response(200, text="<!doctype html>" + "x" * 2000)


# --- edge-дедуп -------------------------------------------------------------


async def _edge(failing: bool) -> str | None:
    return await _alert_on_edge("frontend", failing=failing, fail_msg=FAIL, ok_msg=OK)


@pytest.mark.asyncio
async def test_first_failure_alerts(redis):
    assert await _edge(failing=True) == FAIL


@pytest.mark.asyncio
async def test_repeated_failure_is_silent(redis):
    """Ядро задачи: сайт лежит 20 тиков подряд → ровно ОДНО сообщение."""
    assert await _edge(failing=True) == FAIL
    for _ in range(19):
        assert await _edge(failing=True) is None


@pytest.mark.asyncio
async def test_recovery_alerts_after_failure(redis):
    await _edge(failing=True)
    assert await _edge(failing=False) == OK


@pytest.mark.asyncio
async def test_healthy_first_run_is_silent(redis):
    """Первый прогон на живом сайте — не событие, молчим."""
    assert await _edge(failing=False) is None


@pytest.mark.asyncio
async def test_repeated_recovery_is_silent(redis):
    await _edge(failing=True)
    assert await _edge(failing=False) == OK
    for _ in range(5):
        assert await _edge(failing=False) is None


@pytest.mark.asyncio
async def test_state_key_has_no_ttl(redis):
    """TTL на ключе состояния вернул бы спам при длительной поломке."""
    await _edge(failing=True)
    key = "site:alert_state:frontend"
    assert redis.store[key] == "fail"
    assert redis.ttls[key] is None


@pytest.mark.asyncio
async def test_without_redis_failure_still_alerts(monkeypatch):
    """Redis лёг — сигнал о поломке важнее дедупа."""

    async def _no_redis():
        return None

    monkeypatch.setattr("app.core.cache.get_redis", _no_redis)
    assert await _edge(failing=True) == FAIL


@pytest.mark.asyncio
async def test_without_redis_recovery_is_silent(monkeypatch):
    """Без состояния recovery неотличим от «всегда было хорошо» — не шлём на каждом тике."""

    async def _no_redis():
        return None

    monkeypatch.setattr("app.core.cache.get_redis", _no_redis)
    assert await _edge(failing=False) is None


@pytest.mark.asyncio
async def test_frontend_and_api_dedup_independently(redis):
    """Разные проверки не гасят алерты друг друга."""
    assert await _alert_on_edge("frontend", failing=True, fail_msg=FAIL, ok_msg=OK) == FAIL
    assert await _alert_on_edge("api", failing=True, fail_msg=FAIL, ok_msg=OK) == FAIL


# --- сами проверки ----------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_site_passes(monkeypatch, redis, sent):
    _mock_transport(monkeypatch, _healthy)
    result = await run_site_availability_monitor()
    assert result["ok"] is True
    assert result["alerts_sent"] == 0
    assert sent == []


@pytest.mark.asyncio
async def test_connection_refused_is_caught(monkeypatch, redis, sent):
    """Ровно инцидент 29 июля: TLS-хендшейк рвётся, никто не слушает 443."""

    def _refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    _mock_transport(monkeypatch, _refused)
    result = await run_site_availability_monitor()

    assert result["ok"] is False
    assert result["checks"]["frontend"]["ok"] is False
    assert result["checks"]["api"]["ok"] is False
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_non_200_is_caught(monkeypatch, redis, sent):
    """502 от nginx при мёртвом апстриме."""
    _mock_transport(monkeypatch, lambda r: httpx.Response(502, text="Bad Gateway"))
    result = await run_site_availability_monitor()

    assert result["ok"] is False
    assert "502" in result["checks"]["frontend"]["detail"]


@pytest.mark.asyncio
async def test_empty_html_is_caught(monkeypatch, redis, sent):
    """200, но пустой HTML = белый экран у пользователя. API при этом здоров."""

    def _empty_html(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, text="ok")
        return httpx.Response(200, text="")

    _mock_transport(monkeypatch, _empty_html)
    result = await run_site_availability_monitor()

    assert result["ok"] is False
    assert result["checks"]["frontend"]["ok"] is False
    assert result["checks"]["api"]["ok"] is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_redirect_is_not_followed(monkeypatch, redis, sent):
    """Молчаливый редирект на заглушку хостера не должен считаться здоровьем."""
    _mock_transport(monkeypatch, lambda r: httpx.Response(302, headers={"location": "https://parked.example"}))
    result = await run_site_availability_monitor()

    assert result["ok"] is False
    assert "302" in result["checks"]["frontend"]["detail"]


@pytest.mark.asyncio
async def test_api_down_frontend_up(monkeypatch, redis, sent):
    """Backend лёг, статика жива — алерт только про API."""

    def _api_down(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, text="<!doctype html>" + "x" * 2000)

    _mock_transport(monkeypatch, _api_down)
    result = await run_site_availability_monitor()

    assert result["ok"] is False
    assert result["checks"]["frontend"]["ok"] is True
    assert result["checks"]["api"]["ok"] is False
    assert len(sent) == 1
    assert "API" in sent[0]


@pytest.mark.asyncio
async def test_outage_sends_once_across_ticks(monkeypatch, redis, sent):
    """Сквозной: сайт лежит 10 прогонов → 2 сообщения (фронт+API), не 20."""

    def _refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    _mock_transport(monkeypatch, _refused)
    for _ in range(10):
        await run_site_availability_monitor()

    assert len(sent) == 2


@pytest.mark.asyncio
async def test_alert_send_failure_does_not_crash_job(monkeypatch, redis):
    """TG недоступен — джоба обязана вернуть результат, а не упасть."""

    async def _boom(text: str) -> None:
        raise RuntimeError("telegram down")

    monkeypatch.setattr("app.tasks.notifications.send_admin_message", _boom)
    _mock_transport(monkeypatch, lambda r: httpx.Response(500, text="err"))

    result = await run_site_availability_monitor()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_registered_in_cron_runner():
    """Слаг должен быть в обоих реестрах — иначе джоба не запустится по расписанию."""
    from scripts.cron_runner import COMMANDS, JOB_DESCRIPTIONS

    assert COMMANDS["site-availability"] == (
        "app.jobs.site_availability_monitor",
        "run_site_availability_monitor",
    )
    assert "site-availability" in JOB_DESCRIPTIONS
