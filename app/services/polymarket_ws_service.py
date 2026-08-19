"""
Polymarket WebSocket Service — Real-time данные из CLOB.

Поставляет данные для Liquidity Gating (§3.3):
  δ = k / max(s, ε)
  Λ_whale_gated = Λ_whale · tanh(δ · L/V)

Где:
  L (depth) — глубина стакана, из Market WebSocket
  s (spread) — bid-ask spread, из Market WebSocket

Polymarket WS API формат:
  Market channel (public): {"type": "market", "assets_ids": [...], "initial_dump": true}
  User channel (auth required): {"auth": {...}, "type": "user", "markets": [...]}
  Keepalive: "PING" каждые 50 сек
"""

import asyncio
import json
import socket
import time
from datetime import UTC, datetime

import structlog
import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI, WebSocketException

import app.core.cache as _cache_module
from app.core.cache import cache_get, cache_set
from app.core.config import get_settings
from app.detectors.m2_vacuum_hook import _m2_depletion_check, _m2_emit_async, _m2_enabled

logger = structlog.get_logger()

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

# Макс. token_ids в одном subscribe сообщении (Polymarket ограничение)
MAX_BATCH_SIZE = 100

# State-кэш (fix 2026-07-03): microstructure-снапшот валиден, пока WS живо —
# «нет тика = цена не изменилась». TTL 60с выселял спокойные рынки и давал
# live_coverage=0% (~10 ключей из ~10k). Валидность решает epoch-гейт в
# get_live_yes_price, TTL здесь — только сборка мусора по really-dead рынкам.
MICROSTRUCTURE_STATE_TTL_SEC = 6 * 3600
WS_CONN_EPOCH_TTL_SEC = 7 * 24 * 3600
RESUBSCRIBE_INTERVAL_SEC = 900

# REST /books bootstrap (Fix-0, latency-аудит 2026-07-06): initial dump приходит
# на ~2% подписки — остальные книги сеем батчами POST /books после (ре)коннекта
# и раз в RESUBSCRIBE_INTERVAL_SEC для тихих рынков. Бюджет платформы 500 req/10с:
# 5000 YES-токенов чанками по 100 = 50 запросов с паузами, ~13с на полный посев.
REST_BOOKS_CHUNK_SIZE = 100
REST_BOOKS_CHUNK_PAUSE_SEC = 0.25

# PING keepalive интервал (секунды)
PING_INTERVAL = 50


def extract_fill_candidates(message: dict) -> list[dict]:
    """Кандидаты на fill собственных ордеров из события user-канала.

    Толерантна к вариациям формата (snake_case/camelCase): неизвестная форма →
    пустой список (fallback — REST-поллинг в v2_execution_resolve остаётся
    reconcile-путём). Формат событий user-канала Polymarket в бою неизвестен —
    первый прод-fill логируется через ws_user_event_raw, extract_fill_candidates
    поправить при расхождении.
    """
    out: list[dict] = []

    def _num(v) -> float | None:
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None

    taker_id = message.get("taker_order_id") or message.get("takerOrderId")
    price, size = _num(message.get("price")), _num(message.get("size") or message.get("amount"))
    if taker_id and price and size:
        out.append({"order_id": str(taker_id), "price": price, "size": size})
    for mo in message.get("maker_orders") or message.get("makerOrders") or []:
        if not isinstance(mo, dict):
            continue
        oid = mo.get("order_id") or mo.get("orderId") or mo.get("id")
        mprice = _num(mo.get("price")) or price
        msize = _num(mo.get("matched_amount") or mo.get("matchedAmount") or mo.get("size"))
        if oid and mprice and msize:
            out.append({"order_id": str(oid), "price": mprice, "size": msize})
    return out


