"""
Transaction Processing Pipeline

Handler для обработки входящих транзакций.
Steps 1-4: базовый pipeline (idempotency, whale, store, volume).
Steps 4.5-4.8: аналитические метрики (PnL, timing, arbitrage, Bayesian input).
Step 5: Oracle Score recalc.
Step 5.5: Whale Alert notification.
Step 5.6: Custom alerts.
"""

import asyncio
import logging
from decimal import Decimal

from app.core.task_queue import enqueue
from app.handlers.whale_watcher import (
    check_transaction_exists,
    create_or_get_whale,
    store_transaction,
    update_whale_volume,
)
from app.services.database import execute, fetch, fetchval, records_to_list

logger = logging.getLogger(__name__)


def build_enriched_transaction_payload(
    tx: dict,
    whale: dict,
    market: dict | None,
    anomaly: dict | None,
) -> dict:
    """Создать обогащённый payload для WebSocket broadcast."""
    wins = int(whale.get("wins") or 0)
    losses = int(whale.get("losses") or 0)
    win_rate = wins / max(wins + losses, 1)

    return {
        "id": tx.get("id"),
        "tx_hash": tx.get("tx_hash"),
        "market_id": tx.get("market_id"),
        "market_title": market.get("question") if market else None,
        "direction": tx.get("direction"),
        "outcome": tx.get("outcome"),
        "amount": float(tx.get("amount", 0)),
        "price": float(tx.get("price") or 0.5),
        "category": market.get("category") if market else None,
        "is_whale": True,
        "created_at": tx.get("created_at"),
        "whale": {
            "id": whale.get("id"),
            "address": whale.get("address"),
            "win_rate": round(win_rate, 3),
            "roi_percent": float(whale.get("roi_percent") or 0),
            "total_volume": float(whale.get("total_volume") or 0),
            "expertise": whale.get("category_expertise"),
            "pnl_usd": float(whale.get("pnl_usd") or 0),
            "is_followed": None,
        },
        "anomaly": anomaly
        or {
            "is_anomalous": False,
            "flags": [],
            "risk_label": "clean",
            "account_age_days": 0,
            "unique_markets": 0,
            "total_trades": 0,
            "volume_concentration": 0.0,
        },
        "market_context": {
            "current_price": float(market.get("current_price") or 0.5) if market else 0.5,
            "oracle_score": float(market.get("oracle_score") or 0.5) if market else 0.5,
            "volume_24h": float(market.get("volume_24h") or 0) if market else 0,
        },
    }


