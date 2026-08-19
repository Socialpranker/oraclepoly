"""Wallet Resolver — единый SSoT для funder/signer/signature_type.

На Polymarket три типа отношений между ключом подписи (signer EOA) и
адресом-владельцем USDC (funder):

  signature_type | funder выводится из     | сценарий
  -------------- | ----------------------- | --------------------------
  0 (EOA)        | сам signer EOA          | EOA напрямую держит USDC
  1 (POLY_PROXY) | Magic/email derived     | старый Polymarket UI (Magic Link)
  2 (POLY_SAFE)  | Gnosis Safe derived     | новый Polymarket UI (Browser Wallet)

`POLYMARKET_SIGNATURE_TYPE` + `POLYMARKET_FUNDER_ADDRESS` — единственная пара,
которую читает бот. Если они не совпадают с твоим реальным wallet'ом на
Polymarket — `wallet=0` и live-режим заблокирован.

Memory: polymarket_wallets.md (актуальная связка), live_execution_audit_2026-04-29.md.
"""

from __future__ import annotations

import threading

import httpx
import structlog
from py_clob_client_v2 import ApiCreds, BalanceAllowanceParams, ClobClient

from app.core.config import get_settings

logger = structlog.get_logger(__name__)


def get_funder_address() -> str:
    """Адрес кошелька, который держит USDC и от имени которого исполняются ордера.

    Источник: settings.polymarket_address (env POLYMARKET_ADDRESS, legacy POLYMARKET_FUNDER).
    """
    s = get_settings()
    return s.polymarket_address


def get_signature_type() -> int:
    """signature_type для CLOB API. Должен соответствовать типу funder'а.

    Источник: settings.polymarket_signature_type (env POLYMARKET_SIGNATURE_TYPE, default=1).
    """
    s = get_settings()
    return getattr(s, "polymarket_signature_type", 1)


# endpoint-суффиксы, для которых ОБЯЗАТЕЛЕН прокси: только размещение ордера.
# Геофенс блокирует write (POST /order, /orders) с IP сервера; всё остальное
# (balance-allowance, /data/trades, /book — read) отвечает 200 с любого IP и
# должно идти НАПРЯМУЮ, чтобы не жечь платный residential-трафик IPRoyal.
_PROXY_ORDER_SUFFIXES = ("/order", "/orders")
# lock защищает подмену модуль-глобала _http_client внутри обёртки request():
# execution-джобы — отдельные процессы, но внутри одного процесса CLOB-вызовы
# могут идти из разных потоков (run_in_executor). Без lock'а параллельный read
# мог бы «увести» write на прямой клиент и наоборот.
_proxy_swap_lock = threading.Lock()


def _is_order_write(method: str, endpoint: str) -> bool:
    """True только для POST размещения ордера — единственный write, которому нужен прокси."""
    return method == "POST" and endpoint.rstrip("/").endswith(_PROXY_ORDER_SUFFIXES)


