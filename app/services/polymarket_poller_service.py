"""
Polymarket Trade Poller Service

Polls Polymarket APIs for recent trades, fetches market data (OI, order book),
and resolves conditionIds to internal market UUIDs.

APIs:
  GET https://data-api.polymarket.com/trades (работает, no auth)
  GET https://gamma-api.polymarket.com/markets (замена мёртвого data-api, no auth)
  GET https://clob.polymarket.com/book (order book, no auth)
  Holders/Positions/Leaderboard — из внутренней БД (data-api endpoints мертвы)
"""

import asyncio
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
import pybreaker
import structlog

from app.core.circuit_breaker import polymarket_breaker
from app.core.retry import http_retry
from app.services.database import fetchval

logger = structlog.get_logger()

TRADES_API_URL = "https://data-api.polymarket.com/trades"
CLOB_API_URL = "https://clob.polymarket.com"
# Gamma API — рабочая замена для data-api (который возвращает 404)
GAMMA_API_URL = "https://gamma-api.polymarket.com"
GAMMA_MARKETS_URL = f"{GAMMA_API_URL}/markets"
GAMMA_EVENTS_URL = f"{GAMMA_API_URL}/events"
WHALE_THRESHOLD_USD = Decimal("1000")


class PolymarketPollerService:
    """
    Polls Polymarket Data API for trades, filters whales, and normalizes
    data for the transaction-processing pipeline.
    """

    def __init__(self) -> None:
        self.api_url = TRADES_API_URL

    async def fetch_recent_trades(self, limit: int = 1000, offset: int = 0) -> list[dict[str, Any]]:
        """
        Fetch recent trades from the Polymarket Data API.

        Protected by Circuit Breaker -- returns empty list when circuit is OPEN.
        Protected by Retry -- retries on transient HTTP errors.

        Note:
            The Polymarket Data API does not support ``startDate`` filtering,
            so time-windowing is **not** performed here.  Instead, the caller
            (the cron job) controls the effective time window via ``limit`` /
            ``offset`` pagination and deduplicates already-processed trades.

        Args:
            limit: Maximum trades to fetch (API max 1000).
            offset: Pagination offset (API max 3000).

        Returns:
            List of raw trade dicts from the API, or empty list on failure.
        """
        try:
            return await self._fetch_trades_with_resilience(limit, offset)
        except pybreaker.CircuitBreakerError:
            logger.error(
                "trade_poller_circuit_open",
                msg="Circuit breaker is OPEN, returning empty data",
            )
            return []
        except Exception as e:
            logger.error("trade_poller_fetch_error", error=str(e), error_type=type(e).__name__)
            return []

    @polymarket_breaker
    @http_retry
    async def _fetch_trades_with_resilience(self, limit: int, offset: int) -> list[dict[str, Any]]:
        """Internal method with Circuit Breaker and Retry."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                self.api_url,
                params={
                    "limit": limit,
                    "offset": offset,
                    "filterType": "CASH",
                    "filterAmount": float(WHALE_THRESHOLD_USD),
                },
            )
            response.raise_for_status()
            return response.json()

    async def fetch_trades_since(self, after_ts: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """Fetch trades after timestamp. Timestamp cursor pagination."""
        try:
            return await self._fetch_trades_since_with_resilience(after_ts, limit)
        except pybreaker.CircuitBreakerError:
            logger.error("trade_poller_circuit_open")
            return []
        except Exception as e:
            logger.error("trade_poller_fetch_since_error", error=str(e))
            return []

    @polymarket_breaker
    @http_retry
    async def _fetch_trades_since_with_resilience(self, after_ts: str | None, limit: int) -> list[dict[str, Any]]:
        """Internal method with Circuit Breaker and Retry."""
        params: dict[str, Any] = {"limit": limit}
        if after_ts:
            params["after"] = after_ts

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(self.api_url, params=params)
            response.raise_for_status()
            return response.json()

    async def fetch_portfolio_value(self, proxy_address: str) -> float | None:
        """
        Fetch total position value из внутренней БД.

        Data API (value endpoint) мёртв. Оцениваем bankroll из total_volume в whales.

        Returns:
            Total portfolio value in USD, or None on failure.
        """
        try:
            total_volume = await fetchval(
                "SELECT total_volume FROM whales WHERE LOWER(address) = LOWER($1)",
                proxy_address,
            )
            if total_volume is not None:
                return float(total_volume)
            return None
        except Exception as e:
            logger.error("value_fetch_error", address=proxy_address, error=str(e))
            return None

    def filter_whale_trades(
        self,
        trades: list[dict[str, Any]],
        min_usd: Decimal = WHALE_THRESHOLD_USD,
    ) -> list[dict[str, Any]]:
        """
        Filter trades to only include those at or above the USD threshold.

        USD value is computed as size * price (size is in shares, not USD).
        There is no server-side size filter, so this must be done client-side.

        Uses Decimal arithmetic for consistency with :meth:`normalize_trade`
        and to avoid floating-point precision issues near the threshold.

        Args:
            trades: List of raw trade dicts from the API.
            min_usd: Minimum USD value to qualify as a whale trade.
                     Defaults to :data:`WHALE_THRESHOLD_USD` ($1 000).

        Returns:
            Filtered list of whale-sized trades.
        """
        threshold = Decimal(str(min_usd))
        return [
            trade
            for trade in trades
            if Decimal(str(trade.get("size", 0))) * Decimal(str(trade.get("price", 0))) >= threshold
        ]

    def normalize_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        """
        Convert a raw Polymarket trade dict into the event data format
        used by the transaction-processor pipeline.

        Args:
            trade: Raw trade dict from the Polymarket Data API.

        Returns:
            Dict matching the oracle/transaction.received event data schema.
        """
        size = Decimal(str(trade.get("size", 0)))
        price = Decimal(str(trade.get("price", 0)))
        amount = (size * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Convert unix timestamp to ISO 8601 datetime string
        ts = trade.get("timestamp", 0)
        block_timestamp = datetime.fromtimestamp(ts, tz=UTC).isoformat()

        return {
            "tx_hash": trade.get("transactionHash", ""),
            "log_index": 0,
            "from_address": trade.get("proxyWallet", "").lower(),
            "to_address": "",
            "amount": str(amount),
            "direction": trade.get("side", "").lower(),
            "asset": "USDC",
            "raw_contract_address": None,
            "block_number": 0,
            "block_timestamp": block_timestamp,
            "r2_archive_key": None,
            "market_id": None,
            "is_whale": True,
            "source": "polymarket_api",
            "condition_id": trade.get("conditionId", ""),
            "outcome": trade.get("outcome", ""),
            "price": str(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "market_title": trade.get("title", ""),
            "market_slug": trade.get("slug", ""),
            "maker_address": trade.get("maker_address"),
            "taker_address": trade.get("taker_address"),
        }

    async def fetch_order_book_rest(self, token_id: str) -> dict[str, Any] | None:
        """
        Fetch order book snapshot via CLOB REST API.

        GET https://clob.polymarket.com/book?token_id={token_id}

        REST fallback когда WebSocket microstructure cache пуст.

        Returns:
            {"bids": [...], "asks": [...]} or None on failure.
        """
        try:
            return await self._fetch_book_with_resilience(token_id)
        except pybreaker.CircuitBreakerError:
            logger.debug("book_circuit_open", token_id=token_id[:16])
            return None
        except Exception as e:
            logger.debug("book_fetch_error", token_id=token_id[:16], error=str(e))
            return None

    @polymarket_breaker
    @http_retry
    async def _fetch_book_with_resilience(self, token_id: str) -> dict[str, Any] | None:
        """Internal: fetch order book с Circuit Breaker и Retry."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{CLOB_API_URL}/book",
                params={"token_id": token_id},
            )
            response.raise_for_status()
            return response.json()

    async def fetch_order_books_rest(self, token_ids: list[str]) -> list[dict[str, Any]]:
        """
        Batch fetch order books: POST https://clob.polymarket.com/books
        с телом [{"token_id": ...}, ...]. Лимит платформы 500 req/10s —
        один вызов = один запрос независимо от числа токенов.

        Returns:
            Список книг [{"asset_id": ..., "bids": [...], "asks": [...]}, ...],
            [] при ошибке (посев fire-and-forget, исключения не пропагируются).
        """
        if not token_ids:
            return []
        try:
            books = await self._fetch_books_with_resilience(token_ids)
            return books if isinstance(books, list) else []
        except pybreaker.CircuitBreakerError:
            logger.debug("books_circuit_open", n_tokens=len(token_ids))
            return []
        except Exception as e:
            logger.debug("books_fetch_error", n_tokens=len(token_ids), error=str(e))
            return []

    @polymarket_breaker
    @http_retry
    async def _fetch_books_with_resilience(self, token_ids: list[str]) -> list[dict[str, Any]] | None:
        """Internal: batch fetch order books с Circuit Breaker и Retry."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{CLOB_API_URL}/books",
                json=[{"token_id": t} for t in token_ids],
            )
            response.raise_for_status()
            return response.json()

    # =========================================================================
    # Новые API-методы (P1: Open Interest, Holders, Leaderboard)
    # =========================================================================

    async def fetch_open_interest(self, condition_id: str) -> dict[str, float] | None:
        """
        Fetch Open Interest (liquidity + volume) для рынка через Gamma API.

        GET https://gamma-api.polymarket.com/markets?condition_id={cid}

        Returns:
            {"liquidity": float, "volume": float} или None при ошибке.
        """
        if not condition_id:
            return None
        try:
            return await self._fetch_oi_with_resilience(condition_id)
        except pybreaker.CircuitBreakerError:
            logger.debug("oi_circuit_open", condition_id=condition_id[:16])
            return None
        except Exception as e:
            logger.debug("oi_fetch_error", condition_id=condition_id[:16], error=str(e))
            return None

    @polymarket_breaker
    @http_retry
    async def _fetch_oi_with_resilience(self, condition_id: str) -> dict[str, float] | None:
        """Internal: fetch OI через Gamma API с Circuit Breaker и Retry."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                GAMMA_MARKETS_URL,
                params={"condition_id": condition_id},
            )
            response.raise_for_status()
            data = response.json()
            if not data:
                return None
            market = data[0] if isinstance(data, list) else data
            # Gamma API возвращает liquidityNum/volumeNum (числовые) и liquidity/volume (строковые)
            liquidity = market.get("liquidityNum") or market.get("liquidity")
            volume = market.get("volumeNum") or market.get("volume")
            if liquidity is None:
                return None
            return {
                "liquidity": float(liquidity),
                "volume": float(volume or 0),
            }

    async def fetch_market_holders(self, condition_id: str, market_id: str | None = None) -> dict[str, Any] | None:
        """
        Fetch крупных держателей позиций на рынке из внутренней БД транзакций.

        Data API (positions endpoint) мёртв (404). Используем собственные данные
        из таблицы transactions — агрегируем по whale_id, считаем concentration.

        Returns:
            {"total_holders": int, "total_size": float, "top5_concentration": float,
             "holders": [{"address": str, "size": float}]}
            или None при ошибке.
        """
        if not market_id:
            return None
        try:
            return await self._fetch_holders_from_db(market_id)
        except Exception as e:
            logger.debug("holders_fetch_error", market_id=str(market_id)[:16], error=str(e))
            return None

    async def _fetch_holders_from_db(self, market_id: str) -> dict[str, Any] | None:
        """Fetch holders из внутренней БД транзакций с directional parsing (Deep Research v2)."""
        from app.services.database import fetch as db_fetch

        rows = await db_fetch(
            """
            SELECT w.address, t.outcome, SUM(t.amount) AS total_size
            FROM transactions t
            JOIN whales w ON w.id = t.whale_id
            WHERE t.market_id = $1 AND t.direction = 'buy'
            GROUP BY w.address, t.outcome
            HAVING SUM(t.amount) > 0
            ORDER BY total_size DESC
            LIMIT 40
            """,
            market_id,
        )

        if not rows:
            return None

        holders_yes: list[dict[str, Any]] = []
        holders_no: list[dict[str, Any]] = []
        holders_unknown: list[dict[str, Any]] = []

        # Fetch token_outcomes маппинг для named outcomes
        token_outcomes = None
        if market_id:
            from app.services.database import fetchval

            token_outcomes = await fetchval("SELECT token_outcomes FROM markets WHERE id = $1::uuid", market_id)

        yes_name = token_outcomes.get("Yes") if isinstance(token_outcomes, dict) else None
        no_name = token_outcomes.get("No") if isinstance(token_outcomes, dict) else None

        for r in rows:
            size = float(r["total_size"])
            holder = {"address": r["address"], "size": size}
            outcome_raw = str(r.get("outcome") or "")
            if outcome_raw in ("0", "Yes", "yes", "YES") or (yes_name and outcome_raw == yes_name):
                holders_yes.append(holder)
            elif outcome_raw in ("1", "No", "no", "NO") or (no_name and outcome_raw == no_name):
                holders_no.append(holder)
            else:
                holders_unknown.append(holder)

        all_holders = holders_yes + holders_no + holders_unknown
        if not all_holders:
            return None

        all_holders.sort(key=lambda h: h["size"], reverse=True)
        total_size = sum(h["size"] for h in all_holders)
        top5_size = sum(h["size"] for h in all_holders[:5])

        # Directional concentration
        concentration_yes = None
        concentration_no = None
        if holders_yes or holders_no:
            holders_yes.sort(key=lambda h: h["size"], reverse=True)
            holders_no.sort(key=lambda h: h["size"], reverse=True)
            total_yes = sum(h["size"] for h in holders_yes)
            total_no = sum(h["size"] for h in holders_no)
            top5_yes = sum(h["size"] for h in holders_yes[:5])
            top5_no = sum(h["size"] for h in holders_no[:5])
            concentration_yes = top5_yes / total_yes if total_yes > 0 else 0.0
            concentration_no = top5_no / total_no if total_no > 0 else 0.0

        return {
            "total_holders": len(all_holders),
            "total_size": total_size,
            "top5_concentration": top5_size / total_size if total_size > 0 else 0.0,
            "concentration_yes": concentration_yes,
            "concentration_no": concentration_no,
            "holders": all_holders[:10],
        }

    async def fetch_leaderboard_rank(self, proxy_address: str) -> dict[str, Any] | None:
        """
        Fetch ранг трейдера — из внутренней БД (pnl, roi, wins/losses).

        Data API leaderboard endpoint мёртв. Используем нашу таблицу whales.

        Returns:
            {"rank": int, "profit": float, "volume": float, "positions": int, "markets_traded": int}
            или None если не найден.
        """
        if not proxy_address:
            return None
        try:
            row = await fetchval(
                """
                SELECT json_build_object(
                    'rank', COALESCE(
                        (SELECT COUNT(*) + 1 FROM whales w2 WHERE w2.pnl_usd > w.pnl_usd),
                        9999
                    ),
                    'profit', COALESCE(w.pnl_usd, 0),
                    'volume', COALESCE(w.total_volume, 0),
                    'positions', COALESCE(w.wins + w.losses, 0),
                    'markets_traded', COALESCE(w.wins + w.losses, 0)
                )
                FROM whales w
                WHERE LOWER(w.address) = LOWER($1)
                """,
                proxy_address,
            )
            if row:
                if isinstance(row, str):
                    import json as _json

                    return _json.loads(row)
                return row
            return None
        except Exception as e:
            logger.debug("leaderboard_fetch_error", address=proxy_address[:16], error=str(e))
            return None

    async def fetch_order_books_batch(
        self,
        token_market_pairs: list[tuple[str, str]],
        concurrency: int = 20,
    ) -> dict[str, dict]:
        """
        Batch fetch order books для списка (token_id, market_id) пар.

        Кэширует результат в Redis (TTL 120s) — достаточно для oracle_recalculate loop.
        Возвращает {market_id: microstructure_dict}.
        """
        from app.core.cache import cache_get, cache_set
        from app.services.polymarket_ws_service import PolymarketWebSocketService

        ws_calc = PolymarketWebSocketService()
        semaphore = asyncio.Semaphore(concurrency)
        results: dict[str, dict] = {}
        lock = asyncio.Lock()
        fetched = 0
        cached = 0
        errors = 0

        async def fetch_one(token_id: str, market_id: str) -> None:
            nonlocal fetched, cached, errors
            async with semaphore:
                # Проверяем cache — может WS уже обновил. State-TTL (2026-07-03):
                # WS-хит валиден только через epoch-гейт; собственные REST-записи
                # (source=rest_poll) свежи по построению — их TTL 120с.
                from app.services.live_prices import is_ws_snapshot_live

                existing = await cache_get(f"microstructure:{market_id}")
                if existing and (existing.get("source") == "rest_poll" or await is_ws_snapshot_live(existing)):
                    async with lock:
                        results[market_id] = existing
                        cached += 1
                    return

                try:
                    book_data = await self.fetch_order_book_rest(token_id)
                    if book_data:
                        bids = book_data.get("bids", [])
                        asks = book_data.get("asks", [])
                        if bids or asks:
                            micro = ws_calc._calculate_microstructure(bids, asks)
                            micro["source"] = "rest_poll"
                            await cache_set(f"microstructure:{market_id}", micro, ttl_seconds=120)
                            async with lock:
                                results[market_id] = micro
                                fetched += 1
                            return
                except Exception:
                    pass
                async with lock:
                    errors += 1

        tasks = [fetch_one(token_id, market_id) for token_id, market_id in token_market_pairs]
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info(
            "order_books_batch_done",
            total=len(token_market_pairs),
            fetched=fetched,
            cached=cached,
            errors=errors,
        )
        return results

    async def resolve_condition_to_market(self, condition_id: str | None) -> str | None:
        """
        Resolve a Polymarket conditionId to an internal market UUID.

        Looks up the markets table in PostgreSQL where the condition_id column
        matches the given value.

        Args:
            condition_id: The Polymarket conditionId to look up.

        Returns:
            Internal market UUID if found, None otherwise.
        """
        if not condition_id:
            return None

        try:
            market_id = await fetchval(
                "SELECT id FROM markets WHERE condition_id = $1 LIMIT 1",
                condition_id,
            )

            if market_id:
                logger.info(
                    "trade_poller_market_resolved",
                    condition_id=condition_id[:16] + "...",
                    market_id=str(market_id),
                )
                return str(market_id)

            logger.debug(
                "trade_poller_market_not_found",
                condition_id=condition_id[:16] + "...",
            )
            return None

        except Exception as e:
            logger.warning(
                "trade_poller_market_resolution_failed",
                condition_id=condition_id,
                error=str(e),
            )
            return None


# =============================================================================
# Singleton factory
# =============================================================================

_poller_instance: PolymarketPollerService | None = None


def get_poller_service() -> PolymarketPollerService:
    """Return a singleton PolymarketPollerService instance."""
    global _poller_instance
    if _poller_instance is None:
        _poller_instance = PolymarketPollerService()
    return _poller_instance
