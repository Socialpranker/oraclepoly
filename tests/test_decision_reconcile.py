"""Tests для app.jobs.decision_reconcile (Phase 2 C4)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.decision_reconcile import run_decision_reconcile


def _due_row(action_id: int, metric: str, entity: str, baseline):
    return {
        "id": action_id,
        "effect": json.dumps(
            {"acknowledged": True, "expectation": {"metric": metric, "entity": entity, "baseline": baseline}}
        ),
    }


@pytest.mark.asyncio
async def test_empty_journal_is_valid():
    """Пустой журнал — основной сценарий до выхода из shadow, падать нельзя."""
    with patch("app.jobs.decision_reconcile.fetch", new=AsyncMock(return_value=[])):
        summary = await run_decision_reconcile()

    assert summary["status"] == "no_due"
    assert summary["n_due"] == 0
    assert summary["n_reconciled"] == 0


@pytest.mark.asyncio
async def test_numeric_improvement_is_classified_and_closed():
    rows = [_due_row(1, "roi", "D3_x", -0.33)]
    with (
        patch("app.jobs.decision_reconcile.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.decision_reconcile.fetchrow", new=AsyncMock(return_value={"roi": -0.10})),
        patch("app.jobs.decision_reconcile.execute", new=AsyncMock(return_value="UPDATE 1")) as ex,
    ):
        summary = await run_decision_reconcile()

    assert summary["n_reconciled"] == 1
    assert summary["outcomes"]["improved"] == 1
    result = json.loads(ex.await_args.args[2])
    assert result["outcome"] == "improved"
    assert result["actual"] == -0.10


@pytest.mark.asyncio
async def test_missing_metric_closes_as_not_applicable():
    """Детектор исчез → ожидание закрывается с причиной, а не висит вечно."""
    rows = [_due_row(1, "roi", "D_gone", -0.33)]
    with (
        patch("app.jobs.decision_reconcile.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.decision_reconcile.fetchrow", new=AsyncMock(return_value=None)),
        patch("app.jobs.decision_reconcile.execute", new=AsyncMock(return_value="UPDATE 1")) as ex,
    ):
        summary = await run_decision_reconcile()

    assert summary["outcomes"]["not_applicable"] == 1
    result = json.loads(ex.await_args.args[2])
    assert result["outcome"] == "not_applicable"
    assert result["reason"]


@pytest.mark.asyncio
async def test_verdict_metric_uses_dq_run():
    rows = [_due_row(1, "verdict", "dq_audit_daily", "RED")]
    with (
        patch("app.jobs.decision_reconcile.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.decision_reconcile.fetchrow", new=AsyncMock(return_value={"verdict": "GREEN"})),
        patch("app.jobs.decision_reconcile.execute", new=AsyncMock(return_value="UPDATE 1")) as ex,
    ):
        summary = await run_decision_reconcile()

    assert summary["outcomes"]["improved"] == 1
    assert json.loads(ex.await_args.args[2])["actual"] == "GREEN"


@pytest.mark.asyncio
async def test_broken_expectation_payload_closes_not_applicable():
    """Строка без expectation (или битый JSON) не роняет весь прогон."""
    rows = [{"id": 1, "effect": json.dumps({"acknowledged": True})}]
    with (
        patch("app.jobs.decision_reconcile.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.decision_reconcile.execute", new=AsyncMock(return_value="UPDATE 1")),
    ):
        summary = await run_decision_reconcile()

    assert summary["outcomes"]["not_applicable"] == 1


@pytest.mark.asyncio
async def test_expectation_missing_baseline_key_closes_not_applicable():
    """expectation с metric+entity, но БЕЗ ключа baseline → not_applicable, строка закрывается.

    Прямой exp["baseline"] на невалидированном payload кинул бы KeyError, пойманный
    внешним try/except БЕЗ reconciled_at — строка выбиралась бы каждым прогоном вечно.
    """
    rows = [
        {
            "id": 1,
            "effect": json.dumps({"acknowledged": True, "expectation": {"metric": "roi", "entity": "D3_x"}}),
        }
    ]
    with (
        patch("app.jobs.decision_reconcile.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.decision_reconcile.fetchrow", new=AsyncMock()) as fr,
        patch("app.jobs.decision_reconcile.execute", new=AsyncMock(return_value="UPDATE 1")) as ex,
    ):
        summary = await run_decision_reconcile()

    fr.assert_not_awaited()  # payload невалиден ДО измерения — _measure не вызывается
    assert summary["n_reconciled"] == 1
    assert summary["outcomes"]["not_applicable"] == 1
    result = json.loads(ex.await_args.args[2])
    assert result["outcome"] == "not_applicable"
    assert result["reason"]


@pytest.mark.asyncio
async def test_expectation_baseline_zero_is_legitimate_value():
    """baseline == 0.0 — легитимное значение (falsy, но присутствует), не невалидный payload."""
    rows = [_due_row(1, "roi", "D3_x", 0.0)]
    with (
        patch("app.jobs.decision_reconcile.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.decision_reconcile.fetchrow", new=AsyncMock(return_value={"roi": -0.10})),
        patch("app.jobs.decision_reconcile.execute", new=AsyncMock(return_value="UPDATE 1")) as ex,
    ):
        summary = await run_decision_reconcile()

    assert summary["n_reconciled"] == 1
    assert "not_applicable" not in summary["outcomes"]  # обработано как обычная сверка, не отброшено
    result = json.loads(ex.await_args.args[2])
    assert result["baseline"] == 0.0
    assert result["outcome"] == "worsened"  # actual -0.10 < baseline 0.0, вне band ±0.01


@pytest.mark.asyncio
async def test_execute_failure_on_one_row_does_not_abort_run():
    """execute() бросает исключение на первой строке — вторая всё равно закрывается."""
    rows = [_due_row(1, "roi", "D3_x", -0.33), _due_row(2, "roi", "D3_y", -0.33)]
    with (
        patch("app.jobs.decision_reconcile.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.decision_reconcile.fetchrow", new=AsyncMock(return_value={"roi": -0.10})),
        patch(
            "app.jobs.decision_reconcile.execute",
            new=AsyncMock(side_effect=[RuntimeError("transient db error"), "UPDATE 1"]),
        ),
    ):
        summary = await run_decision_reconcile()

    assert summary["n_due"] == 2
    assert summary["n_reconciled"] == 1
    assert summary["outcomes"]["improved"] == 1


@pytest.mark.asyncio
async def test_no_tg_is_sent():
    """Ежедневная сверка молчит — единственный выход наружу это quarterly-ретро."""
    rows = [_due_row(1, "roi", "D3_x", -0.33)]
    with (
        patch("app.jobs.decision_reconcile.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.decision_reconcile.fetchrow", new=AsyncMock(return_value={"roi": -0.10})),
        patch("app.jobs.decision_reconcile.execute", new=AsyncMock(return_value="UPDATE 1")),
    ):
        summary = await run_decision_reconcile()

    assert "tg_sent" not in summary
