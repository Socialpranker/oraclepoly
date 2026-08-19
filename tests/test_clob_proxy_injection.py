"""Тесты селективной инъекции прокси в HTTP-слой py_clob_client_v2.

Геофенс Polymarket блокирует размещение ордеров с IP сервера (verified
2026-06-25 через polymarket.com/api/geoblock). Библиотека py_clob_client_v2 НЕ
принимает proxy в конструкторе ClobClient — весь HTTP идёт через единственный
модуль-глобал `_http_client = httpx.Client(http2=True)` и единственную функцию
`request()` в http_helpers.helpers.

Прокси нужен ТОЛЬКО для write размещения ордера (POST /order, /orders) —
read-эндпоинты (balance-allowance, /data/trades, /book) отвечают 200 с любого IP
и должны идти НАПРЯМУЮ, чтобы не жечь платный residential-трафик IPRoyal
(инцидент 2026-06-28: ежеминутные execution-джобы гнали read через прокси).

Поэтому проброс реализован как обёртка `request()`, которая выбирает proxy-клиент
только для POST /order|/orders. Тест проверяет:
  1. при заданном polymarket_proxy функция request() заменяется обёрткой;
  2. write-ордер (POST /order) уходит через proxy-клиент;
  3. read (GET balance-allowance/trades/book) идёт через прямой _http_client;
  4. при пустом polymarket_proxy ничего не трогаем (read-path не ломаем);
  5. идемпотентность: повторный install не плодит обёртки;
  6. guard: приватный API либы (_http_client + request) ещё существует.
"""

from __future__ import annotations

import httpx
import py_clob_client_v2.http_helpers.helpers as clob_helpers
import pytest

from app.core.config import Settings
from app.services.wallet_resolver import _is_order_write, install_clob_proxy_transport

PROXY_URL = "http://user:pass@proxy.example.com:8080"


@pytest.fixture(autouse=True)
def _isolate_clob_globals():
    """Снимок/восстановление модуль-глобалов py_clob_client_v2 вокруг каждого теста.

    `request` и `_http_client` — process-global мутируемое состояние; другой тест
    в общем прогоне мог оставить их подменёнными на РЕАЛЬНЫЙ прокси из .env
    (install_clob_proxy_transport вызывается реальным execution-кодом). Без этой
    изоляции read-тест ловит чужой geo.iproyal-клиент.
    """
    saved_request = getattr(clob_helpers.request, "_oracle_original", clob_helpers.request)
    saved_client = clob_helpers._http_client
    # вход в тест — гарантированно чистый baseline. trust_env=False, иначе httpx
    # подхватит HTTPS_PROXY из окружения (env-прокси) и "прямой" клиент окажется
    # проксированным — тест ложно увидит proxy-host на read-пути.
    clob_helpers.request = saved_request
    clob_helpers._http_client = httpx.Client(http2=True, trust_env=False)
    yield
    clob_helpers.request = saved_request
    clob_helpers._http_client = saved_client


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        database_url="postgresql://x:x@localhost/x",
        openai_api_key="sk-x",
        redis_url="redis://localhost",
        telegram_bot_token="1:x",
        admin_telegram_ids=[1],
        admin_api_key="x",
        polymarket_api_key="x",
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def _restore(original_request, original_client):
    clob_helpers.request = original_request
    clob_helpers._http_client = original_client


def _proxy_host_of(client: httpx.Client) -> str | None:
    """Достаёт host proxy-mount'а из httpx-клиента (None если прокси нет)."""
    for transport in client._mounts.values():
        pool = getattr(transport, "_pool", None)
        proxy_url = getattr(pool, "_proxy_url", None)
        if proxy_url is not None:
            host = proxy_url.host
            return host.decode() if isinstance(host, bytes) else host
    return None


def test_library_still_exposes_patch_points():
    """Guard: патч-точки _http_client и request существуют. Падёт если либа обновилась."""
    assert hasattr(clob_helpers, "_http_client")
    assert isinstance(clob_helpers._http_client, httpx.Client)
    assert callable(getattr(clob_helpers, "request", None))


def test_is_order_write_classification():
    """Только POST /order и /orders считаются write-ордером."""
    assert _is_order_write("POST", "https://clob.polymarket.com/order") is True
    assert _is_order_write("POST", "https://clob.polymarket.com/orders") is True
    assert _is_order_write("POST", "https://clob.polymarket.com/order/") is True
    # read и прочие POST — не write-ордер
    assert _is_order_write("GET", "https://clob.polymarket.com/balance-allowance") is False
    assert _is_order_write("GET", "https://clob.polymarket.com/data/trades") is False
    assert _is_order_write("GET", "https://clob.polymarket.com/book") is False
    assert _is_order_write("POST", "https://clob.polymarket.com/order-book") is False
    assert _is_order_write("DELETE", "https://clob.polymarket.com/order") is False


