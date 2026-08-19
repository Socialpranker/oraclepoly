"""V2 Execution — event-level re-entry guard tests.

Контекст (2026-06-04, см. memory standalone_virtual_pnl_inflation_2026-06-04):
  Virtual Daily Summary показывал +$10.9K/день = артефакт. STANDALONE_V2_LGB
  заходит в один event 10-32 раза за 16ч: TP=+30% срабатывает за ~12 мин →
  позиция settled → predictor (cron 30 мин) генерит новый сигнал → вход снова.
  Артефакт двухслойный: повтор внутри market_id И между рынками одного neg-risk
  event (predictor скорит каждый market независимо, не зная о корреляции).

Решение (вариант A, held-to-resolution): один вход на event пока событие
активно. Блокируем повторный сигнал, если на event уже есть ЛЮБАЯ virtual-
позиция (открытая или закрытая через TP/SL/резолюцию). Симуляция на истории:
raw +$45,045 → +$663 (444 → 35 позиций).

Контракт _event_reentry_blocked(detector, event_id):
  - event_id None/empty → False (нет события — нечего гейтить, не падаем).
  - COUNT существующих virtual-позиций (placed/filled/settled) на
    (detector, event_id) > 0 → True (blocked).
  - COUNT == 0 → False (первый вход на событие).
  - Guard per-detector: чужой детектор на том же event не блокирует.

Эталонный паттерн БД-мока — test_v2_execution_virtual_cap.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs import v2_execution


@pytest.mark.asyncio
async def test_blocked_when_existing_position_on_event():
    """На event уже есть virtual-позиция (COUNT=1) → blocked."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=1)):
        blocked = await v2_execution._event_reentry_blocked("STANDALONE_V2_LGB", "411268")
    assert blocked is True


@pytest.mark.asyncio
async def test_not_blocked_when_no_position_on_event():
    """Первый вход на событие (COUNT=0) → не blocked."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=0)):
        blocked = await v2_execution._event_reentry_blocked("STANDALONE_V2_LGB", "411268")
    assert blocked is False


@pytest.mark.asyncio
async def test_blocked_counts_settled_positions():
    """Вариант A: даже закрытая (settled через TP) позиция блокирует re-entry.

    COUNT включает virtual_settled, поэтому повтор после TP-выхода → blocked
    пока событие активно. Это ядро фикса против скальпа одного события.
    """
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=10)):
        blocked = await v2_execution._event_reentry_blocked("STANDALONE_V2_LGB", "411268")
    assert blocked is True


@pytest.mark.asyncio
async def test_none_event_id_not_blocked():
    """event_id=None (сигнал без события) → не гейтим, не падаем, не вызываем БД."""
    mock = AsyncMock(return_value=99)
    with patch.object(v2_execution, "fetchval", new=mock):
        blocked = await v2_execution._event_reentry_blocked("STANDALONE_V2_LGB", None)
    assert blocked is False
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_empty_event_id_not_blocked():
    """event_id='' (transient/missing) → не гейтим, БД не трогаем."""
    mock = AsyncMock(return_value=99)
    with patch.object(v2_execution, "fetchval", new=mock):
        blocked = await v2_execution._event_reentry_blocked("STANDALONE_V2_LGB", "")
    assert blocked is False
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_handles_none_count_from_db():
    """fetchval вернул None (нет строк) → трактуем как 0 → не blocked."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=None)):
        blocked = await v2_execution._event_reentry_blocked("STANDALONE_V2_LGB", "411268")
    assert blocked is False


@pytest.mark.asyncio
async def test_guard_is_per_detector():
    """Guard передаёт detector в запрос — проверяем что detector в аргументах SQL.

    Это гарантирует, что открытая позиция D3 на event НЕ заблокирует STANDALONE
    на том же event (разные детекторы — независимые торговые единицы).
    """
    mock = AsyncMock(return_value=0)
    with patch.object(v2_execution, "fetchval", new=mock):
        await v2_execution._event_reentry_blocked("STANDALONE_V2_LGB", "411268")
    # fetchval(sql, detector, event_id) — detector и event_id среди позиционных args
    call_args = mock.call_args.args
    assert "STANDALONE_V2_LGB" in call_args
    assert "411268" in call_args


def test_event_reentry_virtual_sql_filters_virtual_statuses():
    """Virtual SQL считает только virtual-позиции placed/filled/settled,
    НЕ cancelled (несостоявшийся вход не должен блокировать событие)."""
    sql = v2_execution._EVENT_REENTRY_VIRTUAL_SQL
    assert "execution_mode = 'virtual'" in sql
    assert "virtual_placed" in sql
    assert "virtual_filled" in sql
    assert "virtual_settled" in sql
    assert "virtual_cancelled" not in sql


# ─── Live-aware guard (fix 2026-06-04) ───────────────────────────────────────


def test_event_reentry_live_sql_filters_live_modes_and_statuses():
    """Live SQL считает maker/taker позиции в статусах intent/placed/filled/settled,
    НЕ rejected/cancelled. Это гейтит live re-entry (раньше guard был virtual-only)."""
    sql = v2_execution._EVENT_REENTRY_LIVE_SQL
    assert "maker" in sql
    assert "taker" in sql
    assert "intent" in sql
    assert "'placed'" in sql
    assert "'filled'" in sql
    assert "'settled'" in sql
    # Live SQL НЕ должен смотреть на virtual-позиции (разные арены).
    assert "execution_mode = 'virtual'" not in sql
    assert "rejected" not in sql


@pytest.mark.asyncio
async def test_live_signal_uses_live_sql():
    """live_mode=True → используется _EVENT_REENTRY_LIVE_SQL (maker/taker), НЕ virtual."""
    mock = AsyncMock(return_value=0)
    with patch.object(v2_execution, "fetchval", new=mock):
        await v2_execution._event_reentry_blocked("STANDALONE_V2_LGB", "411268", live_mode=True)
    sql_arg = mock.call_args.args[0]
    assert sql_arg == v2_execution._EVENT_REENTRY_LIVE_SQL
    assert "maker" in sql_arg


@pytest.mark.asyncio
async def test_virtual_signal_uses_virtual_sql_by_default():
    """live_mode опущен (default False) → используется virtual SQL. Обратная совместимость."""
    mock = AsyncMock(return_value=0)
    with patch.object(v2_execution, "fetchval", new=mock):
        await v2_execution._event_reentry_blocked("STANDALONE_V2_LGB", "411268")
    sql_arg = mock.call_args.args[0]
    assert sql_arg == v2_execution._EVENT_REENTRY_VIRTUAL_SQL


@pytest.mark.asyncio
async def test_live_signal_blocked_when_prior_live_position():
    """live_mode=True + COUNT>0 (есть прошлая live-позиция на event) → blocked.

    Это ядро live-fix: STANDALONE в live не повторит скальп одного event реальными
    деньгами (раньше live re-entry не гейтился — execution_mode='virtual' хардкод).
    """
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=1)):
        blocked = await v2_execution._event_reentry_blocked("STANDALONE_V2_LGB", "411268", live_mode=True)
    assert blocked is True


@pytest.mark.asyncio
async def test_live_none_event_id_not_blocked():
    """live_mode=True + event_id None → не гейтим, БД не трогаем."""
    mock = AsyncMock(return_value=99)
    with patch.object(v2_execution, "fetchval", new=mock):
        blocked = await v2_execution._event_reentry_blocked("STANDALONE_V2_LGB", None, live_mode=True)
    assert blocked is False
    mock.assert_not_called()
