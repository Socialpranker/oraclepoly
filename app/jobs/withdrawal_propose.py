"""Monthly withdrawal proposal job (Phase 1 Stage 7).

Runs 1-го числа 09:00 UTC. Считает profit за прошлый календарный месяц,
формирует proposal на 50% от profit, отправляет TG с inline keyboard.

Source of truth: execution_orders.net_pnl где status='settled' AND
execution_mode != 'virtual' (только real trades).

При withdrawal_enabled=False — skip без ошибки. Это default state до
verify Polymarket withdraw SDK (Stage 7 follow-up).
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.core.config import get_settings
from app.services.database import fetchrow

logger = structlog.get_logger()


async def run_propose_withdrawal() -> dict[str, Any]:
    """Считает monthly profit и создаёт proposed withdrawal row + TG message.

    Returns dict со status, чтобы cron-runner логировал результат:
      - disabled        — withdrawal_enabled=False
      - no_destination  — destination address пуст
      - no_profit       — profit <= 0 (skip без alert)
      - below_min       — proposed amount < withdrawal_min_amount_usd
      - proposed        — row создан, TG отправлен
    """
    settings = get_settings()

    if not settings.withdrawal_enabled:
        logger.info("withdrawal_propose_disabled")
        return {"status": "disabled"}

    if not settings.withdrawal_destination_address:
        # Не должно случиться — lifespan check ловит это раньше. Belt-and-suspenders.
        logger.error("withdrawal_propose_no_destination_address")
        return {"status": "no_destination"}

    # Profit за прошлый календарный UTC месяц.
    # execution_mode != 'virtual' — только real trades, не paper.
    row = await fetchrow(
        """
        SELECT COALESCE(SUM(net_pnl), 0)::float AS profit
          FROM execution_orders
         WHERE execution_mode != 'virtual'
           AND status = 'settled'
           AND settled_at >= date_trunc('month', now() AT TIME ZONE 'UTC') - INTERVAL '1 month'
           AND settled_at <  date_trunc('month', now() AT TIME ZONE 'UTC')
        """
    )
    monthly_profit = float(row["profit"] if row else 0.0)

    if monthly_profit <= 0:
        await _notify_no_profit(monthly_profit)
        return {"status": "no_profit", "monthly_profit": monthly_profit}

    proposed_amount = round(monthly_profit * settings.withdrawal_max_pct_of_monthly_profit, 2)

    if proposed_amount < settings.withdrawal_min_amount_usd:
        await _notify_below_min(proposed_amount, monthly_profit)
        return {"status": "below_min", "proposed": proposed_amount, "monthly_profit": monthly_profit}

    expires_at = datetime.now(UTC) + timedelta(hours=settings.withdrawal_proposal_ttl_hours)

    inserted = await fetchrow(
        """
        INSERT INTO withdrawals (
            amount_usd, destination_address, status, expires_at, monthly_profit_basis
        )
        VALUES ($1, $2, 'proposed', $3, $4)
        RETURNING id
        """,
        proposed_amount,
        settings.withdrawal_destination_address,
        expires_at,
        monthly_profit,
    )
    withdrawal_id = str(inserted["id"])

    await _send_proposal_tg(withdrawal_id, proposed_amount, monthly_profit, expires_at)

    logger.info(
        "withdrawal_proposed",
        id=withdrawal_id,
        amount=proposed_amount,
        monthly_profit=monthly_profit,
    )
    return {"status": "proposed", "id": withdrawal_id, "amount": proposed_amount}


async def _send_proposal_tg(
    withdrawal_id: str,
    amount: float,
    profit_basis: float,
    expires_at: datetime,
) -> None:
    from app.tasks.notifications import send_admin_message_with_keyboard

    settings = get_settings()
    addr = settings.withdrawal_destination_address
    addr_masked = f"{addr[:6]}...{addr[-4:]}" if len(addr) > 10 else addr

    text = (
        f"💰 <b>Withdrawal proposal</b>\n\n"
        f"Amount: <b>${amount:.2f}</b> "
        f"({int(settings.withdrawal_max_pct_of_monthly_profit * 100)}% от прошлого месяца "
        f"profit ${profit_basis:.2f})\n"
        f"Destination: <code>{addr_masked}</code>\n"
        f"Expires: {expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"ID: <code>{withdrawal_id}</code>"
    )
    buttons = [
        {"text": "✅ Approve", "callback_data": f"withdraw:approve:{withdrawal_id}"},
        {"text": "❌ Reject", "callback_data": f"withdraw:reject:{withdrawal_id}"},
    ]
    await send_admin_message_with_keyboard(text, buttons)


async def _notify_no_profit(monthly_profit: float) -> None:
    from app.tasks.notifications import send_admin_message

    await send_admin_message(f"ℹ️ Withdrawal propose: прошлый месяц profit ${monthly_profit:.2f} ≤ 0 — skip.")


async def _notify_below_min(proposed_amount: float, monthly_profit: float) -> None:
    from app.tasks.notifications import send_admin_message

    settings = get_settings()
    await send_admin_message(
        f"ℹ️ Withdrawal propose: proposed ${proposed_amount:.2f} "
        f"(50% от profit ${monthly_profit:.2f}) < min ${settings.withdrawal_min_amount_usd:.2f} — skip."
    )
