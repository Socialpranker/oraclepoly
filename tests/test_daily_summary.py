"""Tests для app.jobs.daily_summary."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.daily_summary import _build_message, _format_delta, run_daily_summary


@pytest.mark.asyncio
async def test_run_daily_summary_smoke():
    """Smoke: run_daily_summary отрабатывает с SSoT-хелперами активности.

    Ловит broken import (как было: _parse_active_cron_detectors из slo_monitor) —
    форматтер-тесты этого НЕ покрывали, поэтому смок обязателен. n_active_db/cron
    читаются через fetch_db_active_detectors/parse_active_cron_detectors (импорт
    внутри функции → патчим источник app.services.detector_metrics.*).
    """
    with (
        patch("app.jobs.daily_summary.fetch", new=AsyncMock(return_value=[])),
        patch(
            "app.services.detector_metrics.fetch_db_active_detectors",
            new=AsyncMock(return_value={"D36_obi_pressure", "D44_late_convergence_carry"}),
        ),
        patch(
            "app.services.detector_metrics.parse_active_cron_detectors",
            return_value={"D36_obi_pressure"},
        ),
        patch("app.jobs.daily_summary.get_settings") as m_settings,
    ):
        m_settings.return_value.admin_telegram_ids = []
        result = await run_daily_summary()

    assert result["n_sent"] == 0
    assert result["n_detectors_with_data"] == 0


def test_format_delta_positive_growth():
    """Today > yesterday → positive delta + percent."""
    s = _format_delta(127.5, 94.0)
    assert "+" in s
    assert "$33.50" in s
    assert "+36%" in s or "+35%" in s  # rounding


def test_format_delta_loss_from_profit():
    """Today loss, yesterday profit."""
    s = _format_delta(-50.0, 100.0)
    assert "−" in s
    assert "$150.00" in s


def test_format_delta_yesterday_zero_no_percent():
    """Yesterday=0 → percent shown as — (нельзя делить на ноль)."""
    s = _format_delta(50.0, 0.0)
    assert "—" in s
    assert "$50.00" in s


def test_format_delta_both_zero():
    """Both zero → 0 delta, no percent."""
    s = _format_delta(0.0, 0.0)
    assert "$0.00" in s
    assert "—" in s


def test_build_message_no_data():
    """Empty rows → still valid message (no top/bottom sections, no win-rate)."""
    msg = _build_message([], n_active_db=10, n_active_cron=15)
    assert "OraclePoly Daily Summary" in msg
    assert "Placed: <b>0</b>" in msg
    assert "10 (db)" in msg
    assert "15 (cron)" in msg
    # No top/bottom blocks when no data
    assert "🏆" not in msg
    assert "📉" not in msg
    # No win-rate line когда не было resolved
    assert "Resolved: WIN" not in msg
    assert "Early exits:" not in msg


def _row(detector, pnl_24h, settled_24h=5, wins=0, losses=0, ee=0, ee_win=0, pnl_prev=0.0, pnl_7d=0.0):
    """Test row builder с дефолтами для краткости."""
    return {
        "detector": detector,
        "placed_24h": settled_24h + 2,
        "filled_24h": settled_24h,
        "settled_24h": settled_24h,
        "wins_24h": wins,
        "losses_24h": losses,
        "early_exits_24h": ee,
        "early_exits_win_24h": ee_win,
        "pnl_24h": pnl_24h,
        "pnl_prev_24h": pnl_prev,
        "pnl_7d": pnl_7d,
    }


def test_build_message_with_top_and_bottom():
    """Mix of winners and losers → top + bottom sections present."""
    rows = [
        _row("D_winner", pnl_24h=100.0, settled_24h=6, wins=5, losses=1, pnl_prev=50.0, pnl_7d=500.0),
        _row("D_loser", pnl_24h=-60.0, settled_24h=4, wins=0, losses=4, pnl_prev=-20.0, pnl_7d=-150.0),
    ]
    msg = _build_message(rows, n_active_db=2, n_active_cron=2)

    # Aggregates: 8+6 = 14 placed (each row +2)
    assert "Placed: <b>14</b>" in msg
    # Resolved block: wins=5, losses=5 → 50%
    assert "Resolved: WIN <b>5</b>" in msg
    assert "(50%)" in msg

    # P&L aggregation
    assert "+$40.00" in msg  # 100 - 60
    assert "+$350.00" in msg  # 500 - 150 (7d)

    # Top/bottom
    assert "D_winner" in msg
    assert "D_loser" in msg
    assert "🏆" in msg
    assert "📉" in msg


def test_build_message_all_winners_no_bottom_section():
    """All detectors PnL >= 0 → no bottom section."""
    rows = [_row("D_winner", pnl_24h=50.0, settled_24h=5, wins=4, losses=1, pnl_prev=30.0, pnl_7d=200.0)]
    msg = _build_message(rows, n_active_db=1, n_active_cron=1)
    assert "🏆" in msg
    assert "📉" not in msg


def test_build_message_loser_not_in_top():
    """Bug fix: loser detector НЕ должен попадать в Top (раньше попадал т.к. top = max())."""
    rows = [_row("D_only_loser", pnl_24h=-100.0, settled_24h=5, losses=5, pnl_7d=-100.0)]
    msg = _build_message(rows, n_active_db=1, n_active_cron=1)
    assert "📉" in msg
    assert "🏆" not in msg  # no positive → no top section


def test_build_message_early_exit_only():
    """All trades EARLY_EXIT (текущий реальный pipeline) → показываем EE block, не WIN/LOSS."""
    rows = [
        _row(
            "D_ee_only", pnl_24h=-200.0, settled_24h=10,
            wins=0, losses=0, ee=10, ee_win=3, pnl_prev=0.0, pnl_7d=-200.0,
        ),
    ]
    msg = _build_message(rows, n_active_db=1, n_active_cron=1)
    assert "Early exits: 10 (3 в плюс = 30%)" in msg
    assert "Resolved: WIN" not in msg  # нет resolved → no win-rate line


def test_build_message_html_safe_amp_for_telegram():
    """TG parse_mode=HTML — & должен быть &amp;. Иначе TG отбрасывает сообщение."""
    msg = _build_message([], n_active_db=1, n_active_cron=1)
    # P&L → P&amp;L
    assert "P&amp;L" in msg
    assert "P&L" not in msg or msg.count("P&amp;L") == msg.count("P&L".replace("&", "&amp;"))