def test_wrapper_installed_when_configured(monkeypatch):
    """С заданным polymarket_proxy request() заменяется обёрткой, _http_client не трогаем."""
    original_request = clob_helpers.request
    original_client = clob_helpers._http_client
    try:
        s = _make_settings(polymarket_proxy=PROXY_URL)
        monkeypatch.setattr("app.services.wallet_resolver.get_settings", lambda: s)

        installed = install_clob_proxy_transport()

        assert installed is True
        assert clob_helpers.request is not original_request
        assert getattr(clob_helpers.request, "_oracle_proxy", None) == PROXY_URL
        # прямой клиент (read-path) НЕ заменён на proxy
        assert clob_helpers._http_client is original_client
    finally:
        _restore(original_request, original_client)


def _install_with_probe(monkeypatch, captured: dict):
    """Перед install подменяем библиотечную request на пробу, которая фиксирует,
    КАКОЙ клиент (_http_client) активен в момент её вызова. Обёртка install'а
    захватит пробу как _oracle_original и вызовет именно её — и для read (с прямым
    _http_client), и для write (под swap'нутым proxy-клиентом). Так проверяем выбор
    транспорта детерминированно, без сети и без патча класса httpx.Client.

    baseline (чистый request + direct-клиент) гарантирован фикстурой _isolate_clob_globals.
    """
    s = _make_settings(polymarket_proxy=PROXY_URL)
    monkeypatch.setattr("app.services.wallet_resolver.get_settings", lambda: s)

    original_request = clob_helpers.request
    original_client = clob_helpers._http_client

    def _probe(endpoint, method, headers=None, data=None, params=None):
        captured["client_proxy_host"] = _proxy_host_of(clob_helpers._http_client)
        captured["method"] = method
        captured["endpoint"] = endpoint
        return {}

    clob_helpers.request = _probe
    install_clob_proxy_transport()
    return original_request, original_client


def test_write_order_uses_proxy_client(monkeypatch):
    """POST /order: под swap'ом активен proxy-клиент (host = proxy.example.com)."""
    captured: dict = {}
    original_request, original_client = _install_with_probe(monkeypatch, captured)
    try:
        clob_helpers.request("https://clob.polymarket.com/order", "POST", data="{}")
        assert captured["client_proxy_host"] == "proxy.example.com", "write-ордер должен идти под proxy-клиентом"
    finally:
        _restore(original_request, original_client)


def test_read_uses_direct_client(monkeypatch):
    """GET balance-allowance/trades/book: активен прямой _http_client (без proxy)."""
    captured: dict = {}
    original_request, original_client = _install_with_probe(monkeypatch, captured)
    try:
        for ep in (
            "https://clob.polymarket.com/balance-allowance",
            "https://clob.polymarket.com/data/trades",
            "https://clob.polymarket.com/book",
        ):
            clob_helpers.request(ep, "GET")
            assert captured["client_proxy_host"] is None, f"read {ep} должен идти под прямым клиентом (без proxy)"
    finally:
        _restore(original_request, original_client)


def test_no_op_when_proxy_empty(monkeypatch):
    """Пустой polymarket_proxy → request НЕ оборачиваем (read-path не ломаем)."""
    original_request = clob_helpers.request
    original_client = clob_helpers._http_client
    try:
        s = _make_settings(polymarket_proxy="")
        monkeypatch.setattr("app.services.wallet_resolver.get_settings", lambda: s)

        installed = install_clob_proxy_transport()

        assert installed is False
        assert clob_helpers.request is original_request, "request не должен меняться"
        assert clob_helpers._http_client is original_client
    finally:
        _restore(original_request, original_client)


def test_idempotent_install(monkeypatch):
    """Повторный вызов с тем же proxy не плодит обёртки бесконечно."""
    original_request = clob_helpers.request
    original_client = clob_helpers._http_client
    try:
        s = _make_settings(polymarket_proxy=PROXY_URL)
        monkeypatch.setattr("app.services.wallet_resolver.get_settings", lambda: s)

        install_clob_proxy_transport()
        first = clob_helpers.request
        install_clob_proxy_transport()
        second = clob_helpers.request

        assert first is second, "идемпотентность: обёртка не переустанавливается"
        # под обёрткой по-прежнему оригинальный request, а не обёртка над обёрткой
        assert getattr(second, "_oracle_original") is original_request
    finally:
        _restore(original_request, original_client)
