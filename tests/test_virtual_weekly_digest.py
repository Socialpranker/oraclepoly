"""Тесты недельной TG-сводки по virtual-ордерам (WIN/LOSS/EXIT/EXPIRED, net PnL, категории).

Virtual-позиции — бэктест-режим бота, per-trade уведомление было бы слишком шумным
(десятки в день), поэтому одна сводка раз в неделю (cron Sunday), а не per-trade
как для live (см. app/jobs/v2_execution_resolve.py::_notify_settlement).

Категорийная разбивка кластеризована по событию (markets.event_id) — per-order
агрегат врёт из-за десятков коррелированных входов в один рынок (аудит 2026-07-07).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.jobs.virtual_weekly_digest as digest_mod


def _dispatch_fetch(outcome_rows, category_rows, trend_rows=None, verdict_rows=None, hold_rows=None):
    """fetch(): outcome → категории → hold-контрфактика → тренды → shadow-гейт."""

    async def _fake_fetch(sql, *args):
        if sql is digest_mod._CATEGORY_WEEKLY_SQL:
            return category_rows
        if sql is digest_mod._HOLD_COUNTERFACTUAL_SQL:
            return hold_rows or []
        if sql is digest_mod._TREND_SQL:
            return trend_rows or []
        if sql is digest_mod._GATE_VERDICT_SQL:
            return verdict_rows or []
        return outcome_rows

    return _fake_fetch


def _cat_row(category, events, net_pnl, roi, best, worst):
    return {
        "category": category,
        "events": events,
        "net_pnl": net_pnl,
        "roi": roi,
        "best_event_pnl": best,
        "worst_event_pnl": worst,
    }


@pytest.mark.asyncio
async def test_digest_no_rows_sends_nothing(monkeypatch):
    notify_mock = AsyncMock()
    monkeypatch.setattr(digest_mod, "fetch", _dispatch_fetch([], []))
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    result = await digest_mod.run_virtual_weekly_digest()

    notify_mock.assert_not_awaited()
    assert result == {"status": "ok", "trades": 0}


@pytest.mark.asyncio
async def test_digest_summarizes_all_outcomes(monkeypatch):
    outcome_rows = [
        {"outcome": "WIN", "n": 42, "net_pnl_sum": 210.50},
        {"outcome": "LOSS", "n": 18, "net_pnl_sum": -95.20},
        {"outcome": "EARLY_EXIT", "n": 7, "net_pnl_sum": 30.00},
        {"outcome": "EXPIRED", "n": 3, "net_pnl_sum": 0.0},
    ]

    notify_mock = AsyncMock()
    monkeypatch.setattr(digest_mod, "fetch", _dispatch_fetch(outcome_rows, []))
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    result = await digest_mod.run_virtual_weekly_digest()

    notify_mock.assert_awaited_once()
    text = notify_mock.await_args.args[0]
    assert "42" in text and "WIN" in text
    assert "18" in text and "LOSS" in text
    assert "7" in text and "EXIT" in text
    assert "3" in text and "EXPIRED" in text
    assert "210.50" in text
    assert "95.20" in text
    # Net = 210.50 - 95.20 + 30.00 = 145.30 — EARLY_EXIT входит в Net
    assert "145.30" in text
    assert result == {"status": "ok", "trades": 70}


@pytest.mark.asyncio
async def test_digest_appends_category_breakdown(monkeypatch):
    outcome_rows = [{"outcome": "WIN", "n": 1, "net_pnl_sum": 5.0}]
    category_rows = [
        # 2 BTC-события делают весь плюс категории → флаг концентрации
        _cat_row("Crypto", 23, 487.30, 0.37, 335.17, -12.0),
        # ровный минус без доминирующего события → без флага
        _cat_row("Sports", 89, -142.44, -0.05, 20.0, -25.0),
    ]

    notify_mock = AsyncMock()
    monkeypatch.setattr(digest_mod, "fetch", _dispatch_fetch(outcome_rows, category_rows))
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    await digest_mod.run_virtual_weekly_digest()

    text = notify_mock.await_args.args[0]
    assert "По категориям" in text
    assert "Crypto: +487.30$ (23 соб., ROI +0.37)" in text
    assert "⚠️ топ-событие +335.17$" in text
    assert "Sports: -142.44$ (89 соб., ROI -0.05)" in text
    # у Sports нет доминирующего события — флага быть не должно
    sports_line = next(line for line in text.splitlines() if "Sports" in line)
    assert "⚠️" not in sports_line


@pytest.mark.asyncio
async def test_digest_appends_gate_verdict_and_trend(monkeypatch):
    outcome_rows = [{"outcome": "WIN", "n": 1, "net_pnl_sum": 5.0}]
    trend_rows = [
        # смена знака при достаточном n → алерт
        {"detector": "D3_cross_mkt_arb", "category": "Crypto", "ev_recent": 12, "ev_prior": 15,
         "roi_recent": -0.10, "roi_prior": 0.20},
        # мало событий в recent-окне → молчим
        {"detector": "D41_momentum", "category": "Politics", "ev_recent": 4, "ev_prior": 20,
         "roi_recent": -0.30, "roi_prior": 0.10},
        # знак не менялся → молчим
        {"detector": "D36_obi_pressure", "category": "Sports", "ev_recent": 50, "ev_prior": 60,
         "roi_recent": -0.02, "roi_prior": -0.04},
    ]
    verdict_rows = [
        {"detector": "D42_event_driven_ml", "category": "Sports", "flagged": 34, "settled": 30, "net_pnl": -254.00},
        {"detector": "D36_obi_pressure", "category": "Culture", "flagged": 10, "settled": 8, "net_pnl": 80.00},
    ]

    notify_mock = AsyncMock()
    monkeypatch.setattr(digest_mod, "fetch", _dispatch_fetch(outcome_rows, [], trend_rows, verdict_rows))
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    await digest_mod.run_virtual_weekly_digest()

    text = notify_mock.await_args.args[0]
    assert "Смена знака ROI" in text
    assert "📉 D3_cross_mkt_arb × Crypto: ROI +0.20 → -0.10 (15/12 соб.)" in text
    assert "D41_momentum" not in text
    assert "D36_obi_pressure × Sports" not in text
    assert "Shadow-гейт категорий" in text
    assert "• D42_event_driven_ml × Sports: сэкономил бы +254.00$ (34 сигн., 30 settled)" in text
    assert "• D36_obi_pressure × Culture: упустил бы +80.00$ (10 сигн., 8 settled)" in text


@pytest.mark.asyncio
async def test_digest_omits_empty_gate_and_trend_sections(monkeypatch):
    outcome_rows = [{"outcome": "WIN", "n": 1, "net_pnl_sum": 5.0}]

    notify_mock = AsyncMock()
    monkeypatch.setattr(digest_mod, "fetch", _dispatch_fetch(outcome_rows, []))
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    await digest_mod.run_virtual_weekly_digest()

    text = notify_mock.await_args.args[0]
    assert "Shadow-гейт" not in text
    assert "Смена знака" not in text


def _hold_row(side, stake, fill_price, net_pnl, category, event_id, winner):
    return {
        "side": side,
        "stake": stake,
        "fill_price": fill_price,
        "net_pnl": net_pnl,
        "category": category,
        "event_id": event_id,
        "fee_schedule": None,
        "winner": winner,
    }


def test_hold_counterfactual_uses_settlement_formula():
    """Hold-PnL считается той же формулой, что прод-settle по winner — не flat fee."""
    from app.jobs.v2_execution_resolve import _compute_settlement_pnl

    rows = [
        # SL срезал будущего winner'а: as-is −15, hold = WIN
        _hold_row("YES", 100.0, 0.40, -15.0, "Sports", "ev1", "yes"),
        # TP забрал у будущего loser'а: as-is +20, hold = LOSS
        _hold_row("NO", 50.0, 0.30, 20.0, "Sports", "ev2", "yes"),
    ]
    expected_hold = (
        _compute_settlement_pnl(stake=100.0, fill_price=0.40, outcome="WIN", category="Sports")["net_pnl"]
        + _compute_settlement_pnl(stake=50.0, fill_price=0.30, outcome="LOSS", category="Sports")["net_pnl"]
    )

    lines = digest_mod._hold_counterfactual_lines(rows)

    assert len(lines) == 1
    assert lines[0].startswith("• Sports: as-is +5.00$ → hold ")
    assert f"hold {expected_hold:+.2f}$" in lines[0]
    assert "2 орд./2 соб." in lines[0]


def test_hold_counterfactual_skips_bad_rows():
    rows = [
        _hold_row("YES", 100.0, 0.0, -15.0, "Sports", "ev1", "yes"),  # fill_price=0 → skip
        _hold_row("YES", 100.0, 0.40, -15.0, "Sports", "ev2", "void"),  # winner не yes/no → skip
    ]
    assert digest_mod._hold_counterfactual_lines(rows) == []


@pytest.mark.asyncio
async def test_digest_appends_hold_section(monkeypatch):
    outcome_rows = [{"outcome": "WIN", "n": 1, "net_pnl_sum": 5.0}]
    hold_rows = [_hold_row("YES", 100.0, 0.40, -15.0, "Sports", "ev1", "yes")]

    notify_mock = AsyncMock()
    monkeypatch.setattr(digest_mod, "fetch", _dispatch_fetch(outcome_rows, [], hold_rows=hold_rows))
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    await digest_mod.run_virtual_weekly_digest()

    text = notify_mock.await_args.args[0]
    assert "Exit-политика: as-is vs hold" in text
    assert "Sports: as-is -15.00$ → hold" in text


def test_hold_counterfactual_sql_scoping():
    sql = digest_mod._HOLD_COUNTERFACTUAL_SQL
    assert "outcome IN ('EARLY_EXIT', 'EXPIRED')" in sql
    assert "m.resolved = true" in sql
    assert "m.winner IN ('yes', 'no')" in sql
    assert "interval '60 days'" in sql


def test_concentration_flag_rules():
    # доминирует худшее событие (по |pnl|) — флаг с отрицательным значением
    line = digest_mod._format_category_line(_cat_row("World", 12, -100.0, -0.10, 10.0, -80.0))
    assert "⚠️ топ-событие -80.00$" in line
    # мало событий — не флагуем, малое n видно и так
    line = digest_mod._format_category_line(_cat_row("Tech", 3, 100.0, 0.20, 90.0, -5.0))
    assert "⚠️" not in line
    # событие < 50% |P&L| — категория, а не выброс
    line = digest_mod._format_category_line(_cat_row("Crypto", 30, 100.0, 0.15, 40.0, -10.0))
    assert "⚠️" not in line


@pytest.mark.asyncio
async def test_digest_sql_scoping():
    for sql in (digest_mod._VIRTUAL_WEEKLY_SQL, digest_mod._CATEGORY_WEEKLY_SQL):
        assert "execution_mode = 'virtual'" in sql
        assert "settled_at >" in sql
        assert "interval '7 days'" in sql
    assert "'EARLY_EXIT'" in digest_mod._VIRTUAL_WEEKLY_SQL
    # кластеризация по событию, EXPIRED вне знаменателя ROI
    assert "m.event_id" in digest_mod._CATEGORY_WEEKLY_SQL
    assert "FILTER (WHERE eo.outcome <> 'EXPIRED')" in digest_mod._CATEGORY_WEEKLY_SQL


@pytest.mark.asyncio
async def test_digest_notify_failure_does_not_raise(monkeypatch):
    outcome_rows = [{"outcome": "WIN", "n": 1, "net_pnl_sum": 5.0}]

    monkeypatch.setattr(digest_mod, "fetch", _dispatch_fetch(outcome_rows, []))
    monkeypatch.setattr(
        "app.tasks.notifications.send_admin_message", AsyncMock(side_effect=RuntimeError("TG down"))
    )

    result = await digest_mod.run_virtual_weekly_digest()

    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_digest_skipped_when_debounce_locked(monkeypatch):
    """Конкурентный ручной перезапуск во время планового cron не должен слать дубль."""

    async def _locked_debounce(_key, ttl):
        return False

    notify_mock = AsyncMock()
    monkeypatch.setattr(digest_mod, "fetch", _dispatch_fetch([{"outcome": "WIN", "n": 1, "net_pnl_sum": 5.0}], []))
    monkeypatch.setattr(digest_mod, "debounce_acquire", _locked_debounce)
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    result = await digest_mod.run_virtual_weekly_digest()

    notify_mock.assert_not_awaited()
    assert result == {"status": "skipped", "reason": "already_running_or_recent"}
