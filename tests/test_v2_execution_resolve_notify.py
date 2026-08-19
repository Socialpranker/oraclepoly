"""Тесты per-trade TG-уведомления в _settle_filled (live WIN/LOSS).

Live (execution_mode IN ('maker', 'taker')) — уведомление сразу по каждой
settled-ставке. Virtual — не уведомляем здесь (сводка по virtual идёт отдельной
недельной cron-джобой, см. app/jobs/virtual_weekly_digest.py).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.jobs.v2_execution_resolve as resolve_mod


def _note_text(call_arg) -> str:
    """Склеивает поля уведомления в текст.

    Джобы отдают в publish() структуру, а не строку, но проверять надо СМЫСЛ
    сообщения — поэтому собираем текст из полей и оставляем assert'ы по
    подстрокам рабочими.
    """
    notes = call_arg if isinstance(call_arg, list) else [call_arg]
    parts = []
    for n in notes:
        parts += [n.headline, *n.what_happened, *(n.evidence or [])]
        parts += [n.what_to_do or "", n.what_if_ignored or ""]
    return " ".join(parts)



def _row(**overrides):
    row = {
        "id": "order-1",
        "execution_mode": "taker",
        "fill_price": 0.40,
        "stake": 10.0,
        "outcome": "WIN",
        "category": "crypto",
        "fee_schedule": None,
        "side": "YES",
        "market_id": "mkt-1",
        "question": "NVDA close above $180",
        "winner": "yes",  # live-outcome выводится из markets.winner (settle-гейт 2026-07-05)
        "as_penalty_usd": None,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_settle_filled_notifies_live_win(monkeypatch):
    """Live WIN settled-ставка → одно TG-сообщение с net PnL и вопросом рынка."""
    row = _row(outcome="WIN")

    async def _fake_fetch(sql, *args):
        return [row]

    execute_mock = AsyncMock()
    notify_mock = AsyncMock()
    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(resolve_mod, "execute", execute_mock)
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", notify_mock)

    await resolve_mod._settle_filled()

    notify_mock.assert_awaited_once()
    text = _note_text(notify_mock.await_args.args[0])
    assert "WIN" in text
    assert "NVDA close above $180" in text
    assert "10.00" in text  # stake


@pytest.mark.asyncio
async def test_settle_filled_notifies_live_loss(monkeypatch):
    row = _row(outcome="LOSS", winner="no")

    async def _fake_fetch(sql, *args):
        return [row]

    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(resolve_mod, "execute", AsyncMock())
    notify_mock = AsyncMock()
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", notify_mock)

    await resolve_mod._settle_filled()

    notify_mock.assert_awaited_once()
    text = _note_text(notify_mock.await_args.args[0])
    assert "LOSS" in text


@pytest.mark.asyncio
async def test_settle_filled_skips_live_expired_without_market_resolve(monkeypatch):
    """EXPIRED сигнала при нерезолвнутом рынке НЕ settle'ит live-ордер и не шлёт
    уведомление (settle-гейт 2026-07-05: live закрывается только по markets.winner;
    Python-страховка на случай дырки в SQL-гейте)."""
    row = _row(outcome="EXPIRED", winner=None)

    async def _fake_fetch(sql, *args):
        return [row]

    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)
    execute_mock = AsyncMock()
    monkeypatch.setattr(resolve_mod, "execute", execute_mock)
    notify_mock = AsyncMock()
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", notify_mock)

    n = await resolve_mod._settle_filled()

    assert n == 0
    execute_mock.assert_not_awaited()
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_settle_filled_does_not_notify_virtual(monkeypatch):
    """Virtual-ордера не шлют per-trade уведомление — только недельная сводка."""
    row = _row(execution_mode="virtual", outcome="WIN")

    async def _fake_fetch(sql, *args):
        return [row]

    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(resolve_mod, "execute", AsyncMock())
    notify_mock = AsyncMock()
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", notify_mock)

    await resolve_mod._settle_filled()

    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_settle_filled_notify_failure_does_not_break_settlement(monkeypatch):
    """Если TG упал — settlement (count) не должен ломаться, только warning в лог."""
    row = _row(outcome="WIN")

    async def _fake_fetch(sql, *args):
        return [row]

    execute_mock = AsyncMock()
    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(resolve_mod, "execute", execute_mock)
    monkeypatch.setattr(
        "app.jobs.v2_execution_resolve.publish", AsyncMock(side_effect=RuntimeError("TG down"))
    )

    count = await resolve_mod._settle_filled()

    assert count == 1
    execute_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_settle_filled_commits_db_before_sending_notifications(monkeypatch):
    """Regression: TG-уведомление должно уходить ПОСЛЕ commit'а batch UPDATE, не до.

    Если процесс упадёт между notify и execute(), строка останется в старом
    status и следующий cron-тик пришлёт дублирующее уведомление за ту же
    сделку. Порядок операций должен исключать это окно.
    """
    row = _row(outcome="WIN")

    async def _fake_fetch(sql, *args):
        return [row]

    call_order: list[str] = []

    async def _fake_execute(sql, *args):
        call_order.append("execute")

    async def _fake_notify(notes, **kw):
        call_order.append("notify")

    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(resolve_mod, "execute", _fake_execute)
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", _fake_notify)

    await resolve_mod._settle_filled()

    assert call_order == ["execute", "notify"], f"expected DB commit before notify, got {call_order}"


def test_settled_trades_get_per_order_subjects():
    """У каждой закрытой сделки свой subject.

    С общим subject вторая и третья закрытые сделки подавились бы cooldown'ом
    первой — владелец увидел бы одну сделку из трёх.
    """
    from app.jobs.v2_execution_resolve import settlement_notification

    a = settlement_notification({"id": "o-a", "outcome": "WIN", "stake": 10.0, "side": "YES"}, 5.0)
    b = settlement_notification({"id": "o-b", "outcome": "LOSS", "stake": 10.0, "side": "NO"}, -10.0)
    assert a.subject != b.subject
    assert a.subject == "order:o-a"


def test_settlement_is_fyi_not_action_required():
    """Закрытая сделка — отчёт о факте, а не запрос действия."""
    from app.analytics.notification import Severity
    from app.jobs.v2_execution_resolve import settlement_notification

    note = settlement_notification({"id": "o-1", "outcome": "WIN", "stake": 10.0, "side": "YES"}, 5.0)
    assert note.severity is Severity.FYI
    assert note.what_if_ignored is None


def test_expired_says_signal_timeout_not_market_resolution():
    """EXPIRED — это таймаут сигнала, а НЕ резолв рынка.

    Регрессия: перепутать эти два означает сказать владельцу, что рынок
    разрешился не в его пользу, хотя рынок вообще не разрешался.
    """
    from app.jobs.v2_execution_resolve import settlement_notification

    note = settlement_notification({"id": "o-1", "outcome": "EXPIRED", "stake": 10.0, "side": "YES"}, 0.0)
    blob = " ".join(note.what_happened).lower()
    assert "срок" in blob or "истёк" in blob
    assert "не резолв" in blob
