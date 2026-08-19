"""Tests для app.jobs.improvement_brief_weekly."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.analytics.improvement_brief import BriefCard
from app.jobs.improvement_brief_weekly import (
    _build_keyboard_rows,
    _week_label,
    run_improvement_brief_weekly,
)


def _card(card_id: int, ev: float | None, status: str = "open") -> BriefCard:
    return BriefCard(
        id=card_id,
        card_key=f"adverse_selection:detector:D{card_id}",
        title=f"Card {card_id}",
        leak_usd=-100.0,
        effort_hours=2.0,
        ev_per_hour=ev,
        status=status,
        source_kind="adverse_selection",
    )


# ── чистые хелперы (без моков) ────────────────────────────────────────────────


def test_build_keyboard_rows_one_row_per_card():
    from datetime import date

    rows = _build_keyboard_rows([_card(1, 500.0), _card(2, 300.0)], date(2026, 7, 27))
    assert len(rows) == 2
    # Ряд карточки: 3 кнопки-действия с номером карточки в тексте
    assert [b["callback_data"] for b in rows[0]] == [
        "impcard:accept:1:20260727",
        "impcard:defer:1:20260727",
        "impcard:reject:1:20260727",
    ]
    assert all("1" in b["text"] for b in rows[0])


def test_week_label_format():
    from datetime import date

    # Понедельник 27.07 → неделя 27.07–02.08
    assert _week_label(date(2026, 7, 27)) == "27.07–02.08"


# ── run_improvement_brief_weekly ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_happy_path_sends_and_persists():
    cards = [_card(1, 500.0), _card(2, 300.0, status="deferred"), _card(3, 100.0), _card(4, 50.0)]
    with (
        patch(
            "app.jobs.improvement_brief_weekly._fetch_brief_cards",
            new=AsyncMock(return_value=cards),
        ),
        patch(
            "app.jobs.improvement_brief_weekly._fetch_rejected_total",
            new=AsyncMock(return_value=7),
        ),
        patch("app.jobs.improvement_brief_weekly._send_brief", new=AsyncMock(return_value=True)) as send,
        patch("app.jobs.improvement_brief_weekly._persist_run", new=AsyncMock()) as persist,
    ):
        result = await run_improvement_brief_weekly()
    assert result["status"] == "ok"
    assert result["n_cards"] == 4
    assert result["n_top"] == 3 and result["n_tail"] == 1
    assert result["n_deferred"] == 1  # одна deferred в выборке
    assert result["n_rejected_total"] == 7
    assert result["top_ev"] == pytest.approx(500.0)
    assert result["tg_sent"] is True
    send.assert_awaited_once()
    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_no_cards_skips_telegram():
    with (
        patch("app.jobs.improvement_brief_weekly._fetch_brief_cards", new=AsyncMock(return_value=[])),
        patch("app.jobs.improvement_brief_weekly._fetch_rejected_total", new=AsyncMock(return_value=0)),
        patch("app.jobs.improvement_brief_weekly._send_brief", new=AsyncMock()) as send,
        patch("app.jobs.improvement_brief_weekly._persist_run", new=AsyncMock()) as persist,
    ):
        result = await run_improvement_brief_weekly()
    assert result["status"] == "no_cards"
    assert result["tg_sent"] is False
    send.assert_not_awaited()
    persist.assert_awaited_once()  # run persist ВСЕГДА


@pytest.mark.asyncio
async def test_run_telegram_failure_is_not_fatal():
    with (
        patch(
            "app.jobs.improvement_brief_weekly._fetch_brief_cards",
            new=AsyncMock(return_value=[_card(1, 500.0)]),
        ),
        patch("app.jobs.improvement_brief_weekly._fetch_rejected_total", new=AsyncMock(return_value=0)),
        patch("app.jobs.improvement_brief_weekly._send_brief", new=AsyncMock(return_value=False)),
        patch("app.jobs.improvement_brief_weekly._persist_run", new=AsyncMock()) as persist,
    ):
        result = await run_improvement_brief_weekly()  # не бросает
    assert result["status"] == "ok"
    assert result["tg_sent"] is False
    persist.assert_awaited_once()
