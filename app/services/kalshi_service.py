"""Kalshi REST client — Phase 1 MVP (read-only).

Source: research/kalshi-integration-2026-05/prd.md

Authentication: RSA-PSS SHA256 signing per Kalshi API spec.
Headers per request:
    KALSHI-ACCESS-KEY        = settings.kalshi_api_key_id
    KALSHI-ACCESS-TIMESTAMP  = milliseconds since epoch
    KALSHI-ACCESS-SIGNATURE  = base64(RSA-PSS-SHA256(timestamp + method + path))

Signing string: timestamp_str + HTTP_method + request_path (БЕЗ query params!).

This client covers read endpoints needed for Phase 1.0:
    - GET /markets (list all active)
    - GET /markets/{ticker}
    - GET /markets/{ticker}/orderbook
    - GET /markets/orderbooks (bulk)
    - GET /markets/trades
    - GET /events

Phase 1.0 does NOT implement order placement — execution comes in Phase 2.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import httpx
import structlog
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.config import get_settings
from app.core.retry import http_retry
from app.core.task_queue import debounce_acquire

logger = structlog.get_logger()


class KalshiAuthError(RuntimeError):
    """Raised когда private key не загружен или не валиден."""


_AUTH_ALERT_TTL_SEC = 6 * 3600  # раз в 6ч на джобу — авторизация не чинится сама


async def alert_kalshi_auth_failure(job: str, error: str) -> None:
    """TG-алерт о протухшем/отсутствующем ключе Kalshi. Debounce 6ч через Redis.

    Без алерта джобы молча возвращают status=skipped и ingestion Kalshi
    останавливается незаметно (у Polymarket есть api-health монитор, тут — нет).
    Никогда не бросает: алертинг не роняет джобу.
    """
    try:
        if not await debounce_acquire(f"kalshi_auth_alert:{job}", ttl=_AUTH_ALERT_TTL_SEC):
            return
        from app.tasks.notifications import send_admin_message

        await send_admin_message(
            f"🔑 Kalshi auth failure в {job}: {error[:200]}\nIngestion Kalshi остановлен до починки ключа."
        )
    except Exception as exc:  # noqa: BLE001 — алертинг не роняет джобу
        logger.warning("kalshi_auth_alert_failed", job=job, error=str(exc)[:150])


def _load_private_key(path: str) -> rsa.RSAPrivateKey:
    """Read PEM file and return RSAPrivateKey instance.

    Args:
        path: absolute path to PEM file (e.g. `/run/secrets/kalshi_rsa.pem`).

    Raises:
        KalshiAuthError: если файла нет или ключ не RSA.
    """
    pem_path = Path(path)
    if not pem_path.exists():
        raise KalshiAuthError(f"Kalshi private key not found at {path}")
    pem_bytes = pem_path.read_bytes()
    try:
        key = serialization.load_pem_private_key(pem_bytes, password=None)
    except Exception as exc:
        raise KalshiAuthError(f"Failed to parse Kalshi PEM: {exc}") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise KalshiAuthError(f"Kalshi PEM is not RSA (got {type(key).__name__})")
    return key


def sign_request(
    private_key: rsa.RSAPrivateKey,
    timestamp_ms: int,
    method: str,
    path: str,
) -> str:
    """Sign timestamp+method+path with RSA-PSS SHA256, return base64.

    Per Kalshi spec: path does NOT include query string.
    method must be uppercase (e.g. "GET").
    """
    message = f"{timestamp_ms}{method.upper()}{path}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def _strip_query(path_or_url: str) -> str:
    """Return path component without query string for signing."""
    if "?" in path_or_url:
        return path_or_url.split("?", 1)[0]
    return path_or_url


class KalshiClient:
    """Async Kalshi REST client. Lazy-loads private key on first request.

    Usage:
        async with KalshiClient() as client:
            markets = await client.list_markets(limit=200)
    """

    def __init__(self, settings_override: Any = None) -> None:
        self.settings = settings_override or get_settings()
        self.base_url = self.settings.kalshi_api_base_url.rstrip("/")
        self.timeout = self.settings.kalshi_timeout_sec
        self._private_key: rsa.RSAPrivateKey | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def private_key(self) -> rsa.RSAPrivateKey:
        if self._private_key is None:
            self._private_key = _load_private_key(self.settings.kalshi_private_key_path)
        return self._private_key

    async def __aenter__(self) -> KalshiClient:
        self._client = httpx.AsyncClient(timeout=self.timeout, base_url=self.base_url)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Build KALSHI-ACCESS-* headers for a single request."""
        timestamp_ms = int(time.time() * 1000)
        signature = sign_request(self.private_key, timestamp_ms, method, _strip_query(path))
        return {
            "KALSHI-ACCESS-KEY": self.settings.kalshi_api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Accept": "application/json",
        }

    @http_retry
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send authenticated GET, return parsed JSON."""
        if self._client is None:
            raise RuntimeError("KalshiClient not entered (use 'async with KalshiClient()')")
        headers = self._auth_headers("GET", path)
        resp = await self._client.get(path, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # ─── Public Phase 1.0 methods ────────────────────────────────────────────

    async def list_markets(
        self,
        *,
        limit: int = 200,
        cursor: str | None = None,
        status: str = "open",
        event_ticker: str | None = None,
    ) -> dict[str, Any]:
        """GET /markets — paginated list.

        Args:
            limit: per page (max 1000 per Kalshi docs)
            cursor: pagination cursor from previous response
            status: 'open' | 'closed' | 'settled' (default: only open)
            event_ticker: filter to single event

        Returns:
            {"markets": [...], "cursor": str | None}
        """
        params: dict[str, Any] = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        return await self._get("/markets", params=params)

    async def list_markets_all(
        self,
        *,
        status: str = "open",
        max_pages: int = 50,
        page_limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Paginate /markets until cursor exhausted or max_pages reached.

        Conservative max_pages prevents infinite loop on broken cursors.
        """
        all_markets: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            resp = await self.list_markets(limit=page_limit, cursor=cursor, status=status)
            page = resp.get("markets", [])
            all_markets.extend(page)
            cursor = resp.get("cursor") or None
            if not cursor or not page:
                break
        return all_markets

    async def get_market(self, ticker: str) -> dict[str, Any]:
        """GET /markets/{ticker}."""
        return await self._get(f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        """GET /markets/{ticker}/orderbook."""
        return await self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    async def get_orderbooks_bulk(self, tickers: list[str]) -> dict[str, Any]:
        """GET /markets/orderbooks — bulk до 100 tickers per request.

        Caller отвечает за разбиение на чанки если >100.
        """
        if len(tickers) > 100:
            raise ValueError(f"Bulk endpoint capped at 100 tickers (got {len(tickers)})")
        return await self._get("/markets/orderbooks", params={"tickers": ",".join(tickers)})

    async def get_trades(
        self,
        *,
        ticker: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /markets/trades — all trades or filtered by ticker."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if ticker:
            params["ticker"] = ticker
        return await self._get("/markets/trades", params=params)

    async def list_events(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        status: str = "open",
    ) -> dict[str, Any]:
        """GET /events — list events (groups of markets)."""
        params: dict[str, Any] = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/events", params=params)
