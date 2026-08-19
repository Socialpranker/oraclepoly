"""Withdrawal flow (Phase 1 Stage 7): propose → approve/reject → execute.

Money-критичный код: extra paranoid TDD. Все DB операции замоканы.
Реальные USDC transfers (_polymarket_withdraw_usdc) — NotImplementedError,
worker ловит и пишет status='failed', deny by design.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.api.telegram import CallbackQuery, TelegramUser, _handle_withdrawal_callback
from app.jobs.withdrawal_propose import run_propose_withdrawal
from app.tasks.withdrawals import execute_withdrawal

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_settings(**overrides) -> object:
    """Plain object для patching get_settings — нужен sync attribute access."""
    defaults = {
        "withdrawal_enabled": False,
        "withdrawal_destination_address": "0x1234567890abcdef1234567890abcdef12345678",
        "withdrawal_min_amount_usd": 20.0,
        "withdrawal_proposal_ttl_hours": 48,
        "withdrawal_max_pct_of_monthly_profit": 0.50,
        "admin_telegram_ids": [12345],
    }
    defaults.update(overrides)
    return type("S", (), defaults)()


def _make_callback(user_id: int, data: str) -> CallbackQuery:
    return CallbackQuery(
        id=f"cq_{user_id}",
        **{"from": TelegramUser(id=user_id, is_bot=False, first_name="Admin")},
        data=data,
    )


def _make_transaction_mock() -> tuple[object, AsyncMock]:
    """Mock app.api.telegram.transaction → AsyncMock для conn.execute."""
    conn_execute_mock = AsyncMock()

    class _Conn:
        execute = conn_execute_mock

    @asynccontextmanager
    async def _transaction():
        yield _Conn()

    return _transaction, conn_execute_mock


# ── 7.3 propose job ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_propose_skipped_when_disabled():
    with patch("app.jobs.withdrawal_propose.get_settings", lambda: _make_settings(withdrawal_enabled=False)):
        result = await run_propose_withdrawal()
    assert result["status"] == "disabled"


@pytest.mark.asyncio
async def test_propose_skipped_when_no_destination():
    settings = _make_settings(withdrawal_enabled=True, withdrawal_destination_address="")
    with patch("app.jobs.withdrawal_propose.get_settings", lambda: settings):
        result = await run_propose_withdrawal()
    assert result["status"] == "no_destination"


@pytest.mark.asyncio
async def test_propose_skipped_when_no_profit():
    """Прошлый месяц = $0 profit → no propose, no DB INSERT."""
    settings = _make_settings(withdrawal_enabled=True)
    fetchrow_mock = AsyncMock(return_value={"profit": 0.0})

    with (
        patch("app.jobs.withdrawal_propose.get_settings", lambda: settings),
        patch("app.jobs.withdrawal_propose.fetchrow", fetchrow_mock),
        patch("app.tasks.notifications.send_admin_message", AsyncMock()),
    ):
        result = await run_propose_withdrawal()

    assert result["status"] == "no_profit"
    # Только SELECT profit, никаких INSERT
    assert fetchrow_mock.call_count == 1


@pytest.mark.asyncio
async def test_propose_skipped_when_below_min():
    """profit $30 → 50% = $15 < min $20 → below_min."""
    settings = _make_settings(withdrawal_enabled=True, withdrawal_min_amount_usd=20.0)
    fetchrow_mock = AsyncMock(return_value={"profit": 30.0})

    with (
        patch("app.jobs.withdrawal_propose.get_settings", lambda: settings),
        patch("app.jobs.withdrawal_propose.fetchrow", fetchrow_mock),
        patch("app.tasks.notifications.send_admin_message", AsyncMock()),
    ):
        result = await run_propose_withdrawal()

    assert result["status"] == "below_min"
    assert result["proposed"] == 15.0


@pytest.mark.asyncio
async def test_propose_creates_row_and_sends_tg():
    """profit $200 → 50% = $100 ≥ min $20 → INSERT + TG send."""
    settings = _make_settings(withdrawal_enabled=True)

    # fetchrow called twice: SELECT profit, INSERT RETURNING id
    fetchrow_mock = AsyncMock(side_effect=[
        {"profit": 200.0},
        {"id": "11111111-1111-1111-1111-111111111111"},
    ])
    tg_send_mock = AsyncMock()

    with (
        patch("app.jobs.withdrawal_propose.get_settings", lambda: settings),
        patch("app.jobs.withdrawal_propose.fetchrow", fetchrow_mock),
        patch("app.tasks.notifications.send_admin_message_with_keyboard", tg_send_mock),
    ):
        result = await run_propose_withdrawal()

    assert result["status"] == "proposed"
    assert result["amount"] == 100.0
    assert result["id"] == "11111111-1111-1111-1111-111111111111"
    tg_send_mock.assert_called_once()
    # Buttons содержат withdrawal_id
    _, buttons = tg_send_mock.call_args[0]
    assert any("approve:11111111" in b["callback_data"] for b in buttons)
    assert any("reject:11111111" in b["callback_data"] for b in buttons)


# ── 7.4 TG callback ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_rejected_for_non_admin():
    """user_id не в admin_telegram_ids → 'Not authorized', никаких DB write."""
    cq = _make_callback(user_id=99999, data="withdraw:approve:abc")
    transaction_mock, execute_mock = _make_transaction_mock()
    answer_mock = AsyncMock()

    with (
        patch("app.api.telegram.get_settings", lambda: _make_settings(admin_telegram_ids=[12345])),
        patch("app.api.telegram.fetchrow", AsyncMock()),
        patch("app.api.telegram.transaction", transaction_mock),
        patch("app.api.telegram.answer_callback_query", answer_mock),
    ):
        result = await _handle_withdrawal_callback(cq, "99999")

    assert result["result"] == "not_authorized"
    assert execute_mock.call_count == 0
    answer_mock.assert_called_once()
    assert "authorized" in answer_mock.call_args[0][1].lower()


@pytest.mark.asyncio
async def test_approve_rejected_when_expired():
    """expires_at в прошлом → status='expired', не 'approved'."""
    cq = _make_callback(user_id=12345, data="withdraw:approve:abc")
    transaction_mock, execute_mock = _make_transaction_mock()

    past = datetime.now(UTC) - timedelta(hours=1)
    fetchrow_mock = AsyncMock(return_value={
        "id": "abc",
        "status": "proposed",
        "expires_at": past,
        "amount_usd": 100.0,
    })

    with (
        patch("app.api.telegram.get_settings", lambda: _make_settings(admin_telegram_ids=[12345])),
        patch("app.api.telegram.fetchrow", fetchrow_mock),
        patch("app.api.telegram.transaction", transaction_mock),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
    ):
        result = await _handle_withdrawal_callback(cq, "12345")

    assert result["result"] == "expired"
    # UPDATE для пометки expired
    assert execute_mock.call_count == 1
    assert "status='expired'" in str(execute_mock.call_args_list[0])


@pytest.mark.asyncio
async def test_approve_rejected_when_wrong_status():
    """status='completed' уже → reject, не 'already approved' нельзя re-approve."""
    cq = _make_callback(user_id=12345, data="withdraw:approve:abc")
    transaction_mock, execute_mock = _make_transaction_mock()
    fetchrow_mock = AsyncMock(return_value={
        "id": "abc",
        "status": "completed",
        "expires_at": datetime.now(UTC) + timedelta(hours=24),
        "amount_usd": 100.0,
    })

    with (
        patch("app.api.telegram.get_settings", lambda: _make_settings(admin_telegram_ids=[12345])),
        patch("app.api.telegram.fetchrow", fetchrow_mock),
        patch("app.api.telegram.transaction", transaction_mock),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
    ):
        result = await _handle_withdrawal_callback(cq, "12345")

    assert result["result"] == "wrong_status"
    assert execute_mock.call_count == 0


@pytest.mark.asyncio
async def test_approve_updates_status_and_publishes_redis():
    """Happy path: status='proposed', not expired, admin → 'approved' + Redis publish."""
    cq = _make_callback(user_id=12345, data="withdraw:approve:abc")
    transaction_mock, execute_mock = _make_transaction_mock()
    fetchrow_mock = AsyncMock(return_value={
        "id": "abc",
        "status": "proposed",
        "expires_at": datetime.now(UTC) + timedelta(hours=24),
        "amount_usd": 100.0,
    })

    redis_mock = AsyncMock()
    redis_mock.publish = AsyncMock()

    with (
        patch("app.api.telegram.get_settings", lambda: _make_settings(admin_telegram_ids=[12345])),
        patch("app.api.telegram.fetchrow", fetchrow_mock),
        patch("app.api.telegram.transaction", transaction_mock),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.core.cache.get_redis", AsyncMock(return_value=redis_mock)),
    ):
        result = await _handle_withdrawal_callback(cq, "12345")

    assert result["action"] == "approve"
    assert result["result"] == "ok"
    assert execute_mock.call_count == 1
    assert "approved" in str(execute_mock.call_args_list[0])
    redis_mock.publish.assert_called_once_with("withdrawals:execute", "abc")


@pytest.mark.asyncio
async def test_reject_updates_status_no_redis():
    """Reject path: status='rejected', НЕ публикуется в Redis."""
    cq = _make_callback(user_id=12345, data="withdraw:reject:abc")
    transaction_mock, execute_mock = _make_transaction_mock()
    fetchrow_mock = AsyncMock(return_value={
        "id": "abc",
        "status": "proposed",
        "expires_at": datetime.now(UTC) + timedelta(hours=24),
        "amount_usd": 100.0,
    })
    redis_mock = AsyncMock()

    with (
        patch("app.api.telegram.get_settings", lambda: _make_settings(admin_telegram_ids=[12345])),
        patch("app.api.telegram.fetchrow", fetchrow_mock),
        patch("app.api.telegram.transaction", transaction_mock),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
        patch("app.core.cache.get_redis", AsyncMock(return_value=redis_mock)),
    ):
        result = await _handle_withdrawal_callback(cq, "12345")

    assert result["action"] == "reject"
    assert result["result"] == "ok"
    assert "rejected" in str(execute_mock.call_args_list[0])
    redis_mock.publish.assert_not_called()


# ── 7.5 worker (NotImplementedError stub) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_skips_non_approved_row():
    """status='proposed' → skip, не запускает _polymarket_withdraw_usdc."""
    fetchrow_mock = AsyncMock(return_value={
        "id": "abc",
        "status": "proposed",
        "amount_usd": 100.0,
        "destination_address": "0xdeadbeef",
    })
    with (
        patch("app.tasks.withdrawals.fetchrow", fetchrow_mock),
    ):
        result = await execute_withdrawal("abc")
    assert result["status"] == "skipped_status"


@pytest.mark.asyncio
async def test_execute_marks_failed_on_not_implemented():
    """NotImplementedError из _polymarket_withdraw_usdc → status='failed', TG alert."""
    transaction_mock, execute_mock = _make_transaction_mock()
    fetchrow_mock = AsyncMock(return_value={
        "id": "abc",
        "status": "approved",
        "amount_usd": 100.0,
        "destination_address": "0xdeadbeef",
    })
    tg_mock = AsyncMock()

    with (
        patch("app.tasks.withdrawals.fetchrow", fetchrow_mock),
        patch("app.tasks.withdrawals.transaction", transaction_mock),
        patch("app.tasks.withdrawals.send_admin_message", tg_mock),
    ):
        result = await execute_withdrawal("abc")

    assert result["status"] == "failed"
    assert "NotImplementedError" in result["reason"]
    # UPDATE status='failed' с failure_reason
    assert execute_mock.call_count == 1
    assert "failed" in str(execute_mock.call_args_list[0])
    tg_mock.assert_called_once()