async def process_transaction_event(data: dict):
    """
    Обработка новой транзакции.

    Steps:
    1. Idempotency check
    2. Resolve whale ID
    3. Store transaction
    4. Update volume
    4.5. Compute whale metrics (PnL/ROI/W/L)
    4.6. Compute timing score
    4.7. Check arbitrage
    4.8. Prepare Bayesian input
    5. Trigger Oracle Score recalculation (skip если арбитраж)
    5.5. Trigger Whale Alert notification (≥$50K, не арбитраж)
    5.6. Custom alerts
    """
    tx_hash = data.get("tx_hash")
    log_index = data.get("log_index", 0)
    from_address = data.get("from_address", "").lower()
    to_address = data.get("to_address", "").lower()
    amount_str = data.get("amount", "0")
    amount = Decimal(amount_str)
    direction = data.get("direction")
    market_id = data.get("market_id")
    is_whale_transaction = data.get("is_whale", False)
    asset = data.get("asset")
    r2_archive_key = data.get("r2_archive_key")
    block_number = data.get("block_number")
    block_timestamp = data.get("block_timestamp")
    outcome = data.get("outcome") or "YES"
    price_str = data.get("price")
    market_title = data.get("market_title")
    market_slug = data.get("market_slug")

    # Определяем is_taker: whale = taker если from_address == taker_address
    taker_addr = (data.get("taker_address") or "").lower()
    maker_addr = (data.get("maker_address") or "").lower()
    is_taker: bool | None = None
    if taker_addr and from_address:
        is_taker = from_address == taker_addr
    elif maker_addr and from_address:
        is_taker = from_address != maker_addr

    logger.info(f"Processing transaction {tx_hash} (log: {log_index})")

    # 1. Idempotency Check
    async def check_exists():
        return await check_transaction_exists(tx_hash, log_index)

    already_processed = await check_exists()
    if already_processed:
        logger.info(f"Transaction {tx_hash}:{log_index} already processed, skipping")
        return {"status": "skipped", "reason": "duplicate", "tx_hash": tx_hash}

    # 2. Resolve Whale ID
    async def get_whale():
        return await create_or_get_whale(from_address)

    whale = await get_whale()
    whale_id = whale["id"]

    # Динамическая подписка нового кита на WebSocket
    try:
        from app.services.polymarket_ws_service import get_ws_service

        ws = get_ws_service()
        if from_address and from_address not in ws._subscribed_wallets:
            await ws.subscribe_wallet(from_address)
    except Exception:
        pass  # fire-and-forget

    # 3. Store Transaction
    async def store_tx():
        return await store_transaction(
            tx_hash=tx_hash,
            whale_id=whale_id,
            amount=amount,
            direction=direction,
            outcome=outcome,
            r2_archive_key=r2_archive_key,
            block_number=block_number,
            block_timestamp=block_timestamp,
            market_id=market_id,
            is_whale=is_whale_transaction,
            log_index=log_index,
            from_address=from_address,
            to_address=to_address,
            asset=asset,
            price=price_str,
            market_title=market_title,
            market_slug=market_slug,
            is_taker=is_taker,
        )

    await store_tx()

    # Fire-and-forget: обогатить is_taker через Data API если WS не дал
    if is_taker is None and from_address and data.get("condition_id"):
        asyncio.create_task(_enrich_is_taker(tx_hash, from_address, data.get("condition_id", "")))

    # 4. Update Volume
    async def update_volume():
        await update_whale_volume(whale_id, amount)
        return {"updated": True}

    await update_volume()

    # --- Step 4.5: Compute Whale Metrics ---
    async def compute_whale_metrics():
        """
        Расчёт PnL/ROI/W/L для кита.

        Вызывается на каждую транзакцию, но результаты кэшируются.
        Тяжёлый расчёт (fetch positions из Data API) — запускается
        только если metrics_updated_at > 1 часа назад.
        """
        from app.core.cache import cache_get, cache_set
        from app.handlers.whale_analytics import get_whale_analytics_engine

        cache_key = f"whale_metrics:{whale_id}"
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached

        engine = get_whale_analytics_engine()
        try:
            metrics = await engine.compute_full_metrics(from_address)
            result = {
                "wins": metrics["metrics"].wins,
                "losses": metrics["metrics"].losses,
                "pnl_usd": float(metrics["metrics"].pnl),
                "roi_percent": float(metrics["metrics"].roi),
                "is_arbitrageur": metrics["is_arbitrageur"],
            }
            await cache_set(cache_key, result, ttl_seconds=1800)
            return result
        except Exception as e:
            logger.warning(f"whale_metrics_failed: whale_id={whale_id}, error={e}")
            return {"wins": 0, "losses": 0, "pnl_usd": 0, "roi_percent": 0, "is_arbitrageur": False}

    # --- Steps 4.5, 4.6, 4.7 — параллельно (независимы друг от друга) ---

    async def compute_timing_score():
        """
        Timing Score для текущей транзакции.

        entry_price = цена из транзакции (price_str)
        current_price = цена рынка из маркет-записи
        Δt = 0 (сделка только что произошла)

        Timing Score = (P_current - P_entry) × e^(-λ × Δt)
        При Δt=0: TS = P_current - P_entry (чистый price delta)
        """
        from app.handlers.whale_analytics import get_whale_analytics_engine

        engine = get_whale_analytics_engine()
        try:
            market_price = await fetchval("SELECT current_price FROM markets WHERE id = $1", market_id)
            current_price = float(market_price) if market_price is not None else 0.5
            entry_price = float(price_str) if price_str else current_price

            ts = engine.calculate_timing_score(
                entry_price=entry_price,
                current_price=current_price,
                hours_since_entry=0.0,
                direction=direction or "buy",
            )

            # Update transaction с timing_score
            await execute(
                "UPDATE transactions SET timing_score = $1 WHERE tx_hash = $2 AND log_index = $3",
                ts,
                tx_hash,
                log_index,
            )

            return {"timing_score": ts, "entry_price": entry_price, "current_price": current_price}
        except Exception as e:
            logger.warning(f"timing_score_failed: tx_hash={tx_hash}, error={e}")
            return {"timing_score": 0.0, "entry_price": 0.5, "current_price": 0.5}

    async def check_arbitrage():
        """
        Проверка: является ли текущая сделка арбитражной.

        Загружает последние 20 транзакций кита на этом рынке.
        detect_arbitrage() ищет пары BUY YES + BUY NO.
        """
        from app.handlers.whale_analytics import get_whale_analytics_engine

        # Только для BUY-direction — SELL не может быть частью арбитражной пары
        if direction != "buy":
            return {"is_arbitrage": False, "is_arbitrageur": False}

        engine = get_whale_analytics_engine()
        try:
            recent_rows = await fetch(
                """
                SELECT tx_hash, from_address, market_id, direction, outcome, price, amount, block_timestamp
                FROM transactions
                WHERE whale_id = $1 AND market_id = $2
                ORDER BY created_at DESC
                LIMIT 20
                """,
                whale_id,
                market_id,
            )

            txs = records_to_list(recent_rows)
            arb_hashes = engine.detect_arbitrage(txs)
            is_arb = tx_hash in arb_hashes

            if is_arb:
                await execute(
                    "UPDATE transactions SET is_arbitrage = TRUE WHERE tx_hash = $1 AND log_index = $2",
                    tx_hash,
                    log_index,
                )

            # Проверяем, является ли кошелёк арбитражёром в целом
            all_whale_rows = await fetch(
                """
                SELECT tx_hash, from_address, market_id, direction, outcome, price, amount, block_timestamp
                FROM transactions
                WHERE whale_id = $1
                ORDER BY created_at DESC
                LIMIT 100
                """,
                whale_id,
            )

            is_arb_wallet = engine.is_arbitrageur(records_to_list(all_whale_rows))
            if is_arb_wallet:
                await execute(
                    "UPDATE whales SET is_arbitrageur = TRUE WHERE id = $1",
                    whale_id,
                )

            return {"is_arbitrage": is_arb, "is_arbitrageur": is_arb_wallet}
        except Exception as e:
            logger.warning(f"arbitrage_check_failed: tx_hash={tx_hash}, error={e}")
            return {"is_arbitrage": False, "is_arbitrageur": False}

    whale_metrics, timing_data, arb_data = await asyncio.gather(
        compute_whale_metrics(),
        compute_timing_score(),
        check_arbitrage(),
    )

    # --- Step 4.8: Prepare Bayesian Input ---
    async def prepare_bayesian_input():
        """
        Собрать dict для BayesianScoreCalculator.

        Bankroll оценивается из позиций Polymarket или DEFAULT_BANKROLL.
        """
        from app.handlers.whale_analytics import get_whale_analytics_engine

        engine = get_whale_analytics_engine()

        # Оценить bankroll из позиций Polymarket
        bankroll = await engine.estimate_bankroll(from_address)

        entry_price = float(price_str) if price_str else 0.5
        current_price = timing_data.get("current_price", 0.5)

        # Загрузить token_outcomes для нормализации named outcomes
        _token_outcomes = None
        if market_id:
            _to_raw = await fetchval("SELECT token_outcomes FROM markets WHERE id = $1", market_id)
            if _to_raw:
                import json as _json_to

                _token_outcomes = _json_to.loads(_to_raw) if isinstance(_to_raw, str) else _to_raw

        bi = engine.prepare_bayesian_input(
            whale_address=from_address,
            stake=amount,
            market_price=current_price,
            direction=direction or "buy",
            outcome=outcome,
            bankroll_usd=bankroll,
            wins=whale_metrics.get("wins", 0),
            losses=whale_metrics.get("losses", 0),
            entry_price=entry_price,
            hours_since_entry=0.0,
            expert_weight=float(whale.get("expert_weight", 0.5)),
            roi_percent=float(whale_metrics.get("roi_percent", 0)),
            bet_count=int(whale.get("bet_count", 0) or 0),
            token_outcomes=_token_outcomes,
        )

        # Sector win rates + category для category-aware profile multiplier
        import json as _json

        raw_swr = whale.get("sector_win_rates")
        if raw_swr:
            bi["sector_win_rates"] = _json.loads(raw_swr) if isinstance(raw_swr, str) else raw_swr

        if market_id:
            cat_row = await fetchval("SELECT category FROM markets WHERE id = $1", market_id)
            if cat_row:
                bi["market_category"] = cat_row

        return bi

    bayesian_input = await prepare_bayesian_input()

    # 4.9. (удалён 2026-06-05) Мёртвый D1 FadeWhale shadow-emit: модуль
    # app.detectors.d1_fade_whale не существует (есть только .pyc от старой версии),
    # преемник — d26_fade_whale_extreme в штатном detector-pipeline. Блок ловил
    # ImportError в try/except и спамил `d1_shadow_emit_failed` на КАЖДОЙ whale-tx
    # (каждые ~5 мин в worker-логах). Fade-whale теперь полностью покрыт D26.
    is_arb = arb_data.get("is_arbitrage", False)

    # 5. Trigger Oracle Score Recalculation
    # SKIP если сделка — арбитраж (шум, не conviction)

    if market_id and not is_arb:
        await enqueue(
            "oracle:queue:recalculate",
            {
                "market_id": market_id,
                "latest_bayesian_input": bayesian_input,
            },
        )
    elif is_arb:
        logger.info(f"skipping_recalc_arbitrage: tx_hash={tx_hash}, market_id={market_id}")

    return {
        "status": "processed",
        "tx_hash": tx_hash,
        "log_index": log_index,
        "whale_id": whale_id,
        "whale_metrics": whale_metrics,
        "timing_data": timing_data,
        "arb_data": arb_data,
    }


async def _enrich_is_taker(tx_hash: str, address: str, condition_id: str) -> None:
    """Fire-and-forget: обогатить is_taker после записи транзакции."""
    try:
        from app.services.taker_service import get_taker_service

        svc = get_taker_service()
        result = await svc.resolve_is_taker(address, condition_id, tx_hash)
        if result is not None:
            await execute(
                "UPDATE transactions SET is_taker = $1 WHERE tx_hash = $2 AND is_taker IS NULL",
                result,
                tx_hash,
            )
    except Exception:
        pass  # backfill подберёт