class PolymarketWebSocketService:
    """
    Async WebSocket client для Polymarket CLOB real-time order book данных.

    Market channel: подписка на token_ids → order book updates → Redis cache.
    User channel: требует API creds (Polymarket CLOB API key), подписка по condition_ids.
    Whale detection покрывается trade-poll cron (каждые 5 мин), user channel опционален.
    """

    def __init__(self):
        self.settings = get_settings()
        self._market_ws = None
        self._user_ws = None
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._consecutive_failures: dict[str, int] = {"market": 0, "user": 0}
        self._max_consecutive_failures = 5
        self._channel_cooldown_seconds = 300.0
        self._subscribed_markets: set[str] = set()
        self._subscribed_wallets: set[str] = set()
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._token_to_market_id: dict[str, str] = {}
        # Write-through WS-midpoint → markets.current_price (audit топ-3): детекция
        # сидела на 15-минутном gamma-снапшоте. dirty_prices копится на каждый
        # microstructure-апдейт YES-токена, flush батчем раз в 30с (см. _flush_prices_loop).
        self._dirty_prices: dict[str, float] = {}
        self._yes_token_markets: dict[str, str] = {}
        # Heartbeat freshness (audit second-tier): throttled write, не чаще раза в
        # 5с, чтобы не забивать Redis на каждом market-сообщении. Читается
        # api-health job'ом — молчание > 2 мин = алёрт.
        self._last_hb_ts: float = 0.0
        # Эпоха текущего market-коннекта: снапшоты прошлых соединений отсекаются
        # читателем (get_live_yes_price) без SCAN+DEL при реконнекте.
        self._conn_epoch: float = 0.0
        # Токены с WS-тиком в текущем соединении: у них epoch-снапшот свежее
        # REST-посева — bootstrap/re-seed их пропускает. Сбрасывается на новую
        # эпоху и на каждом re-seed цикле.
        self._ticked_tokens: set[str] = set()
        self._seed_task: asyncio.Task | None = None

    # --- Market Channel ---

    async def connect_market_channel(self):
        """Market WebSocket: подписка на order book updates. Auto-reconnect."""
        ws_url = self.settings.polymarket_ws_url or MARKET_WS_URL

        while self._running:
            try:
                connected_at = asyncio.get_event_loop().time()
                received_messages = False

                async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10, family=socket.AF_INET6) as ws:
                    self._market_ws = ws
                    logger.info("ws_market_connected", url=ws_url)

                    # Новая эпоха ДО подписки: initial dump уже несёт её штамп
                    await self._bump_conn_epoch()

                    # Подписка на все token_ids батчами
                    await self._subscribe_all_markets(ws)

                    # REST-bootstrap книг (Fix-0): initial dump придёт на ~2%
                    # подписки, остальное сеем батчами POST /books в фоне
                    if self._seed_task and not self._seed_task.done():
                        self._seed_task.cancel()
                    self._seed_task = asyncio.create_task(self._seed_all_books(), name="ws_books_bootstrap")

                    # PING keepalive task
                    ping_task = asyncio.create_task(self._ping_loop(ws, "market"))

                    try:
                        async for raw_message in ws:
                            received_messages = True
                            try:
                                message = json.loads(raw_message)
                                # Initial dump может прийти как массив сообщений
                                if isinstance(message, list):
                                    for item in message:
                                        if isinstance(item, dict):
                                            await self._handle_market_message(item)
                                elif isinstance(message, dict):
                                    await self._handle_market_message(message)
                            except json.JSONDecodeError:
                                pass
                            except Exception as e:
                                logger.error("ws_market_handler_error", error=str(e))
                    finally:
                        ping_task.cancel()

            except ConnectionClosed as e:
                logger.warning("ws_market_disconnected", code=e.code, reason=str(e.reason)[:100])
            except (InvalidURI, OSError, WebSocketException) as e:
                logger.error("ws_market_connection_error", error=str(e))
            except Exception as e:
                logger.error("ws_market_unexpected_error", error=str(e))
            finally:
                self._market_ws = None

            if self._running:
                elapsed = asyncio.get_event_loop().time() - connected_at
                if received_messages or elapsed >= 30.0:
                    self._consecutive_failures["market"] = 0
                await self._reconnect("market")

    async def _subscribe_all_markets(self, ws):
        """Отправить subscribe батчами по MAX_BATCH_SIZE token_ids."""
        tokens = list(self._subscribed_markets)
        if not tokens:
            logger.info("ws_market_no_subscriptions")
            return

        for i in range(0, len(tokens), MAX_BATCH_SIZE):
            batch = tokens[i : i + MAX_BATCH_SIZE]
            msg = json.dumps(
                {
                    "type": "market",
                    "assets_ids": batch,
                    "initial_dump": True,
                    "custom_feature_enabled": True,
                }
            )
            await ws.send(msg)

        logger.info("ws_market_subscribed_bulk", total=len(tokens), batches=(len(tokens) - 1) // MAX_BATCH_SIZE + 1)

    async def subscribe_market(self, token_id: str, market_id: str | None = None):
        """Подписка на order book для конкретного рынка (lazy, при первом recalculate)."""
        is_new = token_id not in self._subscribed_markets
        self._subscribed_markets.add(token_id)
        if market_id:
            self._token_to_market_id[token_id] = market_id

        if is_new and self._market_ws:
            try:
                msg = json.dumps(
                    {
                        "type": "market",
                        "assets_ids": [token_id],
                        "initial_dump": True,
                        "custom_feature_enabled": True,
                    }
                )
                await self._market_ws.send(msg)
                logger.info("ws_market_subscribed", token_id=token_id[:20], market_id=market_id)
            except Exception as e:
                logger.warning("ws_market_subscribe_failed", error=str(e))

    async def unsubscribe_market(self, token_id: str):
        """Отписка от рынка."""
        self._subscribed_markets.discard(token_id)
        self._token_to_market_id.pop(token_id, None)

    async def _handle_market_message(self, message: dict):
        """Обработка order book update → Redis cache."""
        await self._record_heartbeat()

        # asset_id ПЕРВЫМ: на Polymarket market-канале asset_id = CLOB token_id
        # (decimal, = markets.clob_token_ids), а market = condition_id (hex). Recorder
        # читает ring/microstructure по token_id, поэтому индексировать обязаны по
        # asset_id (см. docs.polymarket.com/.../websocket/market-channel).
        token_id = message.get("asset_id") or message.get("assets_id") or message.get("market")
        if not token_id:
            return

        msg_type = message.get("type", "")

        if msg_type == "market_resolved":
            await self._handle_market_resolved(message)
            return

        bids = message.get("bids", [])
        asks = message.get("asks", [])

        if msg_type == "best_bid_ask":
            await self._handle_best_bid_ask(message)
            return

        if msg_type == "new_market":
            await self._handle_new_market(message)
            return

        if not bids and not asks:
            return

        microstructure = self._calculate_microstructure(bids, asks)
        microstructure["updated_at"] = datetime.now(UTC).isoformat()
        microstructure["epoch"] = self._conn_epoch
        self._ticked_tokens.add(str(token_id))
        self._mark_dirty_price(token_id, microstructure.get("midpoint"))

        await cache_set(f"microstructure:{token_id}", microstructure, ttl_seconds=MICROSTRUCTURE_STATE_TTL_SEC)
        await cache_set(f"orderbook:{token_id}", microstructure, ttl_seconds=60)

        # Ringbuffer для whale-shock backward-окна (cross-process через Redis).
        from app.services.snapshot_recorder import ring_push

        await ring_push(token_id, microstructure, ts_unix=time.time())

        market_id = self._token_to_market_id.get(token_id)
        if market_id:
            await cache_set(f"microstructure:{market_id}", microstructure, ttl_seconds=MICROSTRUCTURE_STATE_TTL_SEC)
            await cache_set(f"orderbook:{market_id}", microstructure, ttl_seconds=60)

            # ── M2 liquidity-vacuum realtime hook (measurement, shadow) ──
            # O(1) depletion-check на hot-path; тяжёлое — fire-and-forget.
            if _m2_enabled():
                depleted = _m2_depletion_check(microstructure)
                if depleted is not None:
                    asyncio.create_task(_m2_emit_async(market_id, token_id, microstructure, depleted))

            try:
                redis = _cache_module._redis
                if redis:
                    history_key = f"microstructure_history:{market_id}"
                    history_item = json.dumps(
                        {
                            "depth_yes": microstructure.get("depth_yes", 0),
                            "depth_no": microstructure.get("depth_no", 0),
                            "spread": microstructure.get("spread", 0),
                            "ts": time.time(),
                        }
                    )
                    await redis.lpush(history_key, history_item)
                    await redis.ltrim(history_key, 0, 9)
                    await redis.expire(history_key, 600)
            except Exception as exc:  # noqa: BLE001 — history не должна ронять WSS
                logger.debug("microstructure_history_write_failed", market_id=str(market_id)[:20], error=str(exc))

        logger.debug(
            "ws_microstructure_updated",
            token_id=token_id[:20],
            market_id=market_id,
            spread=microstructure["spread"],
        )

    async def _bump_conn_epoch(self) -> None:
        """Новая эпоха на (ре)коннект: публикуется в Redis для epoch-гейта читателей.

        Fail-closed на стороне читателя: если запись упала, старая эпоха в Redis
        не совпадёт со штампами новых снапшотов → is_live=False, не stale-as-live.
        """
        self._conn_epoch = time.time()
        # Тики прошлого соединения не должны заставлять bootstrap пропускать токены
        self._ticked_tokens.clear()
        try:
            await cache_set("ws:conn_epoch", {"epoch": self._conn_epoch}, ttl_seconds=WS_CONN_EPOCH_TTL_SEC)
        except Exception as e:
            logger.warning("ws_conn_epoch_write_failed", error=str(e))

    async def _record_heartbeat(self) -> None:
        """Throttled запись freshness-метки (audit second-tier: WS может тихо
        отвалиться в half-open состояние — reconnect-логика этого не видит).

        Не чаще раза в 5с, чтобы не забивать Redis на каждом market-сообщении
        (десятки/сек в пике). Читается app.jobs.polymarket_api_health: молчание
        > 2 мин → TG-алерт.
        """
        now = time.time()
        if now - self._last_hb_ts < 5.0:
            return
        self._last_hb_ts = now
        await cache_set("ws:last_message_at", {"ts": now}, ttl_seconds=600)

    @staticmethod
    def _calculate_microstructure(bids: list, asks: list) -> dict:
        """Рассчитать depth, spread, levels из raw order book ОДНОГО token_id.

        ВАЖНО: bids/asks — это ордера на ОДИН конкретный outcome token, не
        bids на YES-токен vs bids на NO-токен. На Polymarket у каждого outcome
        свой token_id с независимым стаканом. Возвращаемые depth_bid/depth_ask
        — это ликвидность одной стороны стакана (не направление ставок).

        Исторически поля назывались depth_yes/depth_no, что создавало
        иллюзию "depth YES выигрывает" — из-за этого формула в
        microstructure_bayes_factor давала систематический bias (см. audit
        2026-04-10). Переименованы для честной семантики.

        Совместимость: depth_yes/depth_no сохранены в выводе как aliases,
        чтобы не сломать downstream код. Их использовать НЕ рекомендуется.

        staticmethod: не использует self — позволяет звать из snapshot_recorder
        REST-fallback без инстанциирования сервиса.
        """
        parsed_bids = PolymarketWebSocketService._parse_orders(bids)
        parsed_asks = PolymarketWebSocketService._parse_orders(asks)

        parsed_bids.sort(key=lambda x: x[0], reverse=True)
        parsed_asks.sort(key=lambda x: x[0])

        best_bid = parsed_bids[0][0] if parsed_bids else 0.0
        best_ask = parsed_asks[0][0] if parsed_asks else 0.0
        best_bid_size = parsed_bids[0][1] if parsed_bids else 0.0
        best_ask_size = parsed_asks[0][1] if parsed_asks else 0.0

        if best_ask > 0 and best_bid > 0:
            spread = (best_ask - best_bid) / best_ask
        else:
            spread = 1.0

        # Честные имена: глубина bid-стороны / ask-стороны одного токена.
        depth_bid = sum(size for _, size in parsed_bids)
        depth_ask = sum(size for _, size in parsed_asks)
        depth_total = depth_bid + depth_ask

        spread_clamped = max(0.0, min(1.0, spread))

        if spread_clamped < 0.02:
            health = "healthy"
        elif spread_clamped < 0.05:
            health = "moderate"
        else:
            health = "illiquid"

        if best_bid > 0 and best_ask > 0:
            midpoint = (best_bid + best_ask) / 2
        else:
            midpoint = best_bid or best_ask or 0.0

        top_bids = [{"price": p, "size": s} for p, s in parsed_bids[:10]]
        top_asks = [{"price": p, "size": s} for p, s in parsed_asks[:10]]

        return {
            # Честные имена
            "depth_bid": depth_bid,
            "depth_ask": depth_ask,
            "depth_total": depth_total,
            # Legacy aliases (deprecated — не использовать как direction signal!)
            "depth_yes": depth_bid,
            "depth_no": depth_ask,
            "spread": spread_clamped,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "best_bid_size": best_bid_size,
            "best_ask_size": best_ask_size,
            "midpoint": midpoint,
            "health": health,
            "levels": {"bids": top_bids, "asks": top_asks},
        }

    @staticmethod
    def _parse_orders(orders: list) -> list[tuple[float, float]]:
        """Парсинг ордеров из разных форматов в [(price, size)]."""
        result = []
        for order in orders:
            try:
                if isinstance(order, dict):
                    price = float(order.get("price", 0))
                    size = float(order.get("size", 0))
                elif isinstance(order, list | tuple) and len(order) >= 2:
                    price = float(order[0])
                    size = float(order[1])
                else:
                    continue
                if price > 0 and size > 0:
                    result.append((price, size))
            except (ValueError, TypeError):
                continue
        return result

    def _mark_dirty_price(self, token_id: str, midpoint: float | None) -> None:
        """Записать midpoint в dirty-set write-through, если token_id — YES-нога рынка.

        Флашится батчем в markets.current_price раз в 30с (_flush_prices_loop).
        Audit топ-3: детекция сидела на 15-минутном gamma-снапшоте.
        """
        market_id = self._yes_token_markets.get(token_id)
        if not market_id:
            return
        try:
            mid = float(midpoint)
        except (TypeError, ValueError):
            return
        if 0.0 < mid < 1.0:
            self._dirty_prices[market_id] = mid

    async def _handle_best_bid_ask(self, message: dict):
        """Partial microstructure update из best_bid_ask event."""
        # asset_id ПЕРВЫМ (token_id), не market (condition_id) — см. _handle_market_message.
        token_id = message.get("asset_id") or message.get("market") or ""
        if not token_id:
            return

        try:
            best_bid = float(message.get("best_bid", 0))
            best_ask = float(message.get("best_ask", 0))
        except (ValueError, TypeError):
            return

        if best_bid <= 0 or best_ask <= 0:
            return

        spread = (best_ask - best_bid) / best_ask
        midpoint = (best_bid + best_ask) / 2
        spread_clamped = max(0.0, min(1.0, spread))

        if spread_clamped < 0.02:
            health = "healthy"
        elif spread_clamped < 0.05:
            health = "moderate"
        else:
            health = "illiquid"

        update = {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread_clamped,
            "midpoint": midpoint,
            "health": health,
            "updated_at": datetime.now(UTC).isoformat(),
            "epoch": self._conn_epoch,
        }

        self._ticked_tokens.add(str(token_id))
        self._mark_dirty_price(token_id, midpoint)

        # best_bid_ask несёт только top-of-book цены (без size/depth/levels) —
        # merge поверх существующего кэша, чтобы не затирать поля последнего
        # полного book-сообщения (audit 2026-07-02, топ-2: fast-path требует
        # best_bid_size/best_ask_size, которые тут не пересчитываются).
        existing = await cache_get(f"microstructure:{token_id}") or {}
        existing.update(update)
        await cache_set(f"microstructure:{token_id}", existing, ttl_seconds=MICROSTRUCTURE_STATE_TTL_SEC)

        market_id = self._token_to_market_id.get(token_id)
        if market_id:
            existing_market = await cache_get(f"microstructure:{market_id}") or {}
            existing_market.update(update)
            await cache_set(f"microstructure:{market_id}", existing_market, ttl_seconds=MICROSTRUCTURE_STATE_TTL_SEC)

    async def _handle_new_market(self, message: dict):
        """Автодобавление нового рынка при WS new_market event."""
        import json as _json

        from app.jobs.market_sync import process_market
        from app.services.polymarket_service import get_polymarket_service

        condition_id = message.get("condition_id") or message.get("market", "")
        if not condition_id:
            return

        try:
            poly_service = get_polymarket_service()
            market_data = await poly_service.get_market_details(condition_id)
        except Exception as e:
            logger.warning("ws_new_market_gamma_failed", condition_id=str(condition_id)[:20], error=str(e))
            return

        if not market_data:
            return

        try:
            result = await process_market(market_data)
            market_id = result.get("id")

            clob_ids = market_data.get("clobTokenIds", [])
            if isinstance(clob_ids, str):
                clob_ids = _json.loads(clob_ids)
            for tid in clob_ids:
                await self.subscribe_market(str(tid), market_id)

            logger.info("ws_new_market_added", condition_id=str(condition_id)[:20], market_id=market_id)
            # Weather arb reactive analysis removed в Phoenix V2 cleanup.
        except Exception as e:
            logger.error("ws_new_market_process_failed", condition_id=str(condition_id)[:20], error=str(e))

    async def _handle_market_resolved(self, message: dict):
        """Обработка market_resolved event → мгновенный resolve pipeline (уровень 1)."""
        asset_id = message.get("asset_id") or message.get("market", "")
        market_id = self._token_to_market_id.get(asset_id)

        if not market_id:
            logger.warning("ws_market_resolved_unknown_token", asset_id=str(asset_id)[:20])
            return

        price = float(message.get("price", 0.5))
        outcome = "yes" if price > 0.5 else "no"

        logger.info("ws_market_resolved", market_id=market_id, asset_id=str(asset_id)[:20], outcome=outcome)

        try:
            from app.services.database import execute

            await execute(
                "UPDATE markets SET is_active = false, resolved = true, winner = $1 WHERE id = $2::uuid",
                outcome.upper(),
                market_id,
            )

            from app.tasks.resolve_outcomes import resolve_single_market

            snap_count = await resolve_single_market(market_id, outcome, price)
            logger.info("ws_market_resolved_complete", market_id=market_id, snaps=snap_count)
        except Exception as e:
            logger.error("ws_market_resolved_failed", market_id=market_id, error=str(e))

    # --- User Channel ---

    async def connect_user_channel(self):
        """
        User WebSocket: требует API creds (apiKey, secret, passphrase).
        Подписка по condition_id рынков, не по wallet address.
        Без creds — канал не стартует.
        """
        api_key = getattr(self.settings, "polymarket_api_key", None)
        api_secret = getattr(self.settings, "polymarket_api_secret", None)
        api_passphrase = getattr(self.settings, "polymarket_api_passphrase", None)

        if not all([api_key, api_secret, api_passphrase]):
            logger.info("ws_user_skipped", reason="no API creds (polymarket_api_secret/passphrase)")
            return

        while self._running:
            try:
                connected_at = asyncio.get_event_loop().time()
                received_messages = False

                async with websockets.connect(
                    USER_WS_URL, ping_interval=30, ping_timeout=10, family=socket.AF_INET6
                ) as ws:
                    self._user_ws = ws
                    logger.info("ws_user_connected")

                    # Подписка с auth
                    subscribe_msg = json.dumps(
                        {
                            "auth": {
                                "apiKey": api_key,
                                "secret": api_secret,
                                "passphrase": api_passphrase,
                            },
                            "type": "user",
                            "markets": [],  # пустой = все рынки юзера
                        }
                    )
                    await ws.send(subscribe_msg)

                    ping_task = asyncio.create_task(self._ping_loop(ws, "user"))

                    try:
                        async for raw_message in ws:
                            received_messages = True
                            try:
                                message = json.loads(raw_message)
                                await self._handle_user_message(message)
                            except json.JSONDecodeError:
                                pass
                            except Exception as e:
                                logger.error("ws_user_handler_error", error=str(e))
                    finally:
                        ping_task.cancel()

            except ConnectionClosed as e:
                logger.warning("ws_user_disconnected", code=e.code, reason=str(e.reason)[:100])
            except (InvalidURI, OSError, WebSocketException) as e:
                logger.error("ws_user_connection_error", error=str(e))
            except Exception as e:
                logger.error("ws_user_unexpected_error", error=str(e))
            finally:
                self._user_ws = None

            if self._running:
                elapsed = asyncio.get_event_loop().time() - connected_at
                if received_messages or elapsed >= 30.0:
                    self._consecutive_failures["user"] = 0
                await self._reconnect("user")

    async def _handle_user_message(self, message: dict):
        """Обработка trade events от user channel → Redis queue + execution_orders fill."""
        logger.debug("ws_user_event_raw", event_type=message.get("type"), keys=sorted(message.keys()))

        trade_type = message.get("type", "")
        if trade_type not in ("trade", "order_matched", "ORDER_MATCHED"):
            return

        fills = extract_fill_candidates(message)
        if fills:
            from app.services.database import execute

            ids = [f["order_id"] for f in fills]
            try:
                updated = await execute(
                    """UPDATE execution_orders
                          SET status = 'filled', fill_price = $2, filled_at = now()
                        WHERE order_id = ANY($1::text[]) AND status = 'placed'""",
                    ids,
                    fills[0]["price"],
                )
                logger.info("ws_user_fill_applied", order_ids=ids, result=str(updated))
            except Exception as e:
                logger.error("ws_user_fill_apply_failed", order_ids=ids, error=str(e))

        amount_str = message.get("size", message.get("amount", "0"))
        price_str = message.get("price")

        if not price_str:
            logger.debug("ws_trade_no_price", msg_type=message.get("type"))
            return

        try:
            amount = float(amount_str)
            price = float(price_str)
            if price <= 0 or price >= 1.0:
                logger.debug("ws_trade_invalid_price", price=price)
                return
            usd_value = amount * price
        except (ValueError, TypeError):
            return

        wallet = message.get("owner", message.get("wallet", message.get("user", ""))).lower()

        if usd_value >= 1000.0:
            logger.info("ws_whale_trade_detected", wallet=wallet, usd_value=usd_value)

            side = message.get("side", "").upper()
            # outcome: сначала пробуем поле outcome, потом asset_id
            outcome = message.get("outcome", "").upper()
            if outcome not in ("YES", "NO"):
                outcome = "YES"
            direction = "buy" if side in ("BUY", "B", "") else "sell"
            token_id = message.get("asset_id", message.get("market", ""))
            market_id = self._token_to_market_id.get(token_id)

            try:
                from app.core.task_queue import enqueue

                await enqueue(
                    "oracle:queue:transactions",
                    {
                        "tx_hash": message.get("id", message.get("tradeId", "")),
                        "log_index": 0,
                        "from_address": wallet,
                        "to_address": "",
                        "amount": str(usd_value),
                        "direction": direction,
                        "outcome": outcome,
                        "market_id": market_id,
                        "is_whale": True,
                        "price": price_str,
                        "block_timestamp": message.get("timestamp", datetime.now(UTC).isoformat()),
                        "source": "websocket",
                    },
                )
            except Exception as e:
                logger.error("ws_enqueue_failed", wallet=wallet, error=str(e))

    # --- Lifecycle ---

    async def start(self):
        """Запуск каналов. Вызывается из FastAPI lifespan.

        User channel требует CLOB API auth и используется только для execution_bot.
        Без EXECUTION_ENABLED=true канал даёт ws_user_disconnected 1006 каждую
        секунду — лог-spam и лишний traffic. Запускаем его только когда нужен.
        """
        if self._running:
            return
        self._running = True

        # Загрузить активные рынки для market channel
        await self._load_active_markets()

        self._tasks = [
            asyncio.create_task(self.connect_market_channel(), name="ws_market"),
            asyncio.create_task(self._flush_prices_loop(), name="ws_price_flush"),
            asyncio.create_task(self._resubscribe_loop(), name="ws_resubscribe"),
        ]
        if self.settings.execution_enabled:
            self._tasks.append(asyncio.create_task(self.connect_user_channel(), name="ws_user"))
            logger.info("ws_user_channel_enabled", reason="EXECUTION_ENABLED=true")
        else:
            logger.info("ws_user_channel_skipped", reason="EXECUTION_ENABLED=false")
        logger.info("ws_service_started", market_subs=len(self._subscribed_markets))

    async def _flush_prices_loop(self) -> None:
        """Батч write-through WS-midpoint → markets.current_price раз в 30с (audit топ-3:
        MtM-килл, TP/SL и сканы сидели на 15-минутном gamma-снапшоте)."""
        while self._running:
            await asyncio.sleep(30)
            await self._flush_prices_once()

    async def _flush_prices_once(self) -> None:
        from app.services.database import executemany

        if not get_settings().ws_price_write_through_enabled or not self._dirty_prices:
            return
        batch, self._dirty_prices = self._dirty_prices, {}
        try:
            await executemany(
                "UPDATE markets SET current_price = $2, updated_at = now() WHERE polymarket_id = $1",
                [(market_id, price) for market_id, price in batch.items()],
            )
            logger.debug("ws_price_write_through", n=len(batch))
        except Exception as e:
            # Не терять окно цен: вернуть батч в очередь; тик, пришедший во
            # время неудачной записи, свежее — setdefault его не затирает.
            for market_id, price in batch.items():
                self._dirty_prices.setdefault(market_id, price)
            logger.warning("ws_price_write_through_failed", n_requeued=len(batch), error=str(e)[:200])

    async def _load_active_markets(self):
        """Загрузить token_ids для подписки: top по объёму + рынки открытых позиций.

        clob_token_ids — JSONB массив из 2 token_ids (YES/NO outcome).
        Подписываемся на оба для полного order book.

        Рынки с открытыми real-money позициями (placed/filled) подписываются
        безусловно, независимо от volume-ранга: MtM/exit-контур обязан видеть
        live-цену удерживаемого рынка (fix 2026-07-03: позиция ранга 7251 по
        объёму не попадала в top-подписку → live_coverage=0% для MtM).
        Только добавляет — выпавшие из топа не отписываются до рестарта.
        """
        from app.services.database import fetch

        try:
            rows = await fetch(
                """
                (
                    -- LIMIT в подзапросе по рынкам ДО разворота токенов: снаружи он
                    -- резал строки market×token → top-2500 рынков (аудит 2026-07-06)
                    SELECT m.id, t.token_id, m.clob_token_ids ->> 0 AS yes_token
                    FROM (
                        SELECT id, clob_token_ids
                        FROM markets
                        WHERE is_active = true
                          AND clob_token_ids IS NOT NULL
                          AND jsonb_array_length(clob_token_ids) > 0
                        ORDER BY volume_24h DESC NULLS LAST
                        LIMIT 5000  -- 2026-05-13: 1000→5000, покрываем thin markets для D31/D32/D33
                    ) m,
                    jsonb_array_elements_text(m.clob_token_ids) AS t(token_id)
                )
                UNION
                SELECT m.id, t.token_id, m.clob_token_ids ->> 0 AS yes_token
                FROM markets m,
                     jsonb_array_elements_text(m.clob_token_ids) AS t(token_id)
                WHERE m.clob_token_ids IS NOT NULL
                  AND jsonb_array_length(m.clob_token_ids) > 0
                  AND EXISTS (
                      SELECT 1 FROM execution_orders eo
                      WHERE eo.market_id = m.polymarket_id
                        AND eo.status IN ('placed', 'filled')
                  )
                """
            )

            count = 0
            for row in rows:
                token_id = str(row.get("token_id", ""))
                market_id = str(row.get("id", ""))
                if token_id and market_id:
                    self._subscribed_markets.add(token_id)
                    self._token_to_market_id[token_id] = market_id
                    count += 1

                yes_token = row.get("yes_token")
                if yes_token and market_id:
                    self._yes_token_markets[str(yes_token)] = market_id

            logger.info("ws_active_markets_loaded", count=count)
        except Exception as e:
            logger.warning("ws_load_active_markets_failed", error=str(e))

    async def _refresh_subscriptions(self) -> None:
        """Один проход рефреша: перечитать желаемый набор, дослать subscribe на новые.

        Набор подписок был заморожен на момент старта процесса — рынок, взлетевший
        в топ (или позиция, открытая после старта), не получал live-цену до рестарта.
        """
        before = set(self._subscribed_markets)
        await self._load_active_markets()
        new_tokens = sorted(self._subscribed_markets - before)
        if not new_tokens or not self._market_ws:
            return
        try:
            for i in range(0, len(new_tokens), MAX_BATCH_SIZE):
                batch = new_tokens[i : i + MAX_BATCH_SIZE]
                msg = json.dumps(
                    {
                        "type": "market",
                        "assets_ids": batch,
                        "initial_dump": True,
                        "custom_feature_enabled": True,
                    }
                )
                await self._market_ws.send(msg)
            logger.info("ws_market_resubscribed", new=len(new_tokens))
        except Exception as e:
            # Не откатываем set: на следующем реконнекте _subscribe_all_markets дошлёт
            logger.warning("ws_market_resubscribe_failed", error=str(e))

    async def _seed_books_via_rest(self, token_ids: list[str]) -> int:
        """REST-посев книг батчами POST /books (Fix-0, latency-аудит 2026-07-06).

        Снапшоты пишутся как rest_seed без epoch — та же семантика, что у
        seed_book_snapshot: не тикающий рынок не должен жить 6ч как live.
        Токены с WS-тиком в текущем соединении пропускаются — их epoch-снапшот
        свежее посева.
        """
        from app.services.live_prices import REST_SEED_TTL_SEC
        from app.services.polymarket_poller_service import get_poller_service

        poller = get_poller_service()
        seeded = 0
        first_chunk = True
        for i in range(0, len(token_ids), REST_BOOKS_CHUNK_SIZE):
            chunk = [t for t in token_ids[i : i + REST_BOOKS_CHUNK_SIZE] if t not in self._ticked_tokens]
            if not chunk:
                continue
            if not first_chunk:
                await asyncio.sleep(REST_BOOKS_CHUNK_PAUSE_SEC)
            first_chunk = False
            books = await poller.fetch_order_books_rest(chunk)
            for book in books or []:
                token_id = str(book.get("asset_id") or "")
                if not token_id or token_id in self._ticked_tokens:
                    continue
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                if not bids and not asks:
                    continue
                micro = self._calculate_microstructure(bids, asks)
                micro["source"] = "rest_seed"
                micro["updated_at"] = datetime.now(UTC).isoformat()
                await cache_set(f"microstructure:{token_id}", micro, ttl_seconds=REST_SEED_TTL_SEC)
                market_id = self._token_to_market_id.get(token_id)
                if market_id:
                    await cache_set(f"microstructure:{market_id}", micro, ttl_seconds=REST_SEED_TTL_SEC)
                seeded += 1
        logger.info("ws_books_rest_seeded", requested=len(token_ids), seeded=seeded)
        return seeded

    async def _seed_all_books(self) -> None:
        """Bootstrap после (ре)коннекта: посев книг всех YES-токенов подписки.

        Только YES: их читают get_live_yes_price/bucket_gate/mtm, и microstructure
        по market_id от YES-книги имеет корректный midpoint. NO-книги не сеем —
        вдвое меньше REST-бюджета.
        """
        await self._seed_books_via_rest(sorted(self._yes_token_markets))

    async def _reseed_quiet_books(self) -> None:
        """Периодический re-seed YES-токенов без WS-тиков с прошлого цикла."""
        quiet = sorted(set(self._yes_token_markets) - self._ticked_tokens)
        self._ticked_tokens.clear()
        if quiet:
            await self._seed_books_via_rest(quiet)

    async def _resubscribe_loop(self) -> None:
        while self._running:
            await asyncio.sleep(RESUBSCRIBE_INTERVAL_SEC)
            try:
                await self._refresh_subscriptions()
            except Exception as e:
                logger.warning("ws_resubscribe_loop_error", error=str(e))
            # Re-seed тихих рынков тем же 15-мин тактом; не поверх идущего bootstrap
            if not (self._seed_task and not self._seed_task.done()):
                self._seed_task = asyncio.create_task(self._reseed_quiet_books(), name="ws_books_reseed")

    async def subscribe_wallet(self, wallet_address: str):
        """Legacy: wallet подписка через user channel (требует API creds)."""
        self._subscribed_wallets.add(wallet_address.lower())

    async def load_tracked_wallets(self):
        """Legacy: загрузка whale адресов. User channel теперь работает по condition_ids."""
        pass

    async def stop(self):
        """Graceful shutdown."""
        self._running = False

        for ws in [self._market_ws, self._user_ws]:
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass

        if self._seed_task and not self._seed_task.done():
            self._seed_task.cancel()

        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._tasks.clear()
        self._market_ws = None
        self._user_ws = None
        logger.info("ws_service_stopped")

    async def _reconnect(self, channel: str):
        """Exponential backoff reconnect: 1s → 2s → 4s → ... → max 60s."""
        self._consecutive_failures[channel] += 1
        failures = self._consecutive_failures[channel]

        if failures >= self._max_consecutive_failures:
            logger.warning(
                "ws_channel_cooldown",
                channel=channel,
                failures=failures,
                cooldown=self._channel_cooldown_seconds,
            )
            self._consecutive_failures[channel] = 0
            await asyncio.sleep(self._channel_cooldown_seconds)
            return

        delay = min(self._reconnect_delay * (2 ** (failures - 1)), self._max_reconnect_delay)
        logger.warning("ws_reconnecting", channel=channel, delay=delay, attempt=failures)
        await asyncio.sleep(delay)

    @staticmethod
    async def _ping_loop(ws, channel: str):
        """Отправлять PING каждые 50 сек для keepalive."""
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                await ws.send("PING")
        except Exception:
            pass


# --- Public API ---


async def get_market_microstructure(identifier: str) -> dict | None:
    """
    Получить L и s для рынка из Redis cache.

    Используется в oracle_calculator.py для Liquidity Gating:
        delta = k / max(spread, EPSILON)
        lambda_gated = lambda_whale * tanh(delta * depth / volume)
    """
    return await cache_get(f"microstructure:{identifier}")


# Singleton
_ws_service: PolymarketWebSocketService | None = None


def get_ws_service() -> PolymarketWebSocketService:
    global _ws_service
    if _ws_service is None:
        _ws_service = PolymarketWebSocketService()
    return _ws_service
