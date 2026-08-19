"""Unit tests для demote_alerts_monitor cron job (PR-C + Phase 2 live-мониторинг)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.jobs.demote_alerts_monitor import run_demote_alerts_monitor
from app.services.notification_bus import PublishResult


def _published_text(send_tg_mock) -> str:
    """Весь текст уведомлений одной публикации — замена распаковки (admin_id, text).

    Джоба больше не форматирует строку и не знает про админов: она отдаёт шине
    список Notification, а рассылку делает publish.
    """
    notifications = send_tg_mock.call_args[0][0]
    parts = []
    for n in notifications:
        parts.extend([n.headline, *n.what_happened, n.what_to_do or "", *n.evidence])
    return " ".join(parts)


@pytest.fixture
def _settings_with_admin():
    """Mock settings с одним admin recipient, live_auto_pause_enabled=False (default)."""
    mock_settings = MagicMock()
    mock_settings.admin_telegram_ids = [12345]
    mock_settings.live_auto_pause_enabled = False
    return mock_settings


@pytest.fixture
def _settings_with_auto_pause():
    """Mock settings с live_auto_pause_enabled=True."""
    mock_settings = MagicMock()
    mock_settings.admin_telegram_ids = [12345]
    mock_settings.live_auto_pause_enabled = True
    return mock_settings


def _failing_shadow(terminal: int = 60) -> dict:
    """Signal-level экономика, проваливающая promote G2/G5 (sum_roi_net<0)."""
    return {
        "terminal_7d": terminal,
        "wins_7d": 10,
        "sum_roi_net_7d": -400.0,
        "mean_roi_net_7d": -6.0,
        "sum_neg_sq_roi_7d": 6000.0,
        "median_entry_price_7d": 0.5,
    }


@pytest.mark.asyncio
async def test_no_virtual_detectors_returns_zero():
    """Нет virtual/live детекторов — early return, никаких alerts."""
    with (
        patch(
            "app.jobs.demote_alerts_monitor.fetch_virtual_metrics_7d",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_live_metrics_7d",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        result = await run_demote_alerts_monitor()
    assert result == {"n_virtual": 0, "n_candidates": 0, "n_alerted": 0, "n_deduped": 0}


@pytest.mark.asyncio
async def test_virtual_but_no_candidates_returns_zero():
    """Virtual детекторы есть, но ни один не triggered demote criteria."""
    virtual_metrics = {
        "D1_fade_whale": {"n_settled": 20, "pnl_usd": -800.0},  # terminal < 40 → skip
        "D7b_no_favorite_fade": {"n_settled": 50, "pnl_usd": -100.0},  # экономика OK → skip
    }
    shadow_signals = {
        "D1_fade_whale": {  # terminal < 40 — отсекается по sample floor
            "terminal_7d": 20, "wins_7d": 2, "sum_roi_net_7d": -300.0,
            "mean_roi_net_7d": -15.0, "sum_neg_sq_roi_7d": 9000.0, "median_entry_price_7d": 0.5,
        },
        "D7b_no_favorite_fade": {  # экономика положительна → не candidate
            "terminal_7d": 50, "wins_7d": 30, "sum_roi_net_7d": 180.0,
            "mean_roi_net_7d": 3.6, "sum_neg_sq_roi_7d": 80.0, "median_entry_price_7d": 0.3,
        },
    }
    with (
        patch(
            "app.jobs.demote_alerts_monitor.fetch_virtual_metrics_7d",
            new_callable=AsyncMock,
            return_value=virtual_metrics,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_live_metrics_7d",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_shadow_signals_7d",
            new_callable=AsyncMock,
            return_value=shadow_signals,
        ),
    ):
        result = await run_demote_alerts_monitor()
    assert result["n_candidates"] == 0
    assert result["n_alerted"] == 0


@pytest.mark.asyncio
async def test_candidate_triggers_telegram_alert(_settings_with_admin):
    """Detector с проваленной signal-экономикой на N≥40 → TG alert отправляется."""
    virtual_metrics = {
        "D38_resolution_lag": {"n_settled": 100, "pnl_usd": -1500.0},
    }
    shadow_signals = {"D38_resolution_lag": _failing_shadow(terminal=100)}
    send_tg_mock = AsyncMock(return_value=PublishResult(sent=1))
    execute_mock = AsyncMock()

    with (
        patch(
            "app.jobs.demote_alerts_monitor.fetch_virtual_metrics_7d",
            new_callable=AsyncMock,
            return_value=virtual_metrics,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_live_metrics_7d",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_shadow_signals_7d",
            new_callable=AsyncMock,
            return_value=shadow_signals,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch",
            new_callable=AsyncMock,
            return_value=[],  # нет recent alerts
        ),
        patch(
            "app.jobs.demote_alerts_monitor.get_settings",
            return_value=_settings_with_admin,
        ),
        patch("app.jobs.demote_alerts_monitor.publish", send_tg_mock),
        patch("app.jobs.demote_alerts_monitor.execute", execute_mock),
    ):
        result = await run_demote_alerts_monitor()

    assert result["n_candidates"] == 1
    assert result["n_alerted"] == 1
    send_tg_mock.assert_called_once()
    # Verify TG message содержит detector + signal ROI
    text = _published_text(send_tg_mock)
    assert "D38_resolution_lag" in text
    assert "доходность сигналов" in text
    # Verify dedupe insert
    execute_mock.assert_called_once()


@pytest.mark.asyncio
async def test_dedupe_skips_recent_alert(_settings_with_admin):
    """Detector уже alerted в последние 6h — не повторяем."""
    virtual_metrics = {
        "D38_resolution_lag": {"n_settled": 100, "pnl_usd": -1500.0},
    }
    shadow_signals = {"D38_resolution_lag": _failing_shadow(terminal=100)}
    send_tg_mock = AsyncMock(return_value=PublishResult(sent=1))

    with (
        patch(
            "app.jobs.demote_alerts_monitor.fetch_virtual_metrics_7d",
            new_callable=AsyncMock,
            return_value=virtual_metrics,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_live_metrics_7d",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_shadow_signals_7d",
            new_callable=AsyncMock,
            return_value=shadow_signals,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch",
            new_callable=AsyncMock,
            return_value=[{"detector": "D38_resolution_lag", "last_alert": "2026-05-27"}],
        ),
        patch(
            "app.jobs.demote_alerts_monitor.get_settings",
            return_value=_settings_with_admin,
        ),
        patch("app.jobs.demote_alerts_monitor.publish", send_tg_mock),
    ):
        result = await run_demote_alerts_monitor()

    assert result["n_candidates"] == 1
    assert result["n_alerted"] == 0  # deduped
    assert result["n_deduped"] == 1
    send_tg_mock.assert_not_called()


@pytest.mark.asyncio
async def test_dedupe_table_missing_falls_open(_settings_with_admin):
    """demote_alert_log table не существует — fail-open: alert всё равно идёт."""
    virtual_metrics = {
        "D38_resolution_lag": {"n_settled": 100, "pnl_usd": -1500.0},
    }
    send_tg_mock = AsyncMock(return_value=PublishResult(sent=1))
    execute_mock = AsyncMock()
    fetch_mock = AsyncMock(side_effect=RuntimeError("table does not exist"))

    with (
        patch(
            "app.jobs.demote_alerts_monitor.fetch_virtual_metrics_7d",
            new_callable=AsyncMock,
            return_value=virtual_metrics,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_live_metrics_7d",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_shadow_signals_7d",
            new_callable=AsyncMock,
            return_value={"D38_resolution_lag": _failing_shadow(terminal=100)},
        ),
        patch("app.jobs.demote_alerts_monitor.fetch", fetch_mock),
        patch(
            "app.jobs.demote_alerts_monitor.get_settings",
            return_value=_settings_with_admin,
        ),
        patch("app.jobs.demote_alerts_monitor.publish", send_tg_mock),
        patch("app.jobs.demote_alerts_monitor.execute", execute_mock),
    ):
        result = await run_demote_alerts_monitor()

    # Fail-open: candidate всё равно alerted
    assert result["n_alerted"] == 1
    send_tg_mock.assert_called_once()


@pytest.mark.asyncio
async def test_telegram_failure_does_not_write_dedupe(_settings_with_admin):
    """Если TG fail — не записываем dedupe (чтобы следующий tick мог retry)."""
    virtual_metrics = {
        "D38_resolution_lag": {"n_settled": 100, "pnl_usd": -1500.0},
    }
    send_tg_mock = AsyncMock(side_effect=RuntimeError("tg api down"))
    execute_mock = AsyncMock()

    with (
        patch(
            "app.jobs.demote_alerts_monitor.fetch_virtual_metrics_7d",
            new_callable=AsyncMock,
            return_value=virtual_metrics,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_live_metrics_7d",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_shadow_signals_7d",
            new_callable=AsyncMock,
            return_value={"D38_resolution_lag": _failing_shadow(terminal=100)},
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.jobs.demote_alerts_monitor.get_settings",
            return_value=_settings_with_admin,
        ),
        patch("app.jobs.demote_alerts_monitor.publish", send_tg_mock),
        patch("app.jobs.demote_alerts_monitor.execute", execute_mock),
    ):
        result = await run_demote_alerts_monitor()

    # TG failed → n_alerted=0, и execute (dedupe write) НЕ вызывался
    assert result["n_alerted"] == 0
    execute_mock.assert_not_called()


# ── Phase 2: live-детекторы в demote-контуре ──────────────────────────────────


@pytest.mark.asyncio
async def test_live_candidate_alert_only_when_auto_pause_disabled(_settings_with_admin):
    """Live-детектор провалил экономику, live_auto_pause_enabled=False (default)
    → alert-only, mark_paused НЕ вызывается."""
    live_metrics = {
        "D3_cross_mkt_arb": {"n_settled": 100, "pnl_usd": -1500.0},
    }
    shadow_signals = {"D3_cross_mkt_arb": _failing_shadow(terminal=100)}
    send_tg_mock = AsyncMock(return_value=PublishResult(sent=1))
    mark_paused_mock = AsyncMock()

    with (
        patch(
            "app.jobs.demote_alerts_monitor.fetch_virtual_metrics_7d",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_live_metrics_7d",
            new_callable=AsyncMock,
            return_value=live_metrics,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_shadow_signals_7d",
            new_callable=AsyncMock,
            return_value=shadow_signals,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.jobs.demote_alerts_monitor.get_settings",
            return_value=_settings_with_admin,
        ),
        patch("app.jobs.demote_alerts_monitor.publish", send_tg_mock),
        patch("app.jobs.demote_alerts_monitor.execute", AsyncMock()),
        patch("app.jobs.demote_alerts_monitor.mark_paused", mark_paused_mock),
    ):
        result = await run_demote_alerts_monitor()

    assert result["n_candidates"] == 1
    assert result["n_alerted"] == 1
    assert result["n_live_auto_paused"] == 0
    mark_paused_mock.assert_not_called()
    text = _published_text(send_tg_mock)
    assert "D3_cross_mkt_arb" in text
    assert "продолжает работать" in text  # автопауза выключена — детектор жив


@pytest.mark.asyncio
async def test_live_candidate_auto_paused_when_enabled(_settings_with_auto_pause):
    """Live-детектор провалил экономику, live_auto_pause_enabled=True →
    mark_paused() вызывается + admin alert упоминает AUTO-PAUSE."""
    live_metrics = {
        "D3_cross_mkt_arb": {"n_settled": 100, "pnl_usd": -1500.0},
    }
    shadow_signals = {"D3_cross_mkt_arb": _failing_shadow(terminal=100)}
    send_tg_mock = AsyncMock(return_value=PublishResult(sent=1))
    mark_paused_mock = AsyncMock()

    with (
        patch(
            "app.jobs.demote_alerts_monitor.fetch_virtual_metrics_7d",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_live_metrics_7d",
            new_callable=AsyncMock,
            return_value=live_metrics,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_shadow_signals_7d",
            new_callable=AsyncMock,
            return_value=shadow_signals,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.jobs.demote_alerts_monitor.get_settings",
            return_value=_settings_with_auto_pause,
        ),
        patch("app.jobs.demote_alerts_monitor.publish", send_tg_mock),
        patch("app.jobs.demote_alerts_monitor.execute", AsyncMock()),
        patch("app.jobs.demote_alerts_monitor.mark_paused", mark_paused_mock),
    ):
        result = await run_demote_alerts_monitor()

    assert result["n_candidates"] == 1
    assert result["n_alerted"] == 1
    assert result["n_live_auto_paused"] == 1
    mark_paused_mock.assert_called_once()
    call_args = mark_paused_mock.call_args
    assert call_args[0][0] == "D3_cross_mkt_arb"
    assert "auto_demote" in call_args[0][1]
    text = _published_text(send_tg_mock)
    assert "остановлен" in text  # автопауза сработала


@pytest.mark.asyncio
async def test_virtual_and_live_candidates_both_reported(_settings_with_admin):
    """Смешанный кейс: virtual-кандидат + live-кандидат в одном тике —
    оба попадают в n_candidates, virtual остаётся alert-only независимо от flag."""
    virtual_metrics = {"D7b_no_favorite_fade": {"n_settled": 100, "pnl_usd": -900.0}}
    live_metrics = {"D3_cross_mkt_arb": {"n_settled": 100, "pnl_usd": -1500.0}}
    shadow_signals = {
        "D7b_no_favorite_fade": _failing_shadow(terminal=90),
        "D3_cross_mkt_arb": _failing_shadow(terminal=100),
    }
    send_tg_mock = AsyncMock(return_value=PublishResult(sent=1))

    with (
        patch(
            "app.jobs.demote_alerts_monitor.fetch_virtual_metrics_7d",
            new_callable=AsyncMock,
            return_value=virtual_metrics,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_live_metrics_7d",
            new_callable=AsyncMock,
            return_value=live_metrics,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch_shadow_signals_7d",
            new_callable=AsyncMock,
            return_value=shadow_signals,
        ),
        patch(
            "app.jobs.demote_alerts_monitor.fetch",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.jobs.demote_alerts_monitor.get_settings",
            return_value=_settings_with_admin,
        ),
        patch("app.jobs.demote_alerts_monitor.publish", send_tg_mock),
        patch("app.jobs.demote_alerts_monitor.execute", AsyncMock()),
        patch("app.jobs.demote_alerts_monitor.mark_paused", AsyncMock()),
    ):
        result = await run_demote_alerts_monitor()

    assert result["n_candidates"] == 2
    assert result["n_alerted"] == 2
    text = _published_text(send_tg_mock)
    assert "D7b_no_favorite_fade" in text
    assert "D3_cross_mkt_arb" in text
