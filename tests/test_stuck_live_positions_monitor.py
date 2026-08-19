"""Tests for app.jobs.stuck_live_positions_monitor — алерт на зависшие live-позиции.

Read-only мониторинг: не трогает торговую логику, только видимость.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.jobs import stuck_live_positions_monitor as mod


def _stuck_row(order_id: str = "order-1", hours_ago: float = 30.0, hours_since_end: float = 5.0):
    return {
        "id": order_id,
        "detector": "D3_cross_mkt_arb",
        "market_id": "0xmarket1",
        "side": "YES",
        "stake": 42.5,
        "filled_at": datetime.now(UTC) - timedelta(hours=hours_ago),
        "end_date": datetime.now(UTC) - timedelta(hours=hours_since_end),
    }


@pytest.mark.asyncio
async def test_no_stuck_positions_returns_empty_summary(monkeypatch):
    """Нет старых открытых live-позиций → summary пуст, ничего не шлём."""
    mock_notify = AsyncMock()
    monkeypatch.setattr(mod, "fetch", AsyncMock(return_value=[]))
    monkeypatch.setattr(mod, "publish", mock_notify)

    result = await mod.run_stuck_live_positions_monitor()

    assert result["stuck_count"] == 0
    assert result["alerted"] == []
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_stuck_position_triggers_alert(monkeypatch):
    """Позиция старше threshold → TG-алерт с order_id/detector/market_id/возрастом."""
    row = _stuck_row(order_id="order-stuck", hours_ago=30.0)
    mock_notify = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.exists = AsyncMock(return_value=False)

    monkeypatch.setattr(mod, "fetch", AsyncMock(return_value=[row]))
    monkeypatch.setattr("app.core.cache.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(mod, "publish", mock_notify)

    result = await mod.run_stuck_live_positions_monitor()

    assert result["stuck_count"] == 1
    assert result["alerted"] == ["order-stuck"]
    mock_notify.assert_called_once()
    notes = mock_notify.call_args[0][0]
    assert len(notes) == 1
    note = notes[0]
    assert note.subject == "order:order-stuck"
    assert note.kind == "stuck_live_position"
    blob = " ".join([*note.what_happened, *note.evidence])
    assert "D3_cross_mkt_arb" in blob
    assert "0xmarket1" in blob
    mock_redis.set.assert_called_once()
    assert mock_redis.set.call_args[0][0] == "stuck_live_position_alert:order-stuck"


@pytest.mark.asyncio
async def test_stuck_position_dedup_skips_repeat_alert(monkeypatch):
    """Уже алертили эту позицию (dedup key в Redis) → не шлём повторно в TTL-окне."""
    row = _stuck_row(order_id="order-dup", hours_ago=48.0)
    mock_notify = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.exists = AsyncMock(return_value=True)

    monkeypatch.setattr(mod, "fetch", AsyncMock(return_value=[row]))
    monkeypatch.setattr("app.core.cache.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(mod, "publish", mock_notify)

    result = await mod.run_stuck_live_positions_monitor()

    assert result["stuck_count"] == 1
    assert result["alerted"] == []
    mock_notify.assert_not_called()
    mock_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_redis_down_still_alerts(monkeypatch):
    """Redis недоступен → дедуп пропускается, но алерт всё равно шлётся (conservative)."""
    row = _stuck_row(order_id="order-noredis", hours_ago=25.0)
    mock_notify = AsyncMock()

    monkeypatch.setattr(mod, "fetch", AsyncMock(return_value=[row]))
    monkeypatch.setattr("app.core.cache.get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "publish", mock_notify)

    result = await mod.run_stuck_live_positions_monitor()

    assert result["alerted"] == ["order-noredis"]
    mock_notify.assert_called_once()


@pytest.mark.asyncio
async def test_query_uses_grace_hours_param(monkeypatch):
    """fetch вызывается с STUCK_GRACE_HOURS (2.0) как параметром."""
    captured_args = []

    async def mock_fetch(_sql, *args):
        captured_args.append(args)
        return []

    monkeypatch.setattr(mod, "fetch", mock_fetch)

    await mod.run_stuck_live_positions_monitor()

    assert captured_args == [(mod.STUCK_GRACE_HOURS,)]


@pytest.mark.asyncio
async def test_long_horizon_position_not_stuck_while_market_open(monkeypatch):
    """D3 держит позиции неделями по design (напр. UAL Q2 revenue) — большой возраст
    ПОЗИЦИИ не должен алертить, пока сам рынок ещё не закрылся. Фикс 2026-07-07:
    гейт по m.end_date в SQL уже это обеспечивает — этот тест фиксирует форматирование
    сообщения для случая, когда позиция старая, а рынок закрылся недавно (не наоборот)."""
    row = _stuck_row(order_id="order-longheld", hours_ago=218.6, hours_since_end=3.0)
    mock_notify = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.exists = AsyncMock(return_value=False)

    monkeypatch.setattr(mod, "fetch", AsyncMock(return_value=[row]))
    monkeypatch.setattr("app.core.cache.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(mod, "publish", mock_notify)

    await mod.run_stuck_live_positions_monitor()

    note = mock_notify.call_args[0][0][0]
    blob = " ".join(note.what_happened)
    assert "3.0ч назад" in blob
    assert "218.6ч" in blob


@pytest.mark.asyncio
async def test_null_timestamps_do_not_crash_the_monitor(monkeypatch):
    """filled_at/end_date = NULL → алерт уходит со словом «неизвестно», а не падает.

    SQL ненулевые даты не гарантирует. Раньше None доезжал до f-строки с `:.1f`
    и ронял джобу — то есть монитор молчал ровно в том случае, ради которого
    он написан.
    """
    row = _stuck_row(order_id="order-nulls")
    row["filled_at"] = None
    row["end_date"] = None
    mock_notify = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.exists = AsyncMock(return_value=False)

    monkeypatch.setattr(mod, "fetch", AsyncMock(return_value=[row]))
    monkeypatch.setattr("app.core.cache.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(mod, "publish", mock_notify)

    result = await mod.run_stuck_live_positions_monitor()

    assert result["alerted"] == ["order-nulls"]
    blob = " ".join(mock_notify.call_args[0][0][0].what_happened)
    assert "неизвестно" in blob


@pytest.mark.asyncio
async def test_dedup_key_is_set_only_after_successful_publish(monkeypatch):
    """Публикация упала → ключ дедупа НЕ ставится, следующий прогон попробует снова.

    Раньше ключ выставлялся до отправки: упавшая отправка означала молчание про
    зависшие деньги на 24ч. Лишний повтор дешевле потерянного алерта.
    """
    row = _stuck_row(order_id="order-boom")
    mock_redis = AsyncMock()
    mock_redis.exists = AsyncMock(return_value=False)

    monkeypatch.setattr(mod, "fetch", AsyncMock(return_value=[row]))
    monkeypatch.setattr("app.core.cache.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(mod, "publish", AsyncMock(side_effect=RuntimeError("TG down")))

    with pytest.raises(RuntimeError):
        await mod.run_stuck_live_positions_monitor()

    mock_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_each_position_gets_its_own_subject(monkeypatch):
    """Две зависшие позиции → два уведомления с разными subject.

    Один общий subject означал бы, что вторая позиция подавится cooldown'ом
    первой — то есть про вторые замороженные деньги владелец не узнает.
    """
    rows = [_stuck_row(order_id="order-a"), _stuck_row(order_id="order-b")]
    mock_notify = AsyncMock()
    mock_redis = AsyncMock()
    mock_redis.exists = AsyncMock(return_value=False)

    monkeypatch.setattr(mod, "fetch", AsyncMock(return_value=rows))
    monkeypatch.setattr("app.core.cache.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(mod, "publish", mock_notify)

    await mod.run_stuck_live_positions_monitor()

    subjects = {n.subject for n in mock_notify.call_args[0][0]}
    assert subjects == {"order:order-a", "order:order-b"}
