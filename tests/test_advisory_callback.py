"""Tests для callback-обработчика adv: (Phase 2 C3b)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.api.telegram import CallbackQuery, TelegramChat, TelegramMessage, TelegramUser, _handle_advisory_callback


def _cq(data: str, user_id: int = 12345) -> CallbackQuery:
    return CallbackQuery(
        id=f"cq_{user_id}",
        **{"from": TelegramUser(id=user_id, is_bot=False, first_name="Admin")},
        data=data,
    )


def _cq_with_message(data: str, user_id: int = 12345) -> CallbackQuery:
    """CallbackQuery с валидным message (нужен для reply chat_id в review)."""
    return CallbackQuery(
        id=f"cq_{user_id}",
        **{"from": TelegramUser(id=user_id, is_bot=False, first_name="Admin")},
        message=TelegramMessage(message_id=5, chat=TelegramChat(id=999, type="private"), date=0),
        data=data,
    )


@pytest.mark.asyncio
async def test_advisory_ack_logs_action():
    cq = _cq("adv:ack:abc123def4567890")
    proposal_row = {"verdict": "REDUCE_RISK_REVIEW", "name": "policy_engine:detector:D3", "metrics": {}}
    # side_effect: первый fetchrow — поиск proposal, второй — baseline ROI (_build_ack_expectation).
    # Один и тот же return_value на оба вызова маскировал бы KeyError на "roi" (C4 регрессия).
    fetchrow_mock = AsyncMock(side_effect=[proposal_row, {"roi": -0.33}])
    execute_mock = AsyncMock(return_value="INSERT 0 1")
    with (
        patch("app.api.telegram.fetchrow", fetchrow_mock),
        patch("app.api.telegram.execute", execute_mock),
        patch("app.api.telegram.answer_callback_query", AsyncMock()) as ack,
        patch("app.api.telegram._redraw_advisory_keyboard", AsyncMock()),
    ):
        result = await _handle_advisory_callback(cq)
    assert result == {"result": "ok", "action": "ack", "pid": "abc123def4567890"}
    execute_mock.assert_awaited_once()  # запись в agent_actions
    assert "Принято" in ack.call_args.args[1]
    args = execute_mock.await_args.args
    effect = json.loads(args[6])
    assert "expectation" in effect
    assert effect["expectation"]["baseline"] == -0.33


@pytest.mark.asyncio
async def test_advisory_bad_format():
    with patch("app.api.telegram.answer_callback_query", AsyncMock()):
        result = await _handle_advisory_callback(_cq("adv:ack"))  # нет pid
    assert result["result"] == "bad_format"


@pytest.mark.asyncio
async def test_advisory_proposal_not_found():
    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value=None)),
        patch("app.api.telegram.answer_callback_query", AsyncMock()),
    ):
        result = await _handle_advisory_callback(_cq("adv:ack:deadbeef12345678"))
    assert result["result"] == "not_found"


@pytest.mark.asyncio
async def test_advisory_duplicate_click():
    cq = _cq("adv:ack:abc123def4567890")
    with (
        patch("app.api.telegram.fetchrow", AsyncMock(return_value={"verdict": "REDUCE_RISK_REVIEW", "name": "policy_engine:detector:D3", "metrics": {}})),
        patch("app.api.telegram.execute", AsyncMock(return_value="INSERT 0 0")),  # ON CONFLICT
        patch("app.api.telegram.answer_callback_query", AsyncMock()) as ack,
    ):
        result = await _handle_advisory_callback(cq)
    assert result["result"] == "already_handled"
    assert "Уже обработано" in ack.call_args.args[1]


@pytest.mark.asyncio
async def test_advisory_investigate_writes_claim():
    cq = _cq("adv:investigate:abc123def4567890")
    calls = []

    async def fake_execute(sql, *a):
        calls.append((sql, a))
        return "INSERT 0 1"

    with (
        patch(
            "app.api.telegram.fetchrow",
            AsyncMock(return_value={"verdict": "REDUCE_RISK_REVIEW", "name": "policy_engine:detector:D3", "metrics": {}}),
        ),
        patch("app.api.telegram.execute", fake_execute),
        patch("app.api.telegram.answer_callback_query", AsyncMock()) as ack,
        patch("app.api.telegram._redraw_advisory_keyboard", AsyncMock()),
    ):
        result = await _handle_advisory_callback(cq)
    assert result["result"] == "ok"
    # два execute: claim (agent_claims) + журнал (agent_actions)
    claim_calls = [(s, a) for s, a in calls if "agent_claims" in s]
    assert len(claim_calls) == 1
    assert "human requested investigate" in str(claim_calls[0][1])
    assert "Отмечено" in ack.call_args.args[1]


@pytest.mark.asyncio
async def test_advisory_review_calls_explainer():
    cq = _cq_with_message("adv:review:abc123def4567890")
    sent_msgs = []

    async def fake_send_msg(chat_id, text, **kw):
        sent_msgs.append(text)

    with (
        patch(
            "app.api.telegram.fetchrow",
            AsyncMock(
                return_value={"verdict": "REDUCE_RISK_REVIEW", "name": "policy_engine:detector:D3", "metrics": {"reason": "roi -20%"}}
            ),
        ),
        patch("app.api.telegram.execute", AsyncMock(return_value="INSERT 0 1")),
        patch("app.core.advisory_explainer.explain_advisory", AsyncMock(return_value="Разбор: снизить риск по D3.")),
        patch("app.api.telegram.send_telegram_message", fake_send_msg),
        patch("app.api.telegram.answer_callback_query", AsyncMock()) as ack,
        patch("app.api.telegram._redraw_advisory_keyboard", AsyncMock()),
    ):
        result = await _handle_advisory_callback(cq)
    assert result["result"] == "ok"
    assert any("Разбор" in m for m in sent_msgs)
    assert "отправлен" in ack.call_args.args[1].lower()


@pytest.mark.asyncio
async def test_advisory_review_explainer_unavailable():
    cq = _cq_with_message("adv:review:abc123def4567890")
    with (
        patch(
            "app.api.telegram.fetchrow",
            AsyncMock(return_value={"verdict": "REDUCE_RISK_REVIEW", "name": "policy_engine:detector:D3", "metrics": {}}),
        ),
        patch("app.api.telegram.execute", AsyncMock(return_value="INSERT 0 1")),
        patch("app.core.advisory_explainer.explain_advisory", AsyncMock(return_value=None)),  # нет ключа
        patch("app.api.telegram.answer_callback_query", AsyncMock()) as ack,
        patch("app.api.telegram._redraw_advisory_keyboard", AsyncMock()),
    ):
        result = await _handle_advisory_callback(cq)
    assert result["result"] == "ok"  # журнал всё равно записан
    assert "недоступен" in ack.call_args.args[1].lower()


# ── Phase 2 C4: ожидание при ACK ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ack_writes_expectation_with_due_date():
    """ACK по detector-scope измеряет baseline и пишет ожидание + due_date."""
    from app.api.telegram import _handle_advisory_callback

    cq = _cq_with_message("adv:ack:abc123")
    proposal_row = {"verdict": "CLOSE_POSITION_REVIEW", "name": "policy_engine:detector:D3_x", "metrics": "{}"}

    with (
        patch("app.api.telegram.fetchrow", new=AsyncMock(side_effect=[proposal_row, {"roi": -0.33}])),
        patch("app.api.telegram.execute", new=AsyncMock(return_value="INSERT 0 1")) as ex,
        patch("app.api.telegram.answer_callback_query", new=AsyncMock()),
        patch("app.api.telegram.edit_message_reply_markup", new=AsyncMock()),
    ):
        result = await _handle_advisory_callback(cq)

    assert result["result"] == "ok"
    args = ex.await_args.args
    # args: (SQL, kind, actor, payload_json, payload_hash, idem_key, effect_json, due_date)
    effect = json.loads(args[6])
    assert effect["acknowledged"] is True
    assert effect["expectation"]["metric"] == "roi"
    assert effect["expectation"]["entity"] == "D3_x"
    assert effect["expectation"]["baseline"] == -0.33
    due_date = args[7]
    assert due_date is not None and due_date.tzinfo is not None


@pytest.mark.asyncio
async def test_ack_without_metric_source_still_logs_click():
    """policy:ips — источника метрики нет: ожидание не создаётся, клик записан."""
    from app.api.telegram import _handle_advisory_callback

    cq = _cq_with_message("adv:ack:abc123")
    proposal_row = {"verdict": "INVESTIGATE_DATA", "name": "policy_engine:policy:ips", "metrics": "{}"}

    with (
        patch("app.api.telegram.fetchrow", new=AsyncMock(return_value=proposal_row)),
        patch("app.api.telegram.execute", new=AsyncMock(return_value="INSERT 0 1")) as ex,
        patch("app.api.telegram.answer_callback_query", new=AsyncMock()),
        patch("app.api.telegram.edit_message_reply_markup", new=AsyncMock()),
    ):
        result = await _handle_advisory_callback(cq)

    assert result["result"] == "ok"
    args = ex.await_args.args
    effect = json.loads(args[6])
    assert effect["acknowledged"] is True
    assert "expectation" not in effect
    assert effect["no_expectation_reason"]
    assert args[7] is None


@pytest.mark.asyncio
async def test_ack_survives_baseline_measurement_failure():
    """SQL baseline упал → клик всё равно записан (журнал решения важнее телеметрии)."""
    from app.api.telegram import _handle_advisory_callback

    cq = _cq_with_message("adv:ack:abc123")
    proposal_row = {"verdict": "CLOSE_POSITION_REVIEW", "name": "policy_engine:detector:D3_x", "metrics": "{}"}

    with (
        patch("app.api.telegram.fetchrow", new=AsyncMock(side_effect=[proposal_row, RuntimeError("db down")])),
        patch("app.api.telegram.execute", new=AsyncMock(return_value="INSERT 0 1")) as ex,
        patch("app.api.telegram.answer_callback_query", new=AsyncMock()),
        patch("app.api.telegram.edit_message_reply_markup", new=AsyncMock()),
    ):
        result = await _handle_advisory_callback(cq)

    assert result["result"] == "ok"
    effect = json.loads(ex.await_args.args[6])
    assert effect["acknowledged"] is True
    assert "expectation" not in effect
