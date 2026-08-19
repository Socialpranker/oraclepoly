"""Тесты daily DQ-джобы (Phase 2 C0). БД замокана на уровне приватных шагов."""

import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from app.analytics.dq_audit import (
    VERDICT_AMBER,
    VERDICT_GREEN,
    VERDICT_RED,
    DqCheckResult,
    DqThresholds,
)
from app.jobs.dq_audit_daily import (
    _DEFAULT_PARAMS,
    _load_rule,
    _persist_claim_if_degraded,
    run_dq_audit_daily,
)
from app.services.notification_bus import PublishResult


def _result(check: str, verdict: str) -> DqCheckResult:
    return DqCheckResult(check=check, verdict=verdict, measured=1.0, amber_threshold=2.0, red_threshold=3.0, detail="x")


_GREEN_RESULTS = [_result("freshness:markets", VERDICT_GREEN), _result("freshness:signals", VERDICT_GREEN)]
_RED_RESULTS = [_result("freshness:markets", VERDICT_GREEN), _result("ri:orphans", VERDICT_RED)]


@pytest.mark.asyncio
async def test_load_rule_falls_back_to_defaults_when_missing():
    with patch("app.jobs.dq_audit_daily.fetchrow", new=AsyncMock(return_value=None)):
        version, thresholds = await _load_rule()
    assert version == 0
    assert thresholds == DqThresholds.from_params(_DEFAULT_PARAMS)


@pytest.mark.asyncio
async def test_load_rule_falls_back_to_defaults_when_table_missing():
    """policy_rules ещё не создана (миграция не применена/откачена) — джоба не
    падает целиком, а деградирует на defaults (whole-branch review Fix 4)."""
    err = asyncpg.UndefinedTableError('relation "policy_rules" does not exist')
    with patch("app.jobs.dq_audit_daily.fetchrow", new=AsyncMock(side_effect=err)):
        version, thresholds = await _load_rule()
    assert version == 0
    assert thresholds == DqThresholds.from_params(_DEFAULT_PARAMS)


@pytest.mark.asyncio
async def test_load_rule_parses_jsonb_string():
    row = {"version": 3, "params": json.dumps(_DEFAULT_PARAMS)}
    with patch("app.jobs.dq_audit_daily.fetchrow", new=AsyncMock(return_value=row)):
        version, thresholds = await _load_rule()
    assert version == 3
    assert thresholds.lookback_days == 7


@pytest.mark.asyncio
async def test_persist_claim_skipped_on_green():
    exec_mock = AsyncMock()
    with patch("app.jobs.dq_audit_daily.execute", new=exec_mock):
        await _persist_claim_if_degraded(VERDICT_GREEN, run_id=1)
    exec_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_claim_written_on_red():
    exec_mock = AsyncMock()
    with patch("app.jobs.dq_audit_daily.execute", new=exec_mock):
        await _persist_claim_if_degraded(VERDICT_RED, run_id=42)
    exec_mock.assert_awaited_once()
    args = exec_mock.await_args.args
    assert args[1] == "dq audit degraded"
    assert args[2] == "policy:dq_audit"
    assert args[3] == 42


@pytest.mark.asyncio
async def test_persist_claim_statement_stable_across_verdicts():
    """AMBER и RED должны писать ОДИНАКОВЫЙ statement — иначе ON CONFLICT(statement)
    даёт два независимых вечных claim'а вместо одного дедуплицированного
    (whole-branch review Fix 3). Сам вердикт живёт в agent_runs, не в тексте claim'а."""
    exec_amber = AsyncMock()
    with patch("app.jobs.dq_audit_daily.execute", new=exec_amber):
        await _persist_claim_if_degraded(VERDICT_AMBER, run_id=1)
    exec_red = AsyncMock()
    with patch("app.jobs.dq_audit_daily.execute", new=exec_red):
        await _persist_claim_if_degraded(VERDICT_RED, run_id=2)
    assert exec_amber.await_args.args[1] == exec_red.await_args.args[1] == "dq audit degraded"


def test_notification_lists_only_degraded_checks():
    from app.jobs.dq_audit_daily import to_notifications

    n = to_notifications(VERDICT_RED, VERDICT_GREEN, _RED_RESULTS)[0]
    body = " ".join(n.what_happened)

    assert "GREEN" in body and "RED" in body
    assert "осиротевшие записи" in body  # ri:orphans человеческим языком
    assert "свежесть рынков" not in body  # GREEN-строки не шумят
    assert any("ri:orphans" in e for e in n.evidence)  # техническое имя — в основание


