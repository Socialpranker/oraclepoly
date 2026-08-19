"""Tests для app.jobs.agent_owners_report."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.agent_owners_report import _build_report, _markdown_to_telegram_html, run_agent_owners_report

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _run(run_id, kind="audit", name="d42_portfolio_roi_replay", verdict="Bug confirmed"):
    return {
        "id": run_id,
        "kind": kind,
        "name": name,
        "verdict": verdict,
        "config_hash": "abcdef0123456789",
        "dataset_hash": "0123456789abcdef",
        "started_at": _NOW,
        "finished_at": _NOW,
    }


def _claim(claim_id, statement="stale claim", status="active"):
    return {
        "id": claim_id,
        "statement": statement,
        "scope": "detector:d42",
        "status": status,
        "last_verified_at": _NOW,
        "verify_halflife_days": 90,
    }


_NO_LLM = {"n_calls": 0, "total_cost_usd": 0.0}


def test_build_report_empty_tables():
    """Пустые agent_runs/agent_claims/agent_llm_calls — отчёт генерируется без ошибок."""
    report = _build_report([], [], _NO_LLM, _NOW)
    assert "Owner's Report" in report
    assert "Нет прогонов" in report
    assert "Реестр клеймов пуст." in report
    assert "$0.00" in report
    assert "LLM-слой ещё не активирован" in report


def test_build_report_runs_carry_source_ids_and_hashes():
    """Каждый прогон несёт agent_runs.id + config_hash + dataset_hash — §8 acceptance."""
    runs = [_run(7, verdict="sum_roi != portfolio_roi, баг подтверждён")]
    report = _build_report(runs, [], _NO_LLM, _NOW)
    assert "agent_runs.id=7" in report
    assert "config_hash=abcdef012345" in report
    assert "dataset_hash=0123456789ab" in report


def test_build_report_claims_grouped_by_status_with_ids():
    """Клеймы группируются по status, каждый несёт agent_claims.id."""
    claims = [
        _claim(1, statement="active claim", status="active"),
        _claim(2, statement="expired claim", status="expired"),
        _claim(3, statement="contradicted claim", status="contradicted"),
    ]
    report = _build_report([], claims, _NO_LLM, _NOW)
    assert "agent_claims.id=1" in report
    assert "agent_claims.id=2" in report
    assert "agent_claims.id=3" in report
    assert "active (1)" in report
    assert "expired (1)" in report
    assert "contradicted (1)" in report
    assert "active=1, expired=1, contradicted=1" in report


def test_build_report_llm_cost_zero_when_no_calls():
    """Phase 0: agent_llm_calls пуста → честный $0, не пересказ."""
    report = _build_report([], [], _NO_LLM, _NOW)
    assert "$0.00" in report
    assert "LLM-слой ещё не активирован" in report


def test_build_report_llm_cost_nonzero_when_calls_exist():
    """Если agent_llm_calls непусто (напр. LLM-сид Task 3) — показывает реальную сумму."""
    llm_calls = {"n_calls": 5, "total_cost_usd": 1.23}
    report = _build_report([], [], llm_calls, _NOW)
    assert "$1.23" in report
    assert "5 вызовов" in report


def test_build_report_kind_grouping():
    """Прогоны группируются по kind (audit/dq/experiment/proposal)."""
    runs = [
        _run(1, kind="audit", name="case1"),
        _run(2, kind="dq", name="may_debt_sweep"),
    ]
    report = _build_report(runs, [], _NO_LLM, _NOW)
    assert "### audit (1)" in report
    assert "### dq (1)" in report


@pytest.mark.asyncio
async def test_run_agent_owners_report_smoke_empty_db(tmp_path, monkeypatch):
    """Smoke: run_agent_owners_report отрабатывает на пустых таблицах, пишет файл, шлёт TG."""
    monkeypatch.chdir(tmp_path)
    with (
        patch("app.jobs.agent_owners_report.fetch", new=AsyncMock(side_effect=[[], [], [_NO_LLM]])),
        patch("app.tasks.notifications.send_admin_message", new=AsyncMock()) as m_send,
    ):
        result = await run_agent_owners_report()

    m_send.assert_awaited_once()
    assert result["n_runs"] == 0
    assert result["n_claims"] == 0
    assert result["llm_cost_usd"] == 0.0
    report_path = tmp_path / result["report_path"]
    assert report_path.exists()
    assert "Owner's Report" in report_path.read_text(encoding="utf-8")

    # Владелец должен видеть полный текст отчёта в TG, не только путь к файлу.
    sent_text = m_send.call_args.args[0]
    assert "Реестр клеймов пуст." in sent_text
    assert "Cost ledger" in sent_text or "Cost ledger".upper() in sent_text.upper()


@pytest.mark.asyncio
async def test_run_agent_owners_report_sends_full_text_to_admin_message(tmp_path, monkeypatch):
    """send_admin_message получает весь report_md (конвертированный в HTML), не сводку."""
    monkeypatch.chdir(tmp_path)
    runs = [_run(7, verdict="уникальный маркер вердикта XYZ123")]
    claims = [_claim(1, statement="уникальный маркер клейма ABC789")]
    with (
        patch(
            "app.jobs.agent_owners_report.fetch",
            new=AsyncMock(side_effect=[runs, claims, [_NO_LLM]]),
        ),
        patch("app.tasks.notifications.send_admin_message", new=AsyncMock()) as m_send,
    ):
        await run_agent_owners_report()

    sent_text = m_send.call_args.args[0]
    assert "XYZ123" in sent_text
    assert "ABC789" in sent_text
    # markdown прогнан через HTML-конвертер — сырых '**'/'##' в TG-тексте быть не должно
    assert "**" not in sent_text
    assert "<b>Owner's Report готов</b>" in sent_text


def test_markdown_to_telegram_html_converts_headers_bold_code():
    """## / **bold** / `code` → Telegram-совместимый HTML."""
    result = _markdown_to_telegram_html("## Заголовок\n**bold text** и `code text`")
    assert "<b>Заголовок</b>" in result
    assert "<b>bold text</b>" in result
    assert "<code>code text</code>" in result
    assert "**" not in result
    assert "##" not in result


def test_markdown_to_telegram_html_escapes_html_special_chars():
    """Данные из БД (verdict-строки) могут содержать '<'/'>'/'&' — должны экранироваться,
    иначе Telegram либо отклонит сообщение как невалидный HTML, либо распарсит чужой тег."""
    result = _markdown_to_telegram_html("verdict: a < b & c > d")
    assert "&lt; b &amp; c &gt;" in result
