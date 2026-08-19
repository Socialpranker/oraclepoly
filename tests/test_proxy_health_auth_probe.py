"""Тесты auth-проверки прокси через реальный CLOB-путь (proxy_health_check).

Инцидент 2026-07-18..21: `407 Proxy Authentication Required` от IPRoyal при
размещении ордера (v2-execution.log) — прокси отверг credentials на write-пути.
Существующий TCP-чек (_tcp_connect) видит только открыт ли порт, не проверяет
авторизацию; существующий IPRoyal API-чек (_check_iproyal_api) бьёт в
resi-api.iproyal.com напрямую, не через сам прокси. Ни один не поймал бы 407,
случившийся именно на прокси-туннелируемом запросе к CLOB.

_proxy_auth_probe гоняет лёгкий GET {polymarket_host}/time ЧЕРЕЗ прокси-клиент —
тот же путь, которым реально идут write-ордера (см. wallet_resolver.py:106
proxy_client = httpx.Client(http2=True, proxy=...)).
"""
from __future__ import annotations

import httpx
import pytest

from app.jobs.proxy_health_check import _proxy_auth_probe


@pytest.mark.asyncio
async def test_proxy_auth_probe_true_on_200(monkeypatch):
    """CLOB отвечает 200 через прокси-туннель → auth в порядке."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"epoch": 123})

    monkeypatch.setattr(
        "app.jobs.proxy_health_check._build_probe_client",
        lambda proxy_url: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await _proxy_auth_probe("http://user:pass@proxy.example.com:8080") is True


@pytest.mark.asyncio
async def test_proxy_auth_probe_false_on_407(monkeypatch):
    """Прокси отвечает 407 Proxy Authentication Required → auth сломан (не сеть)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(407, text="Proxy Authentication Required")

    monkeypatch.setattr(
        "app.jobs.proxy_health_check._build_probe_client",
        lambda proxy_url: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await _proxy_auth_probe("http://user:pass@proxy.example.com:8080") is False


@pytest.mark.asyncio
async def test_proxy_auth_probe_false_on_transport_error(monkeypatch):
    """Полный обрыв соединения (не 407) тоже считается сбоем проверки — fail-closed."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        "app.jobs.proxy_health_check._build_probe_client",
        lambda proxy_url: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await _proxy_auth_probe("http://user:pass@proxy.example.com:8080") is False


@pytest.mark.asyncio
async def test_proxy_auth_probe_true_on_unrelated_4xx(monkeypatch):
    """404/иное — эндпоинт ответил, значит auth прошёл; не наша забота здесь."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    monkeypatch.setattr(
        "app.jobs.proxy_health_check._build_probe_client",
        lambda proxy_url: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await _proxy_auth_probe("http://user:pass@proxy.example.com:8080") is True
