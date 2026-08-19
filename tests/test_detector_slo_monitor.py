"""Tests для app.jobs.detector_slo_monitor."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.detector_slo_monitor import (
    ALERT_RATIO_THRESHOLD,
    EXPECTED_RATE_PER_DAY,
    MIN_EXPECTED_24H_FOR_RATIO_CHECK,
    run_detector_slo_check,
)


@pytest.fixture
def mock_settings():
    """Мок шины уведомлений. Имя фикстуры оставлено прежним, чтобы миграция
    на конверт не переписывала сигнатуры всех тестов файла: джоба больше не
    читает settings сама — админов знает шина."""
    with patch("app.jobs.detector_slo_monitor.publish", new=AsyncMock()) as m:
        yield m


def _mock_fetch_returns(signal_rows):
    """Build async fetch side_effect that returns signal_rows (only signals query now)."""

    async def _fetch(query, *args):
        return signal_rows

    return _fetch


@contextlib.contextmanager
def _patch_active(active_detectors):
    """Патч обоих источников активности: cron-парсер и DB-хелпер."""
    active_set = set(active_detectors)
    with (
        patch(
            "app.jobs.detector_slo_monitor.parse_active_cron_detectors",
            return_value=active_set,
        ),
        patch(
            "app.jobs.detector_slo_monitor.fetch_db_active_detectors",
            new=AsyncMock(return_value=active_set),
        ),
    ):
        yield


@pytest.mark.asyncio
async def test_all_detectors_meeting_baseline_no_alerts(mock_settings):
    """Baseline emission rates met → no alerts.

    D3 (0.37/d) и D14 (0.5/d) ниже MIN_EXPECTED_24H_FOR_RATIO_CHECK=2.0 →
    low_rate_skip. D13 (4.2/d) выше порога → ratio check проходит → ok.
    Проверяем 1 ok + 2 low_rate_skip, alerts=0.
    """
    actual_signals = [
        {"detector": "D3_cross_mkt_arb", "n_24h": 0},  # baseline 0.37/d (low_rate_skip)
        {"detector": "D13_retail_confidence_fade", "n_24h": 5},  # baseline 4.2/d
        {"detector": "D14_crypto_winners_distribute_no", "n_24h": 4},  # baseline 0.5/d (low_rate_skip)
    ]
    active = ["D3_cross_mkt_arb", "D13_retail_confidence_fade", "D14_crypto_winners_distribute_no"]

    with (
        patch(
            "app.jobs.detector_slo_monitor.fetch",
            side_effect=_mock_fetch_returns(actual_signals),
        ),
        _patch_active(active),
    ):
        result = await run_detector_slo_check()

    assert result["alerts"] == 0
    assert result["ok"] == 1
    statuses = [b["status"] for b in result["breakdown"]]
    assert statuses.count("low_rate_skip") == 2


@pytest.mark.asyncio
async def test_zero_signals_alerts(mock_settings):
    """D13 baseline 4.2/d — 0 за 24h = catastrophic (ratio 0 < 30%), alert."""
    actual_signals = []
    active = ["D13_retail_confidence_fade"]

    with (
        patch(
            "app.jobs.detector_slo_monitor.fetch",
            side_effect=_mock_fetch_returns(actual_signals),
        ),
        _patch_active(active),
    ):
        result = await run_detector_slo_check()

    assert result["alerts"] == 1
    assert result["ok"] == 0
    # Риск №1 §10: молчащий детектор обязан ДОЙТИ до владельца.
    mock_settings.assert_awaited_once()
    n = mock_settings.await_args[0][0][0]
    assert n.subject == "detector:D13_retail_confidence_fade"


@pytest.mark.asyncio
async def test_low_rate_detector_skipped(mock_settings):
    """Low-rate detectors (D3 0.37/d, D14 0.5/d) skip ratio check."""
    actual_signals = [{"detector": "D3_cross_mkt_arb", "n_24h": 0}]
    active = ["D3_cross_mkt_arb"]

    with (
        patch(
            "app.jobs.detector_slo_monitor.fetch",
            side_effect=_mock_fetch_returns(actual_signals),
        ),
        _patch_active(active),
    ):
        result = await run_detector_slo_check()

    # Low-rate skip — no alert
    assert result["alerts"] == 0
    statuses = [b["status"] for b in result["breakdown"]]
    assert "low_rate_skip" in statuses


@pytest.mark.asyncio
async def test_no_baseline_logged_not_alerted(mock_settings):
    """Detector в active state (БД + cron) но без baseline → no alert (warning logged)."""
    actual_signals = [{"detector": "D_unknown_new", "n_24h": 5}]
    active = ["D_unknown_new"]

    with (
        patch(
            "app.jobs.detector_slo_monitor.fetch",
            side_effect=_mock_fetch_returns(actual_signals),
        ),
        _patch_active(active),
    ):
        result = await run_detector_slo_check()

    assert result["alerts"] == 0
    statuses = [b["status"] for b in result["breakdown"]]
    assert "no_baseline" in statuses


@pytest.mark.asyncio
async def test_db_active_but_cron_cut_is_excluded(mock_settings):
    """SSoT-инвариант: detector активен в БД, но cron закомментирован → исключён (db ∩ cron).

    Ловит '&' vs '|': D13 в БД-active с 0 сигналов (catastrophic при baseline 4.2/d),
    но НЕ в cron-active. effective = db & cron → D13 отсутствует → НЕТ алерта и НЕТ
    строки breakdown. При ошибочном '|' D13 попал бы в проверку и дал alert=1.
    """
    actual_signals = []  # 0 сигналов — был бы catastrophic, попади D13 в проверку
    with (
        patch(
            "app.jobs.detector_slo_monitor.fetch",
            side_effect=_mock_fetch_returns(actual_signals),
        ),
        patch(
            "app.jobs.detector_slo_monitor.fetch_db_active_detectors",
            new=AsyncMock(return_value={"D13_retail_confidence_fade"}),
        ),
        patch(
            "app.jobs.detector_slo_monitor.parse_active_cron_detectors",
            return_value=set(),  # cron закомментирован — пусто
        ),
    ):
        result = await run_detector_slo_check()

    assert result["alerts"] == 0
    detectors_in_breakdown = [b["detector"] for b in result["breakdown"]]
    assert "D13_retail_confidence_fade" not in detectors_in_breakdown


@pytest.mark.asyncio
async def test_partial_drop_below_threshold_alerts(mock_settings):
    """D13 1/24h из baseline 4.2 = 24% < 30% threshold → alert."""
    actual_signals = [{"detector": "D13_retail_confidence_fade", "n_24h": 1}]
    active = ["D13_retail_confidence_fade"]

    with (
        patch(
            "app.jobs.detector_slo_monitor.fetch",
            side_effect=_mock_fetch_returns(actual_signals),
        ),
        _patch_active(active),
    ):
        result = await run_detector_slo_check()

    assert result["alerts"] == 1


@pytest.mark.asyncio
async def test_threshold_boundary_just_above_no_alert(mock_settings):
    """D13 2/4.2 = 48% → above 30% → no alert."""
    actual_signals = [{"detector": "D13_retail_confidence_fade", "n_24h": 2}]
    active = ["D13_retail_confidence_fade"]

    with (
        patch(
            "app.jobs.detector_slo_monitor.fetch",
            side_effect=_mock_fetch_returns(actual_signals),
        ),
        _patch_active(active),
    ):
        result = await run_detector_slo_check()

    assert result["alerts"] == 0
    assert result["ok"] == 1


def test_baseline_rates_match_active_detectors():
    """Каждый detector в EXPECTED_RATE_PER_DAY должен быть в production."""
    expected_active = {
        "D3_cross_mkt_arb",
        "D13_retail_confidence_fade",
        "D14_crypto_winners_distribute_no",
    }
    assert set(EXPECTED_RATE_PER_DAY.keys()) == expected_active


def test_alert_threshold_reasonable():
    """Threshold 0.3 — 30% drop signals real broken state, не case noise."""
    assert 0.1 < ALERT_RATIO_THRESHOLD < 0.5


def test_min_expected_threshold_reasonable():
    """Low-rate skip threshold 2/24h — protects D3/D14 от false alerts."""
    assert 1.0 < MIN_EXPECTED_24H_FOR_RATIO_CHECK < 5.0


def test_parse_active_cron_detectors_skips_commented_lines(tmp_path):
    """Закомментированные # cron-строки → не активны. Активные → попадают в set."""
    from app.services.detector_metrics import parse_active_cron_detectors

    crontab = tmp_path / "crontab.conf"
    crontab.write_text(
        "# cut в Phase 0\n"
        "# 8 */6 * * * docker exec backend python scripts/cron_runner.py d7b-scan\n"
        "30 * * * * docker exec backend python scripts/cron_runner.py d13-scan\n"
        "*/5 * * * * docker exec backend python scripts/cron_runner.py d26-scan\n"
        "0 3 * * 0 python scripts/cron_runner.py unknown-cron-not-in-mapping\n"
    )

    active = parse_active_cron_detectors(crontab)

    assert active == {"D13_retail_confidence_fade", "D26_fade_whale_extreme"}
    assert "D7b_no_favorite_fade" not in active  # commented


def test_parse_active_cron_detectors_missing_file_returns_all():
    """Если crontab.conf не найден → fail-open: вернуть весь маппинг."""
    from pathlib import Path

    from app.services.detector_metrics import _CRON_NAME_TO_DETECTOR, parse_active_cron_detectors

    active = parse_active_cron_detectors(Path("/nonexistent/crontab.conf"))

    assert active == set(_CRON_NAME_TO_DETECTOR.values())
