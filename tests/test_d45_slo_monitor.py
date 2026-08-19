"""Тесты для app/jobs/d45_slo_monitor.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs import d45_slo_monitor as slo

# ---------- _detect_issues (pure logic) ----------


def _full_ok_stats():
    return {
        "enabled": True,
        "pyth": {
            "connected": True,
            "symbols": {"BTC/USD": {"fresh": True, "price": 77000.0, "age_s": 0.3}},
        },
        "binance": {
            "connected": True,
            "symbols": {"BTC/USD": {"fresh": True, "price": 77050.0, "age_s": 0.5}},
        },
    }


def test_detect_issues_all_ok():
    assert slo._detect_issues(_full_ok_stats()) == []


def test_detect_issues_pyth_disconnected():
    stats = _full_ok_stats()
    stats["pyth"]["connected"] = False
    issues = slo._detect_issues(stats)
    assert any("Pyth SSE disconnected" in i for i in issues)


def test_detect_issues_binance_disconnected():
    stats = _full_ok_stats()
    stats["binance"]["connected"] = False
    issues = slo._detect_issues(stats)
    assert any("Binance WS disconnected" in i for i in issues)


def test_detect_issues_pyth_btc_stale_above_threshold():
    stats = _full_ok_stats()
    stats["pyth"]["symbols"]["BTC/USD"]["age_s"] = 600.0  # > 300s
    issues = slo._detect_issues(stats)
    assert any("Pyth BTC/USD" in i and "600" in i for i in issues)


def test_detect_issues_pyth_btc_not_fresh_at_all():
    stats = _full_ok_stats()
    stats["pyth"]["symbols"]["BTC/USD"]["fresh"] = False
    issues = slo._detect_issues(stats)
    assert any("Pyth BTC/USD" in i and "ticks" in i for i in issues)


def test_detect_issues_disabled_returns_empty():
    stats = _full_ok_stats()
    stats["enabled"] = False
    assert slo._detect_issues(stats) == []


# ---------- run_d45_slo_check (integration) ----------


@pytest.mark.asyncio
async def test_run_skipped_when_feature_flag_disabled():
    fake_settings = type("S", (), {"d45_chainlink_leadlag_enabled": False})()
    with patch("app.jobs.d45_slo_monitor.get_settings", return_value=fake_settings):
        result = await slo.run_d45_slo_check()
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_run_ok_when_stats_clean():
    fake_settings = type(
        "S",
        (),
        {"d45_chainlink_leadlag_enabled": True, "admin_api_key": "k", "admin_telegram_ids": []},
    )()
    with (
        patch("app.jobs.d45_slo_monitor.get_settings", return_value=fake_settings),
        patch(
            "app.jobs.d45_slo_monitor._fetch_stats",
            AsyncMock(return_value=_full_ok_stats()),
        ),
    ):
        result = await slo.run_d45_slo_check()
    assert result["status"] == "ok"
    assert result["issues"] == []


@pytest.mark.asyncio
async def test_run_alert_when_pyth_disconnected_sends_tg():
    stats = _full_ok_stats()
    stats["pyth"]["connected"] = False
    fake_settings = type(
        "S",
        (),
        {
            "d45_chainlink_leadlag_enabled": True,
            "admin_api_key": "k",
            "admin_telegram_ids": [123],
        },
    )()

    publish_mock = AsyncMock()
    with (
        patch("app.jobs.d45_slo_monitor.get_settings", return_value=fake_settings),
        patch("app.jobs.d45_slo_monitor._fetch_stats", AsyncMock(return_value=stats)),
        patch("app.jobs.d45_slo_monitor.publish", publish_mock),
    ):
        result = await slo.run_d45_slo_check()

    assert result["status"] == "alert"
    assert any("Pyth SSE disconnected" in i for i in result["issues"])
    publish_mock.assert_awaited_once()
    n = publish_mock.await_args[0][0][0]
    assert publish_mock.await_args.kwargs["actor"] == "d45_slo_monitor"
    assert "Pyth SSE disconnected" in " ".join(n.what_happened)


@pytest.mark.asyncio
async def test_run_alert_when_stats_fetch_fails():
    """HTTP fail → alert через шину. _fetch_stats возвращает None."""
    fake_settings = type(
        "S",
        (),
        {
            "d45_chainlink_leadlag_enabled": True,
            "admin_api_key": "k",
            "admin_telegram_ids": [123],
        },
    )()
    publish_mock = AsyncMock()
    with (
        patch("app.jobs.d45_slo_monitor.get_settings", return_value=fake_settings),
        patch("app.jobs.d45_slo_monitor._fetch_stats", AsyncMock(return_value=None)),
        patch("app.jobs.d45_slo_monitor.publish", publish_mock),
    ):
        result = await slo.run_d45_slo_check()

    assert result["status"] == "alert"
    publish_mock.assert_awaited_once()
    # Недоступный backend — отдельный текст: «не знаем, жив ли поток» вместо
    # утверждения о самом потоке, о котором в этом случае ничего не известно.
    n = publish_mock.await_args[0][0][0]
    assert "не видно" in n.headline.lower()
