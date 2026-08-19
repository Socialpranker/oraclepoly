"""Tests для app.analytics.decision_retro (Phase 2 C4)."""

from __future__ import annotations

from datetime import date

from app.analytics.decision_retro import RetroRow, format_retro, quarter_bounds, quarter_label


def test_quarter_label_all_quarters():
    assert quarter_label(date(2026, 1, 1)) == "Q1 2026"
    assert quarter_label(date(2026, 3, 31)) == "Q1 2026"
    assert quarter_label(date(2026, 4, 1)) == "Q2 2026"
    assert quarter_label(date(2026, 7, 27)) == "Q3 2026"
    assert quarter_label(date(2026, 12, 31)) == "Q4 2026"


def test_quarter_bounds_is_half_open_interval():
    start, end = quarter_bounds(date(2026, 7, 27))
    assert start == date(2026, 7, 1)
    assert end == date(2026, 10, 1)


def test_quarter_bounds_q4_rolls_into_next_year():
    start, end = quarter_bounds(date(2026, 11, 5))
    assert start == date(2026, 10, 1)
    assert end == date(2027, 1, 1)


def test_format_retro_empty_journal_says_so():
    # Основной сценарий до выхода из shadow — текст должен быть осмысленным, не пустым.
    text = format_retro([], n_open=0, label="Q3 2026")
    assert "Q3 2026" in text
    assert "нет" in text.lower()


def test_format_retro_lists_outcomes_and_open_count():
    rows = [
        RetroRow(scope="detector:D3_x", outcome="improved", n=2),
        RetroRow(scope="detector:D3_x", outcome="worsened", n=1),
    ]
    text = format_retro(rows, n_open=4, label="Q3 2026")
    assert "detector:D3_x" in text
    assert "Q3 2026" in text
    assert "4" in text  # открытые ожидания


# ── run_decision_retro_quarterly ──────────────────────────────────────────────

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

from app.jobs.decision_retro_quarterly import run_decision_retro_quarterly  # noqa: E402


@pytest.mark.asyncio
async def test_retro_on_empty_journal_does_not_crash():
    """Основной сценарий на ближайший квартал: журнал пуст."""
    with (
        patch("app.jobs.decision_retro_quarterly.fetch", new=AsyncMock(return_value=[])),
        patch("app.jobs.decision_retro_quarterly.fetchval", new=AsyncMock(return_value=0)),
        patch("app.jobs.decision_retro_quarterly.execute", new=AsyncMock(return_value="INSERT 0 1")),
        patch("app.jobs.decision_retro_quarterly._send_retro", new=AsyncMock(return_value=True)) as send,
    ):
        summary = await run_decision_retro_quarterly()

    assert summary["status"] == "ok"
    assert summary["n_closed"] == 0
    assert summary["n_open"] == 0
    # Дайджест отправляется даже пустым — «ожиданий нет» это тоже ответ на вопрос.
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_retro_aggregates_outcomes_and_persists_run():
    rows = [
        {"scope": "detector:D3_x", "outcome": "improved", "n": 2},
        {"scope": "detector:D3_x", "outcome": "worsened", "n": 1},
    ]
    with (
        patch("app.jobs.decision_retro_quarterly.fetch", new=AsyncMock(return_value=rows)),
        patch("app.jobs.decision_retro_quarterly.fetchval", new=AsyncMock(return_value=4)),
        patch("app.jobs.decision_retro_quarterly.execute", new=AsyncMock(return_value="INSERT 0 1")) as ex,
        patch("app.jobs.decision_retro_quarterly._send_retro", new=AsyncMock(return_value=True)),
    ):
        summary = await run_decision_retro_quarterly()

    assert summary["n_closed"] == 3
    assert summary["n_open"] == 4
    assert summary["quarter"].startswith("Q")
    assert ex.await_count == 1  # persist agent_runs


@pytest.mark.asyncio
async def test_retro_tg_sent_true_when_delivery_succeeds():
    """Регрессия: _send_retro должен вызывать функцию, которая реально возвращает bool.

    Мокаем не _send_retro целиком, а send_telegram_with_keyboard_rows — ту, что
    вызывается внутри. Если бы код всё ещё звал send_telegram_message (implicit
    None на успехе), sent никогда не станет True и этот тест бы упал.
    """
    with (
        patch("app.jobs.decision_retro_quarterly.fetch", new=AsyncMock(return_value=[])),
        patch("app.jobs.decision_retro_quarterly.fetchval", new=AsyncMock(return_value=0)),
        patch("app.jobs.decision_retro_quarterly.execute", new=AsyncMock(return_value="INSERT 0 1")),
        patch("app.jobs.decision_retro_quarterly.get_settings") as mock_settings,
        patch("app.api.telegram.send_telegram_with_keyboard_rows", new=AsyncMock(return_value=True)),
    ):
        mock_settings.return_value.admin_telegram_ids = [12345]
        summary = await run_decision_retro_quarterly()

    assert summary["tg_sent"] is True


@pytest.mark.asyncio
async def test_retro_tg_sent_false_when_all_admins_fail():
    with (
        patch("app.jobs.decision_retro_quarterly.fetch", new=AsyncMock(return_value=[])),
        patch("app.jobs.decision_retro_quarterly.fetchval", new=AsyncMock(return_value=0)),
        patch("app.jobs.decision_retro_quarterly.execute", new=AsyncMock(return_value="INSERT 0 1")),
        patch("app.jobs.decision_retro_quarterly.get_settings") as mock_settings,
        patch("app.api.telegram.send_telegram_with_keyboard_rows", new=AsyncMock(return_value=False)),
    ):
        mock_settings.return_value.admin_telegram_ids = [12345]
        summary = await run_decision_retro_quarterly()

    assert summary["tg_sent"] is False
