"""
Whale Watcher Handler

Processes whale transactions with strict idempotency.
Follows Rule #2: The Idempotency Rule.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from app.core.cache import cache_set
from app.repositories.transaction_repository import TransactionRepository
from app.services.database import fetchrow, fetchval, record_to_dict

logger = structlog.get_logger()


async def check_transaction_exists(tx_hash: str, log_index: int = 0) -> bool:
    """
    Check if a transaction event has already been processed.

    This is the core idempotency check. MUST be called before
    any processing logic to prevent duplicate signals.

    Args:
        tx_hash: Blockchain transaction hash
        log_index: Log index of the event within the transaction

    Returns:
        True if transaction event exists (should skip processing)
    """
    try:
        exists = await fetchval(
            "SELECT EXISTS(SELECT 1 FROM transactions WHERE tx_hash = $1 AND log_index = $2)",
            tx_hash,
            log_index,
        )
        return bool(exists)
    except Exception:
        return False


async def create_or_get_whale(address: str) -> dict[str, Any]:
    """
    Get existing whale profile or create a new one.

    Returns whale record with `_is_new` flag indicating if just created.

    Args:
        address: Wallet address

    Returns:
        Whale record from database (with _is_new flag)
    """
    repo = TransactionRepository()

    # Try to get existing whale
    whale = await repo.get_whale_by_address(address)
    if whale:
        whale["_is_new"] = False
        return whale

    # Create new whale profile
    new_whale = {
        "address": address,
        "expert_weight": 0.5,  # Default neutral weight
        "win_rate": 0.5,
        "total_volume": 0,
    }

    result = await repo.create_whale(new_whale)
    result["_is_new"] = True
    return result


async def store_transaction(
    tx_hash: str,
    whale_id: str,
    amount: Decimal,
    direction: str,
    outcome: str,
    r2_archive_key: str,
    block_number: int | None = None,
    block_timestamp: str | None = None,
    market_id: str | None = None,
    is_whale: bool = False,
    log_index: int = 0,
    from_address: str | None = None,
    to_address: str | None = None,
    asset: str | None = None,
    price: str | None = None,
    market_title: str | None = None,
    market_slug: str | None = None,
    is_taker: bool | None = None,
) -> dict[str, Any]:
    """
    Store a processed transaction in the database.

    All financial amounts must be Decimal (Rule #1).

    Args:
        tx_hash: Unique transaction hash
        whale_id: UUID of the whale
        amount: Transaction amount in USDC (Decimal, 6 decimals)
        direction: "buy" or "sell"
        outcome: "YES" or "NO"
        r2_archive_key: Path to full transaction data in R2
        block_number: Optional block number
        block_timestamp: Optional block timestamp
        market_id: Optional market ID
        is_whale: Whether it's a whale transaction
        log_index: Log index within transaction
        from_address: Sender address
        to_address: Receiver address
        asset: Asset symbol or address
        price: Trade price as string (e.g. "0.75"), stored as float
        market_title: Denormalized market question text
        market_slug: Polymarket market slug for deep links

    Returns:
        Created transaction record
    """
    row = await fetchrow(
        """
        INSERT INTO transactions (
            tx_hash, whale_id, market_id, is_whale, amount, direction, outcome,
            r2_archive_key, block_number, block_timestamp, log_index,
            from_address, to_address, asset, price, market_title, market_slug, is_taker
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7,
            $8, $9, $10, $11,
            $12, $13, $14, $15, $16, $17, $18
        )
        ON CONFLICT (tx_hash, log_index)
        DO UPDATE SET
            whale_id = EXCLUDED.whale_id,
            market_id = EXCLUDED.market_id,
            is_whale = EXCLUDED.is_whale,
            amount = EXCLUDED.amount,
            direction = EXCLUDED.direction,
            outcome = EXCLUDED.outcome,
            r2_archive_key = EXCLUDED.r2_archive_key,
            block_number = EXCLUDED.block_number,
            block_timestamp = EXCLUDED.block_timestamp,
            from_address = EXCLUDED.from_address,
            to_address = EXCLUDED.to_address,
            asset = EXCLUDED.asset,
            price = EXCLUDED.price,
            market_title = EXCLUDED.market_title,
            market_slug = EXCLUDED.market_slug,
            is_taker = EXCLUDED.is_taker
        RETURNING *
        """,
        tx_hash,
        whale_id,
        market_id,
        is_whale,
        float(amount),
        direction,
        outcome,
        r2_archive_key,
        block_number,
        datetime.fromisoformat(block_timestamp) if isinstance(block_timestamp, str) else block_timestamp,
        log_index,
        from_address,
        to_address,
        asset,
        float(price) if price else None,
        market_title,
        market_slug,
        is_taker,
    )

    result = record_to_dict(row)
    if result:
        await _maybe_trigger_event_snapshot(result, amount, market_id)
        return result
    raise Exception("Failed to store transaction")


# Threshold для event-shock snapshot triggering (whale_trade T1).
# См. docs/data_audit_2026-05-21.md C.6 #5 (orderbook event snapshots plan).
# 2026-06-08: $50k→$20k — при $50k recoverable-объём ~3.4/день (мало для
# mv_whale_shock_recovery). $20k даёт ~15/день, 4.3× больше данных (verified прод).
WHALE_SNAPSHOT_TRIGGER_USD = Decimal("20000")


async def _maybe_trigger_event_snapshot(
    transaction: dict[str, Any],
    amount: Decimal,
    market_id: str | None,
) -> None:
    """T1 whale-trade trigger: при trade ≥WHALE_SNAPSHOT_TRIGGER_USD spawn snapshot recorder.

    Non-blocking — ошибки логируются, не пропагируются (не блокировать
    основной transaction flow).
    """
    if amount < WHALE_SNAPSHOT_TRIGGER_USD or not market_id:
        return

    # Observability (2026-06-05): порог пройден — логируем КАЖДЫЙ trigger,
    # чтобы пустую вкладку Whale можно было диагностировать. Раньше 3 silent-return
    # ниже не оставляли следов → невозможно понять, доходит ли до record_event.
    logger.info("whale_snapshot_trigger_fired", market_id=str(market_id), amount_usd=float(amount))

    # M2 confound-флаг: whale-trade ≥$20K на этом рынке. M2-хук читает его
    # как whale_in_window (буфер 90s ≥ 60s окна). Дёшево, одна команда.
    await cache_set(f"m2:whale_seen:{market_id}", "1", ttl_seconds=90)

    try:
        # Fetch condition_id + clob_token_ids (один запрос на trigger).
        row = await fetchrow(
            "SELECT condition_id, clob_token_ids FROM markets WHERE id = $1",
            market_id,
        )
        if not row or not row.get("condition_id"):
            logger.warning("whale_snapshot_skip_no_condition", market_id=str(market_id), row_found=bool(row))
            return

        clob_token_ids_raw = row.get("clob_token_ids") or []
        if isinstance(clob_token_ids_raw, str):
            import json as _json

            try:
                clob_token_ids_raw = _json.loads(clob_token_ids_raw)
            except (ValueError, TypeError):
                clob_token_ids_raw = []

        token_ids = [str(t) for t in (clob_token_ids_raw or []) if t]
        if not token_ids:
            logger.warning("whale_snapshot_skip_no_tokens", market_id=str(market_id))
            return

        from app.services.snapshot_recorder import record_event

        await record_event(
            trigger_type="whale_trade",
            market_id=str(market_id),
            condition_id=str(row["condition_id"]),
            token_ids=token_ids,
            trigger_meta={
                "amount_usd": float(amount),
                "direction": transaction.get("direction"),
                "outcome": transaction.get("outcome"),
                "tx_hash": transaction.get("tx_hash"),
            },
        )
    except Exception as exc:
        logger.warning(
            "snapshot_trigger_whale_trade_failed",
            market_id=str(market_id) if market_id else None,
            amount=float(amount),
            error=str(exc),
        )


async def update_whale_volume(whale_id: str, amount: Decimal) -> None:
    """
    Update whale's total volume after a transaction.

    Args:
        whale_id: UUID of the whale
        amount: Transaction amount to add
    """
    repo = TransactionRepository()

    # Get current volume
    current_volume = await repo.get_whale_volume(whale_id)

    # Update volume
    new_volume = current_volume + amount
    await repo.update_whale_volume(whale_id, new_volume)
