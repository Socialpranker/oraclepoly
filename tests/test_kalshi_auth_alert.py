"""Алерт при KalshiAuthError: TG-уведомление с debounce (не чаще раза в 6ч).

Мотив (аудит пайплайна 2026-07-05): kalshi-джобы на протухший/отсутствующий ключ
молча возвращали status=skipped — ingestion Kalshi останавливался на
неопределённый срок без уведомления (у Polymarket есть api-health монитор,
у Kalshi аналога нет).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.kalshi_service import alert_kalshi_auth_failure


@pytest.mark.asyncio
async def test_alert_sent_when_debounce_acquired():
    with (
        patch("app.services.kalshi_service.debounce_acquire", new=AsyncMock(return_value=True)),
        patch("app.tasks.notifications.send_admin_message", new=AsyncMock()) as send,
    ):
        await alert_kalshi_auth_failure("kalshi_market_sync", "no private key")

    send.assert_awaited_once()
    msg = send.await_args.args[0]
    assert "kalshi_market_sync" in msg
    assert "no private key" in msg


@pytest.mark.asyncio
async def test_alert_debounced_not_sent():
    with (
        patch("app.services.kalshi_service.debounce_acquire", new=AsyncMock(return_value=False)),
        patch("app.tasks.notifications.send_admin_message", new=AsyncMock()) as send,
    ):
        await alert_kalshi_auth_failure("kalshi_market_sync", "no private key")

    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_alert_never_raises():
    """Алертинг не роняет джобу — ни при падении debounce, ни при падении TG."""
    with (
        patch("app.services.kalshi_service.debounce_acquire", new=AsyncMock(side_effect=RuntimeError)),
        patch("app.tasks.notifications.send_admin_message", new=AsyncMock(side_effect=RuntimeError)),
    ):
        await alert_kalshi_auth_failure("kalshi_orderbook_sync", "expired key")  # не бросает
