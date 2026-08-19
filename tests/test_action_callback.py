"""Сквозной путь клика по кнопке «⚡ Сделать» (Phase 3): гейты → executor → receipt."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.analytics.action_gates import snapshot_hash
from app.api.telegram import CallbackQuery, TelegramChat, TelegramMessage, TelegramUser, _handle_action_callback

_SNAP = {"stage": "live", "live_stake_cap_usd": None}


def _cq(data: str = "act:pid123", user_id: int = 12345) -> CallbackQuery:
    return CallbackQuery(
        id=f"cq_{user_id}",
        **{"from": TelegramUser(id=user_id, is_bot=False, first_name="Admin")},
        message=TelegramMessage(message_id=5, chat=TelegramChat(id=999, type="private"), date=0),
        data=data,
    )


def _action_row(**kw) -> dict:
    base = {
        "kind": "pause_detector",
        "status": "proposed",
        "expires_at": datetime.now(UTC) + timedelta(hours=5),
        "payload_hash": snapshot_hash(_SNAP),
        "payload": json.dumps({"detector": "D3_cross_mkt_arb", "reason": "advisory L2"}),
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_happy_path_executes_and_marks_executed():
    exec_result = AsyncMock(
        return_value=type("R", (), {"ok": True, "effect": {"new_stage": "paused"}, "error": None})()
    )
    with (
        patch(
            "app.api.telegram.fetchrow", new=AsyncMock(side_effect=[_action_row(), {"verdict": "GREEN"}, {"id": 1}])
        ),  # 3-й вызов — резерв намерения
        patch("app.api.telegram._current_snapshot", new=AsyncMock(return_value=_SNAP)),
        patch("app.api.telegram.execute_action", new=exec_result),
        patch("app.api.telegram.execute", new=AsyncMock(return_value="UPDATE 1")) as upd,
        patch("app.api.telegram.answer_callback_query", new=AsyncMock()) as ack,
        patch("app.api.telegram._redraw_advisory_keyboard", new=AsyncMock()),
    ):
        res = await _handle_action_callback(_cq())

    assert res["result"] == "executed"
    exec_result.assert_awaited_once()
    assert "executed" in upd.await_args.args[0]  # статус проставлен
    assert "Сделано" in ack.await_args.args[1]


@pytest.mark.asyncio
async def test_expired_ttl_does_not_execute():
    """auto-approve запрещён: истёкшее предложение не исполняется никогда."""
    exec_mock = AsyncMock()
    with (
        patch(
            "app.api.telegram.fetchrow",
            new=AsyncMock(
                side_effect=[_action_row(expires_at=datetime.now(UTC) - timedelta(minutes=1)), {"verdict": "GREEN"}]
            ),
        ),
        patch("app.api.telegram._current_snapshot", new=AsyncMock(return_value=_SNAP)),
        patch("app.api.telegram.execute_action", new=exec_mock),
        patch("app.api.telegram.execute", new=AsyncMock(return_value="UPDATE 1")),
        patch("app.api.telegram.answer_callback_query", new=AsyncMock()) as ack,
    ):
        res = await _handle_action_callback(_cq())

    assert res["result"] == "expired"
    exec_mock.assert_not_awaited()
    assert "просрочено" in ack.await_args.args[1]


@pytest.mark.asyncio
async def test_snapshot_change_does_not_execute():
    exec_mock = AsyncMock()
    with (
        patch(
            "app.api.telegram.fetchrow", new=AsyncMock(side_effect=[_action_row(), {"verdict": "GREEN"}, {"id": 1}])
        ),  # 3-й вызов — резерв намерения
        patch(
            "app.api.telegram._current_snapshot",
            new=AsyncMock(return_value={"stage": "paused", "live_stake_cap_usd": None}),
        ),
        patch("app.api.telegram.execute_action", new=exec_mock),
        patch("app.api.telegram.execute", new=AsyncMock(return_value="UPDATE 1")),
        patch("app.api.telegram.answer_callback_query", new=AsyncMock()) as ack,
    ):
        res = await _handle_action_callback(_cq())

    assert res["result"] == "snapshot_changed"
    exec_mock.assert_not_awaited()
    assert "изменились" in ack.await_args.args[1]


@pytest.mark.asyncio
async def test_dq_red_does_not_execute():
    exec_mock = AsyncMock()
    with (
        patch("app.api.telegram.fetchrow", new=AsyncMock(side_effect=[_action_row(), {"verdict": "RED"}])),
        patch("app.api.telegram._current_snapshot", new=AsyncMock(return_value=_SNAP)),
        patch("app.api.telegram.execute_action", new=exec_mock),
        patch("app.api.telegram.execute", new=AsyncMock(return_value="UPDATE 1")),
        patch("app.api.telegram.answer_callback_query", new=AsyncMock()) as ack,
    ):
        res = await _handle_action_callback(_cq())

    assert res["result"] == "dq_red"
    exec_mock.assert_not_awaited()
    assert "ненадёжны" in ack.await_args.args[1]


@pytest.mark.asyncio
async def test_second_click_does_not_execute_twice():
    """Идемпотентность: строка уже 'executed' — адаптер не зовётся."""
    exec_mock = AsyncMock()
    with (
        patch(
            "app.api.telegram.fetchrow",
            new=AsyncMock(side_effect=[_action_row(status="executed"), {"verdict": "GREEN"}]),
        ),
        patch("app.api.telegram._current_snapshot", new=AsyncMock(return_value=_SNAP)),
        patch("app.api.telegram.execute_action", new=exec_mock),
        patch("app.api.telegram.execute", new=AsyncMock(return_value="UPDATE 1")),
        patch("app.api.telegram.answer_callback_query", new=AsyncMock()) as ack,
    ):
        res = await _handle_action_callback(_cq())

    assert res["result"] == "already_handled"
    exec_mock.assert_not_awaited()
    assert "Уже сделано" in ack.await_args.args[1]


@pytest.mark.asyncio
async def test_adapter_failure_marks_failed_not_executed():
    """Падение адаптера → status='failed' и внятный toast, а не тишина (спека §5)."""
    failed = type("R", (), {"ok": False, "effect": {}, "error": "already_exiting"})()
    with (
        patch(
            "app.api.telegram.fetchrow", new=AsyncMock(side_effect=[_action_row(), {"verdict": "GREEN"}, {"id": 1}])
        ),  # 3-й вызов — резерв намерения
        patch("app.api.telegram._current_snapshot", new=AsyncMock(return_value=_SNAP)),
        patch("app.api.telegram.execute_action", new=AsyncMock(return_value=failed)),
        patch("app.api.telegram.execute", new=AsyncMock(return_value="UPDATE 1")) as upd,
        patch("app.api.telegram.answer_callback_query", new=AsyncMock()) as ack,
    ):
        res = await _handle_action_callback(_cq())

    assert res["result"] == "failed"
    assert "failed" in upd.await_args.args[0]
    assert "already_exiting" in ack.await_args.args[1]


@pytest.mark.asyncio
async def test_missing_action_row_is_reported():
    with (
        patch("app.api.telegram.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.api.telegram.answer_callback_query", new=AsyncMock()) as ack,
    ):
        res = await _handle_action_callback(_cq())

    assert res["result"] == "not_found"
    assert "не найден" in ack.await_args.args[1]


@pytest.mark.asyncio
async def test_bad_callback_format_is_reported():
    with patch("app.api.telegram.answer_callback_query", new=AsyncMock()) as ack:
        res = await _handle_action_callback(_cq(data="act:"))

    assert res["result"] == "bad_format"
    assert ack.await_args.args[1].startswith("❌")


@pytest.mark.asyncio
async def test_second_concurrent_click_loses_reserve_and_does_not_execute():
    """Гонка двух кликов (спека §8 риск 5, находка ревью Task 4).

    Гейты читают СНИМОК строки, поэтому оба одновременных клика проходят их.
    Единственная защита — атомарный захват перед побочным эффектом: проигравший
    получает пустой RETURNING и не вызывает executor.
    """
    exec_mock = AsyncMock()
    with (
        patch(
            # 3-й вызов fetchrow — резерв намерения; None = строку забрал другой клик.
            "app.api.telegram.fetchrow",
            new=AsyncMock(side_effect=[_action_row(), {"verdict": "GREEN"}, None]),
        ),
        patch("app.api.telegram._current_snapshot", new=AsyncMock(return_value=_SNAP)),
        patch("app.api.telegram.execute_action", new=exec_mock),
        patch("app.api.telegram.execute", new=AsyncMock(return_value="UPDATE 1")),
        patch("app.api.telegram.answer_callback_query", new=AsyncMock()) as ack,
    ):
        res = await _handle_action_callback(_cq())

    assert res["result"] == "already_handled"
    exec_mock.assert_not_awaited(), "проигравший гонку клик НЕ должен исполнять действие"
    assert "Уже сделано" in ack.await_args.args[1]


def test_reserve_sql_guards_on_proposed_status():
    """Резерв обязан быть УСЛОВНЫМ на status='proposed' (спека §8 риск 5).

    Мок fetchrow не проверяет текст SQL, поэтому предикат стережётся отдельно:
    без него `UPDATE ... RETURNING` вернул бы строку любому клику, и защита от
    гонки исчезла бы молча — ровно так дефект и был найден на ревью Task 4.
    """
    from app.api.telegram import _CLAIM_L2_ACTION_SQL, _FAIL_L2_ACTION_SQL, _RESERVE_L2_ACTION_SQL

    assert "status = 'proposed'" in _RESERVE_L2_ACTION_SQL, "резерв без условия не защищает от гонки"
    assert "RETURNING" in _RESERVE_L2_ACTION_SQL, "без RETURNING проигравший клик неотличим от победителя"
    # Финализация работает только по строке, которую держим мы.
    for sql in (_CLAIM_L2_ACTION_SQL, _FAIL_L2_ACTION_SQL):
        assert "status = 'approved'" in sql, "финализация должна требовать захваченный статус"


@pytest.mark.asyncio
async def test_legacy_close_pos_is_audited():
    """close_pos: теперь оставляет след в agent_actions (спека §2 п.5)."""
    from app.api.telegram import _log_l2_legacy_action

    with patch("app.api.telegram.execute", new=AsyncMock(return_value="INSERT 0 1")) as ex:
        await _log_l2_legacy_action("close_position", {"order_db_id": "ord-9"}, 12345, {"clob": "x1"}, ok=True)

    sql = ex.await_args.args[0]
    assert "'L2'" in sql
    assert ex.await_args.args[1] == "close_position"


@pytest.mark.asyncio
async def test_legacy_audit_marks_failure():
    from app.api.telegram import _log_l2_legacy_action

    with patch("app.api.telegram.execute", new=AsyncMock(return_value="INSERT 0 1")) as ex:
        await _log_l2_legacy_action("close_position", {"order_db_id": "o"}, 1, {"error": "already_exiting"}, ok=False)

    assert "failed" in ex.await_args.args[0]


@pytest.mark.asyncio
async def test_missing_dq_run_is_fail_closed():
    """Нет DQ-run → вердикт RED → действие НЕ исполняется (спека §4.4).

    Дыра в покрытии, найденная финальным security review: подмена fail-closed
    дефолта на "GREEN" не роняла ни одного теста. Сценарий реальный — первый
    деплой до утреннего прогона dq_audit_daily, или переименование джобы.
    """
    exec_mock = AsyncMock()
    with (
        # 2-й вызов fetchrow — DQ-run; None = джоба ещё не отрабатывала.
        patch("app.api.telegram.fetchrow", new=AsyncMock(side_effect=[_action_row(), None])),
        patch("app.api.telegram._current_snapshot", new=AsyncMock(return_value=_SNAP)),
        patch("app.api.telegram.execute_action", new=exec_mock),
        patch("app.api.telegram.execute", new=AsyncMock(return_value="UPDATE 1")),
        patch("app.api.telegram.answer_callback_query", new=AsyncMock()) as ack,
    ):
        res = await _handle_action_callback(_cq())

    assert res["result"] == "dq_red", "отсутствие DQ-run обязано трактоваться как RED"
    exec_mock.assert_not_awaited(), "без данных о качестве действие исполнять нельзя"
    assert "ненадёжны" in ack.await_args.args[1]
