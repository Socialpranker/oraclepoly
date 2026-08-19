"""Тесты app.jobs.wallet_health_check — конверт уведомлений (спека §3).

Тестов у джобы не было вообще, хотя она сторожит деньги: пустой баланс и нулевые
allowance означают, что live-ордера не пройдут. Первое, что здесь проверяется, —
факт доставки (риск №1 §10) и что дедуп не съедает разные проблемы.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.analytics.notification import Severity
from app.jobs.wallet_health_check import to_notifications


def test_no_issues_no_notification():
    assert to_notifications([], balance=100.0) == []


def test_issue_gets_human_text_not_raw_string():
    """Спека §1 п.2: владельцу идёт человеческий текст, технический — в evidence."""
    n = to_notifications([("zero_allowances", "🚨 USDC allowances = 0. Нажми Approve/Activate.")], balance=50.0)[0]

    assert "не разрешил бирже тратить USDC" in " ".join(n.what_happened)
    assert any("allowances = 0" in e for e in n.evidence)


def test_balance_is_named_because_it_is_a_real_figure():
    """§3 запрещает выдумывать цифру, но баланс — настоящий, его назвать МОЖНО."""
    n = to_notifications([("low_balance", "raw")], balance=12.34)[0]

    assert n.what_if_ignored is not None
    assert "12.34" in n.what_if_ignored


def test_unknown_balance_yields_no_money_claim():
    """Баланс неизвестен → никакой цифры в «если ничего не делать» (§3)."""
    n = to_notifications([("balance_fetch_failed", "raw")], balance=None)[0]

    assert n.what_if_ignored is None


def test_several_issues_merge_into_one_message():
    """Один кошелёк — одно сообщение: три отдельных про него это шум (§4)."""
    ns = to_notifications([("zero_balance", "a"), ("zero_allowances", "b")], balance=0.0)

    assert len(ns) == 1
    assert ns[0].severity == Severity.ACTION_REQUIRED
    assert len(ns[0].what_happened) == 2


def test_unknown_issue_key_falls_back_to_raw_text():
    """fail-open: незнакомый ключ не должен глотать сообщение целиком."""
    n = to_notifications([("brand_new_issue", "сырой текст про новую беду")], balance=5.0)[0]

    assert "сырой текст про новую беду" in " ".join(n.what_happened)


@pytest.mark.asyncio
async def test_run_publishes_fresh_issues():
    """Риск №1 §10: проблема кошелька обязана дойти до владельца."""
    from app.jobs.wallet_health_check import run_wallet_health_check

    redis = AsyncMock()
    redis.exists = AsyncMock(return_value=False)
    settings = type("S", (), {"execution_enabled": True})()

    with (
        patch("app.core.config.get_settings", return_value=settings),
        patch("app.core.cache.get_redis", new=AsyncMock(return_value=redis)),
        patch("app.services.wallet_resolver.get_funder_address", return_value="0xabcdef1234"),
        patch("app.services.wallet_resolver.fetch_balance", new=AsyncMock(return_value=0.0)),
        patch("app.services.wallet_resolver.fetch_allowance", new=AsyncMock(return_value={})),
        patch("app.jobs.wallet_health_check.publish", new=AsyncMock()) as m_publish,
    ):
        result = await run_wallet_health_check()

    m_publish.assert_awaited_once()
    assert m_publish.await_args.kwargs["actor"] == "wallet_health_check"
    assert "zero_balance" in result["alerts"]


@pytest.mark.asyncio
async def test_run_respects_own_dedup():
    """Свой per-issue дедуп остаётся (решение владельца): уже отправленное
    в течение часа не уходит повторно и не доходит до шины."""
    from app.jobs.wallet_health_check import run_wallet_health_check

    redis = AsyncMock()
    redis.exists = AsyncMock(return_value=True)  # алерт уже слали
    settings = type("S", (), {"execution_enabled": True})()

    with (
        patch("app.core.config.get_settings", return_value=settings),
        patch("app.core.cache.get_redis", new=AsyncMock(return_value=redis)),
        patch("app.services.wallet_resolver.get_funder_address", return_value="0xabcdef1234"),
        patch("app.services.wallet_resolver.fetch_balance", new=AsyncMock(return_value=0.0)),
        patch("app.services.wallet_resolver.fetch_allowance", new=AsyncMock(return_value={})),
        patch("app.jobs.wallet_health_check.publish", new=AsyncMock()) as m_publish,
    ):
        result = await run_wallet_health_check()

    m_publish.assert_not_awaited()
    assert result["alerts"] == []


@pytest.mark.asyncio
async def test_run_skipped_when_execution_disabled():
    """execution выключен — кошелёк не трогаем вообще (не спамим 401 в CLOB)."""
    from app.jobs.wallet_health_check import run_wallet_health_check

    settings = type("S", (), {"execution_enabled": False})()

    with (
        patch("app.core.config.get_settings", return_value=settings),
        patch("app.jobs.wallet_health_check.publish", new=AsyncMock()) as m_publish,
    ):
        result = await run_wallet_health_check()

    assert result["status"] == "skipped"
    m_publish.assert_not_awaited()