def test_dq_recovery_uses_different_kind_than_degradation():
    """Возврат в GREEN обязан иметь свой kind, иначе cooldown шины (25ч) подавит
    «данные снова в порядке» после «данные испортились» — та же ловушка, что
    с recovery у proxy_health_check."""
    from app.jobs.dq_audit_daily import to_notifications

    degraded = to_notifications(VERDICT_RED, VERDICT_GREEN, _RED_RESULTS)[0]
    recovered = to_notifications(VERDICT_GREEN, VERDICT_RED, _GREEN_RESULTS)[0]

    assert degraded.kind != recovered.kind
    assert degraded.dedup_key() != recovered.dedup_key()


def test_dq_recovery_is_fyi_without_next_step():
    from app.analytics.notification import Severity
    from app.jobs.dq_audit_daily import to_notifications

    n = to_notifications(VERDICT_GREEN, VERDICT_RED, _GREEN_RESULTS)[0]

    assert n.severity == Severity.FYI
    assert n.what_to_do is None


def test_dq_never_invents_money_figure():
    """§3: цена плохих данных в долларах неизвестна — выдумывать запрещено."""
    from app.jobs.dq_audit_daily import to_notifications

    assert to_notifications(VERDICT_RED, VERDICT_GREEN, _RED_RESULTS)[0].what_if_ignored is None


def _patched_pipeline(results, previous, send_mock):
    """Общий стек патчей полного прогона run_dq_audit_daily."""
    freshness, rest = results[:2], results[2:]
    return (
        patch(
            "app.jobs.dq_audit_daily._load_rule",
            new=AsyncMock(return_value=(1, DqThresholds.from_params(_DEFAULT_PARAMS))),
        ),
        patch("app.jobs.dq_audit_daily._check_freshness", new=AsyncMock(return_value=freshness)),
        patch("app.jobs.dq_audit_daily._check_completeness", new=AsyncMock(return_value=[])),
        patch(
            "app.jobs.dq_audit_daily._check_expired_share",
            new=AsyncMock(return_value=rest[0] if rest else _result("expired_share", VERDICT_GREEN)),
        ),
        patch(
            "app.jobs.dq_audit_daily._check_referential_integrity",
            new=AsyncMock(return_value=rest[1] if len(rest) > 1 else _result("ri:orphans", VERDICT_GREEN)),
        ),
        patch("app.jobs.dq_audit_daily._previous_verdict", new=AsyncMock(return_value=previous)),
        patch("app.jobs.dq_audit_daily._persist_run", new=AsyncMock(return_value=7)),
        patch("app.jobs.dq_audit_daily._persist_claim_if_degraded", new=AsyncMock()),
        # send_mock теперь мокает шину, а не send_telegram_message: джоба отдаёт
        # структуру, а рассылку админам делает publish.
        patch("app.jobs.dq_audit_daily.publish", new=send_mock),
    )


async def _run_with_patches(results, previous, send_mock):
    patches = _patched_pipeline(results, previous=previous, send_mock=send_mock)
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return await run_dq_audit_daily()


@pytest.mark.asyncio
async def test_run_sends_tg_on_verdict_change():
    send_mock = AsyncMock(return_value=PublishResult(sent=1))
    result = await _run_with_patches(_GREEN_RESULTS, previous=VERDICT_AMBER, send_mock=send_mock)
    assert result["verdict"] == VERDICT_GREEN
    assert result["tg_sent"] is True
    send_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_silent_when_verdict_unchanged():
    send_mock = AsyncMock(return_value=PublishResult(sent=1))
    result = await _run_with_patches(_GREEN_RESULTS, previous=VERDICT_GREEN, send_mock=send_mock)
    assert result["tg_sent"] is False
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_first_ever_run_sends_tg():
    # previous=None ≠ verdict → шлём (первый запуск информативен)
    send_mock = AsyncMock(return_value=PublishResult(sent=1))
    result = await _run_with_patches(_GREEN_RESULTS, previous=None, send_mock=send_mock)
    assert result["tg_sent"] is True


@pytest.mark.asyncio
async def test_tg_sent_now_reflects_real_delivery():
    """Раньше tg_sent означал лишь «await не бросил исключение»: send_telegram_message
    возвращает None и на успехе, и на проглоченном HTTPError (известное ограничение,
    Fix 2 / acc68636). Через шину доставка идёт send_telegram_with_keyboard_rows
    с честным bool, поэтому провал отправки обязан давать tg_sent=False."""

    send_mock = AsyncMock(return_value=PublishResult(sent=0, failures=1))
    result = await _run_with_patches(_GREEN_RESULTS, previous=VERDICT_AMBER, send_mock=send_mock)

    assert result["tg_sent"] is False, "неудачная отправка не должна выглядеть как успешная"
    send_mock.assert_awaited_once()
