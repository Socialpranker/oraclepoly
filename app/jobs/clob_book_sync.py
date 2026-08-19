"""
Taker Flow Sync — hourly job (сохраняет исторический command-name clob-book-sync).

Забирает последние executed trades топ-1000 активных рынков через
data-api.polymarket.com/trades, агрегирует BUY по outcomeIndex,
вычисляет taker_bias = (buy_token1 - buy_token2) / total
и пишет в maker_direction_cache.

Исторический анализ (SII-WANGZJ, 122K resolved markets):
- taker BUY bias corr с outcome = 0.7586
- accuracy при |bias|>0.5 = 92.8%, при |bias|>0.9 = 95.3%
- Per category: Weather 0.94, Crypto 0.85, Politics 0.84

Ранее этот job использовал order book (/book endpoint) с формулой
(bid_usd - ask_usd) / total — это оказалось НЕ тем сигналом
(MM ask asymmetry давала систематический -0.82 bias на топ-1000).
Правильный сигнал — это **исполненные BUY трейды** обоих outcome токенов.

Запросы БЕЗ прокси — read-only. Proxy допустим только в execution_bot.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import structlog

from app.repositories.maker_direction_repository import MakerDirectionRepository
from app.services.database import fetch

logger = structlog.get_logger()

# httpx логирует каждый HTTP запрос на INFO — шумно, приглушаем
logging.getLogger("httpx").setLevel(logging.WARNING)

_TRADES_URL = "https://data-api.polymarket.com/trades"
# data-api rate limit calibration (2026-04-10):
#   conc=3  @ n=100  → 100% OK
#   conc=10 @ n=100  → 100% OK
#   conc=20 @ n=100  → 35% fail (429)
#   conc=5  @ n=1000 → 55% fail (burst — пришлись в пик limit окна)
#   conc=3  @ n=1000 → 100% OK (101s, укладываемся в sliding window)
#   conc=3  @ n=3000 → ~180s (3 мин), укладывается в hourly window
# Вывод: conc=3 — 100% success rate. 3000 рынков покрывают ~98% volume.
_CONCURRENCY = 3
_TOP_MARKETS_LIMIT = 3000  # подняли с 1000 (2026-04-13): 94.8% volume → ~98%
_REQUEST_TIMEOUT = 15.0
_TRADES_LIMIT = 500  # сколько последних трейдов забирать per market
_MIN_VOLUME_USD = 25.0  # рынок с $25+ BUY объёма — понижен с $100 для coverage
# Removed 2026-04-30: oracle re-trigger на Δ taker flow.
# Pipeline V2 не использует oracle как primary signal — recalc раз в 6h хватает.
_MAX_RETRIES = 2  # retry on 429 / network errors
_RETRY_BACKOFF = 0.5  # exponential: 0.5s, 1.0s


def compute_taker_bias(trades: list[dict]) -> tuple[float, float, float]:
    """
    Вычисляет directional bias executed BUY трейдов по обоим outcome токенам.

    Корреляция +0.76 с outcome (исторический датасет 122K рынков).

    Args:
        trades: Список trade dicts из data-api.polymarket.com/trades.
                Ожидаемые поля: side ('BUY'/'SELL'), outcomeIndex (0/1),
                size (float), price (float).

    Returns:
        (bias, total_buy_usd, sample_size) где:
        - bias ∈ [-1, 1]: (buy_usd_0 - buy_usd_1) / (buy_usd_0 + buy_usd_1).
          Положительный → taker flow на token0 (обычно YES) → бычий сигнал.
        - total_buy_usd: суммарный USD объём BUY трейдов для фильтра noise.
        - sample_size: количество BUY трейдов в выборке.
    """
    buy_usd_0 = 0.0
    buy_usd_1 = 0.0
    sample_size = 0

    for t in trades:
        if t.get("side") != "BUY":
            continue
        try:
            size = float(t["size"])
            price = float(t["price"])
            idx = int(t["outcomeIndex"])
        except (KeyError, TypeError, ValueError):
            continue
        usd = size * price
        if idx == 0:
            buy_usd_0 += usd
        elif idx == 1:
            buy_usd_1 += usd
        sample_size += 1

    total = buy_usd_0 + buy_usd_1
    if total <= 0:
        return 0.0, 0.0, sample_size
    bias = (buy_usd_0 - buy_usd_1) / total
    return round(bias, 4), round(total, 2), sample_size


class ClobBookSync:
    """Orchestrator: тянет top-markets из БД, забирает /trades per market,
    агрегирует taker flow, upsert-ит bias в maker_direction_cache.

    Имя класса сохранено для обратной совместимости (cron_runner + тесты)."""

    def __init__(self, repo: MakerDirectionRepository | None = None) -> None:
        self.repo = repo or MakerDirectionRepository()

    async def run(self) -> dict:
        markets = await self._fetch_top_markets()
        logger.info("taker_flow_sync_start", markets=len(markets))

        # Phase 2: читаем предыдущие ratios ДО upsert для delta-детекции.
        market_ids = [m["id"] for m in markets]
        prev_ratios = await self._fetch_previous_ratios(market_ids)
        for m in markets:
            m["previous_maker_direction_ratio"] = prev_ratios.get(str(m["id"]))

        semaphore = asyncio.Semaphore(_CONCURRENCY)
        new_ratios: dict[str, float] = {}

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_REQUEST_TIMEOUT),
            # БЕЗ proxy — read-only data-api endpoint
            trust_env=False,
        ) as client:
            tasks = [self._process_market(client, semaphore, m, new_ratios) for m in markets]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        errors = sum(1 for r in results if isinstance(r, Exception))
        skipped = sum(1 for r in results if r == "skipped")
        upserts = len(results) - errors - skipped

        logger.info(
            "taker_flow_sync_done",
            markets=len(markets),
            upserts=upserts,
            skipped=skipped,
            errors=errors,
        )

        # Phase 2: триггер oracle для рынков с |Δ| ≥ 0.10.
        # await вместо create_task — иначе cron_runner закроет DB pool раньше.
        await self._trigger_changed_markets(markets, new_ratios)

        return {
            "markets": len(markets),
            "upserts": upserts,
            "skipped": skipped,
            "errors": errors,
        }

    async def _fetch_previous_ratios(self, market_ids: list) -> dict[str, float]:
        """Читает текущие maker_direction_ratio из кэша (до обновления)."""
        if not market_ids:
            return {}
        try:
            rows = await fetch(
                """SELECT market_id::text AS market_id, maker_direction_ratio
                   FROM maker_direction_cache
                   WHERE market_id = ANY($1::uuid[])""",
                market_ids,
            )
            return {str(row["market_id"]): float(row["maker_direction_ratio"]) for row in rows}
        except Exception as e:
            logger.warning("fetch_previous_ratios_failed", error=str(e))
            return {}

    async def _trigger_changed_markets(
        self,
        markets: list[dict],  # noqa: ARG002
        new_ratios: dict[str, float],  # noqa: ARG002
    ) -> None:
        """Phase 2 (deprecated 2026-04-30): раньше триггерил oracle на Δ taker flow.

        Pipeline V2 не использует oracle как primary signal. Oracle recalc cron
        каждые 6h обновляет данные для legacy и потенциальных feature-detectors.
        Метод оставлен пустым для совместимости (вызывается из run()).
        """
        return

    async def _fetch_top_markets(self) -> list[dict]:
        """Top-N активных рынков по 24h-обороту."""
        rows = await fetch(
            """
            SELECT id, condition_id, clob_token_ids
            FROM markets
            WHERE is_active = true
              AND condition_id IS NOT NULL
            ORDER BY volume_24h DESC NULLS LAST
            LIMIT $1
            """,
            _TOP_MARKETS_LIMIT,
        )
        return [dict(r) for r in rows]

    async def _process_market(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        market: dict,
        new_ratios: dict[str, float] | None = None,
    ) -> str | None:
        condition_id = market.get("condition_id")
        if not condition_id:
            return "skipped"

        async with semaphore:
            try:
                trades = await self._fetch_trades(client, condition_id)
            except Exception as exc:
                logger.warning(
                    "taker_flow_market_error",
                    market_id=str(market["id"]),
                    error=str(exc),
                )
                raise

        if not trades:
            return "skipped"

        taker_bias, total_buy_usd, sample_size = compute_taker_bias(trades)

        # Фильтр noise: рынки без значимого объёма BUY трейдов
        if total_buy_usd < _MIN_VOLUME_USD:
            return "skipped"

        # Для обратной совместимости со схемой maker_direction_cache:
        # maker_direction_ratio ← taker_bias (тот же диапазон [-1, 1], но из executed trades)
        # depth_imbalance      ← None (поле не актуально для taker flow)
        # bid_depth_usd        ← total_buy_usd (переиспользуем как "total volume")
        # ask_depth_usd        ← 0 (не актуально)
        # maker_count          ← sample_size (количество обработанных BUY трейдов)
        await self.repo.upsert(
            market_id=market["id"],
            condition_id=condition_id,
            maker_direction_ratio=taker_bias,
            depth_imbalance=None,
            bid_depth_usd=total_buy_usd,
            ask_depth_usd=0.0,
            maker_count=sample_size,
        )

        if new_ratios is not None:
            new_ratios[str(market["id"])] = taker_bias
        return None

    async def _fetch_trades(self, client: httpx.AsyncClient, condition_id: str) -> list[dict]:
        """Забирает последние N трейдов для рынка из data-api.

        Retry на 429 (rate limit) и 5xx с exponential backoff.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await client.get(
                    _TRADES_URL,
                    params={"market": condition_id, "limit": _TRADES_LIMIT},
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(_RETRY_BACKOFF * (2**attempt))
                        continue
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, list) else []
            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code == 429 and attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF * (2**attempt))
                    continue
                raise
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                last_exc = e
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF * (2**attempt))
                    continue
                raise
        if last_exc:
            raise last_exc
        return []


async def run_clob_book_sync() -> dict:
    """Точка входа для cron_runner."""
    sync = ClobBookSync()
    return await sync.run()