def install_clob_proxy_transport() -> bool:
    """Пробросить POLYMARKET_PROXY в HTTP-слой py_clob_client_v2 — ТОЛЬКО для write-ордеров.

    Геофенс Polymarket блокирует размещение ордеров с IP сервера (verified
    2026-06-25). Библиотека НЕ принимает proxy в конструкторе ClobClient — весь
    HTTP идёт через единственный модуль-глобал `_http_client` и единственную
    функцию `request()` в py_clob_client_v2.http_helpers.helpers.

    Раньше мы пересоздавали `_http_client` с proxy целиком — из-за этого ВСЕ
    вызовы (включая read: balance-allowance, /data/trades, /book) шли через
    платный residential-прокси и жгли трафик IPRoyal (~46 MB/сутки baseline +
    всплески от ежеминутных execution-джоб). Прокси нужен ТОЛЬКО для POST
    размещения ордера (write), read отвечает 200 с любого IP.

    Поэтому теперь оборачиваем `request()`: для `POST /order|/orders` подставляем
    proxy-клиент, для всего остального — прямой (оригинальный `_http_client`).
    `request()` — единственная точка, где известны и method, и endpoint.

    Идемпотентна: повторный вызов с тем же proxy не переустанавливает обёртку.
    No-op при пустом POLYMARKET_PROXY — read-path остаётся прямым.

    ⚠️ Патчит приватный API чужой либы (request + _http_client) — версия
    py_clob_client_v2 запинена, tests/test_clob_proxy_injection.py стережёт
    совместимость патч-точки.

    Returns:
        True если proxy-обёртка установлена, False если no-op (пустой proxy).
    """
    s = get_settings()
    if not s.polymarket_proxy:
        return False

    import py_clob_client_v2.http_helpers.helpers as _clob_helpers

    # уже стоит наша обёртка с тем же proxy — не переустанавливаем (идемпотентность)
    if getattr(_clob_helpers.request, "_oracle_proxy", None) == s.polymarket_proxy:
        return True

    original_request = getattr(_clob_helpers.request, "_oracle_original", _clob_helpers.request)
    proxy_client = httpx.Client(http2=True, proxy=s.polymarket_proxy, timeout=30)

    def _request_with_selective_proxy(endpoint, method, headers=None, data=None, params=None):
        if not _is_order_write(method, endpoint):
            # read / прочее — прямой клиент, прокси не трогаем
            return original_request(endpoint, method, headers=headers, data=data, params=params)
        # write-ордер — временно подменяем глобал на proxy-клиент
        with _proxy_swap_lock:
            direct_client = _clob_helpers._http_client
            _clob_helpers._http_client = proxy_client
            try:
                return original_request(endpoint, method, headers=headers, data=data, params=params)
            finally:
                _clob_helpers._http_client = direct_client

    _request_with_selective_proxy._oracle_proxy = s.polymarket_proxy
    _request_with_selective_proxy._oracle_original = original_request
    _clob_helpers.request = _request_with_selective_proxy
    logger.info("clob_proxy_installed", proxy_host=s.polymarket_proxy.split("@")[-1], scope="order_write_only")
    return True


def build_clob_client() -> ClobClient:
    """Канонический способ создания ClobClient — все вызовы должны идти через wallet_resolver.

    Заменяет хардкод signature_type=1 в polymarket_service.py / execution_job.py / execution_reconcile.py.
    """
    s = get_settings()
    install_clob_proxy_transport()
    client = ClobClient(
        host=s.polymarket_host,
        key=s.polymarket_private_key,
        chain_id=s.polymarket_chain_id,
        signature_type=get_signature_type(),
        funder=get_funder_address(),
    )
    if s.polymarket_api_key and s.polymarket_api_secret and s.polymarket_api_passphrase:
        client.set_api_creds(
            ApiCreds(
                api_key=s.polymarket_api_key,
                api_secret=s.polymarket_api_secret,
                api_passphrase=s.polymarket_api_passphrase,
            )
        )
    return client


async def fetch_balance() -> float | None:
    """USDC баланс funder'а через CLOB API. Возвращает None при ошибке.

    Единственный канонический путь к балансу. Все остальные места (api/bot.py,
    execution_job.check_balance, execution_reconcile) должны вызывать его.
    """
    import asyncio

    def _sync_fetch() -> float | None:
        try:
            client = build_clob_client()
            params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=get_signature_type())
            resp = client.get_balance_allowance(params)
            return round(float(resp.get("balance", 0)) / 1e6, 2)
        except Exception as e:
            logger.warning("wallet_balance_fetch_error", error=str(e)[:200])
            return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_fetch)


async def fetch_allowance() -> dict[str, float] | None:
    """USDC allowances funder'а на CTF Exchange и Neg Risk. None при ошибке.

    Если allowance == 0 — ордера не пройдут даже при положительном балансе.
    Polymarket UI Approve выставляет allowances одним кликом ("Funds Activated").
    """
    import asyncio

    def _sync_fetch() -> dict[str, float] | None:
        try:
            client = build_clob_client()
            params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=get_signature_type())
            resp = client.get_balance_allowance(params)
            raw = resp.get("allowances", {}) or {}
            return {addr: round(float(v) / 1e6, 2) for addr, v in raw.items()}
        except Exception as e:
            logger.warning("wallet_allowance_fetch_error", error=str(e)[:200])
            return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_fetch)
