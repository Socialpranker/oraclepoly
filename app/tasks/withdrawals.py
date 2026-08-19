"""Worker для executing approved withdrawals (Phase 1 Stage 7 stub).

Listens to Redis channel 'withdrawals:execute', читает withdrawal_id, executes
USDC transfer на whitelisted destination.

Stage 7 (current): `_polymarket_withdraw_usdc` = NotImplementedError. Реальная
интеграция отложена до verify Polymarket withdraw SDK (Stage 7 follow-up).

При попытке execute с withdrawal_enabled=true и approved row — fail loud:
status='failed', failure_reason='NotImplementedError', TG alert. Это
defence-in-depth: даже если кто-то approve до Stage 7 follow-up, USDC не
двигается, audit trail полный.
"""

from typing import Any

import structlog

from app.services.database import fetchrow, transaction
from app.tasks.notifications import send_admin_message

logger = structlog.get_logger()


async def execute_withdrawal(withdrawal_id: str) -> dict[str, Any]:
    """Read approved row → call Polymarket withdraw → update status.

    Returns dict со status:
      - skipped_status — row не в 'approved'
      - skipped_missing — row не существует
      - failed — exception при _polymarket_withdraw_usdc
      - completed — tx_hash записан
    """
    row = await fetchrow(
        "SELECT id, status, amount_usd, destination_address FROM withdrawals WHERE id = $1",
        withdrawal_id,
    )
    if row is None:
        logger.warning("execute_withdrawal_missing", id=withdrawal_id)
        return {"status": "skipped_missing", "id": withdrawal_id}
    if row["status"] != "approved":
        logger.warning("execute_withdrawal_skip", id=withdrawal_id, status=row["status"])
        return {"status": "skipped_status", "id": withdrawal_id, "actual_status": row["status"]}

    amount_usd = float(row["amount_usd"])
    destination = row["destination_address"]

    try:
        tx_hash = await _polymarket_withdraw_usdc(
            amount_usd=amount_usd,
            destination=destination,
        )
        async with transaction() as conn:
            await conn.execute(
                """
                UPDATE withdrawals
                   SET status='completed', tx_hash=$1, completed_at=NOW()
                 WHERE id=$2 AND status='approved'
                """,
                tx_hash,
                withdrawal_id,
            )
        await send_admin_message(f"✅ Withdrawal completed ${amount_usd:.2f}\ntx: <code>{tx_hash}</code>")
        logger.info("withdrawal_completed", id=withdrawal_id, amount=amount_usd, tx=tx_hash)
        return {"status": "completed", "id": withdrawal_id, "tx_hash": tx_hash}

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"[:500]
        async with transaction() as conn:
            await conn.execute(
                """
                UPDATE withdrawals
                   SET status='failed', failure_reason=$1
                 WHERE id=$2 AND status='approved'
                """,
                reason,
                withdrawal_id,
            )
        await send_admin_message(f"❌ Withdrawal failed ${amount_usd:.2f}\nreason: {reason}")
        logger.error("withdrawal_failed", id=withdrawal_id, amount=amount_usd, reason=reason)
        return {"status": "failed", "id": withdrawal_id, "reason": reason}


async def _polymarket_withdraw_usdc(*, amount_usd: float, destination: str) -> str:
    """Stage 7 follow-up: actual USDC transfer на Polygon.

    Возможные пути:
    - py-clob-client `client.withdraw(...)` если метод существует
    - Direct Polygon contract call: USDC.transfer(destination, amount * 1e6) через
      signer key (web3.py + Polymarket proxy wallet ABI)
    - Polymarket internal POST endpoint (если документирован)

    До verify SDK — NotImplementedError. Worker ловит, пишет status='failed'
    с reason, отправляет TG alert. USDC не двигается.
    """
    raise NotImplementedError("Stage 7 follow-up: verify Polymarket withdraw SDK method перед production use")
