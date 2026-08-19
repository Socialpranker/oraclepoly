"""Risk daemon checks — Phase 1 Stage 5.

Тестируем pure-функции (без main_loop):
  - check_main_alive: heartbeat fresh/stale/missing.
  - check_exposure: >60% bank deployed.
  - check_daily_pnl_mtm: realized + mtm < -5% bankroll.
  - check_recon_strikes: read bot:reconciliation_strikes из Redis.
  - check_anomalies: orders/rejections per min spike.

DB и Redis замоканы. Тестируем contract pure logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from scripts.risk_daemon import (
    check_daily_pnl_mtm,
    check_exposure,
    check_main_alive,
    check_recon_strikes,
    get_bankroll_for_daemon,
)


def _fake_settings(**overrides):
    """Builder for Settings stub."""
    defaults = {
        "risk_daemon_heartbeat_interval_sec": 30,
        "risk_daemon_check_interval_sec": 30,
        "risk_daemon_main_stale_threshold_sec": 300,
        "risk_daemon_daily_loss_hard_kill_pct": 0.05,
        "risk_daemon_exposure_hard_cap_pct": 0.60,
        "risk_daemon_anomaly_orders_per_min": 10,
        "risk_daemon_anomaly_rejections_per_min": 5,
        "risk_daemon_skip_when_execution_disabled": True,
        "execution_enabled": True,
        "reconciliation_strike_threshold": 3,
    }
    defaults.update(overrides)
    return type("S", (), defaults)()


# --- check_main_alive ----------------------------------------------------


@pytest.mark.asyncio
async def test_main_alive_when_heartbeat_fresh(monkeypatch):
    """heartbeat 10s назад → main alive."""
    mock_redis = AsyncMock()
    fresh = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    mock_redis.get = AsyncMock(return_value=fresh)
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, reason = await check_main_alive()
    assert ok
    assert reason == ""


@pytest.mark.asyncio
async def test_main_dead_when_heartbeat_stale_6min(monkeypatch):
    """heartbeat 6 минут назад > 300s threshold → main dead."""
    mock_redis = AsyncMock()
    stale = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    mock_redis.get = AsyncMock(return_value=stale)
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, reason = await check_main_alive()
    assert not ok
    assert "stale" in reason


@pytest.mark.asyncio
async def test_main_alive_returns_ok_when_redis_down(monkeypatch):
    """Redis down → не блокируем (без heartbeat ничего не знаем)."""
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, _ = await check_main_alive()
    assert ok


@pytest.mark.asyncio
async def test_main_alive_missing_heartbeat_treats_as_dead(monkeypatch):
    """Heartbeat key отсутствует → main_heartbeat_missing."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, reason = await check_main_alive()
    assert not ok
    assert "missing" in reason


# --- check_exposure -------------------------------------------------------


@pytest.mark.asyncio
async def test_exposure_kill_when_60pct(monkeypatch):
    """650/1000 = 65% > 60% threshold → kill."""
    monkeypatch.setattr("scripts.risk_daemon.fetchval", AsyncMock(return_value=650.0))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, reason = await check_exposure(bankroll_usd=1000.0)
    assert not ok
    assert "exposure_hard_cap" in reason


@pytest.mark.asyncio
async def test_exposure_ok_when_30pct(monkeypatch):
    """300/1000 = 30% < 60% → ok."""
    monkeypatch.setattr("scripts.risk_daemon.fetchval", AsyncMock(return_value=300.0))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, _ = await check_exposure(bankroll_usd=1000.0)
    assert ok


@pytest.mark.asyncio
async def test_exposure_handles_zero_bankroll(monkeypatch):
    """bankroll=0 → divide by zero защита. max(bank, 1) → exposure = open_value."""
    monkeypatch.setattr("scripts.risk_daemon.fetchval", AsyncMock(return_value=100.0))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, reason = await check_exposure(bankroll_usd=0.0)
    # 100/max(0,1) = 100 >> 60% → kill, не crash
    assert not ok
    assert "exposure_hard_cap" in reason


# --- get_bankroll_for_daemon ---------------------------------------------


@pytest.mark.asyncio
async def test_bankroll_uses_peak_bank_from_redis(monkeypatch):
    """Primary source: реальный банк из bot:risk:peak_bank_usd, НЕ SUM(stake)."""
    monkeypatch.setattr("scripts.risk_daemon.get_peak_bank", AsyncMock(return_value=5000.0))
    # fetchval не должен использоваться, если peak есть — но мокаем на ловушку.
    monkeypatch.setattr("scripts.risk_daemon.fetchval", AsyncMock(return_value=5.47))

    bankroll = await get_bankroll_for_daemon()
    assert bankroll == 5000.0


@pytest.mark.asyncio
async def test_bankroll_returns_none_when_no_peak(monkeypatch):
    """Redis down / key пуст → None, БЕЗ SUM(stake)-proxy fallback.

    Раньше здесь был fallback на SUM(stake) — тот же числитель, что и у
    check_exposure → самоделение (см. tests/test_risk_daemon_fallback.py).
    fetchval НЕ должен вызываться вовсе — мокаем на ловушку.
    """
    monkeypatch.setattr("scripts.risk_daemon.get_peak_bank", AsyncMock(return_value=None))
    monkeypatch.setattr("scripts.risk_daemon.fetchval", AsyncMock(return_value=5.47))

    bankroll = await get_bankroll_for_daemon()
    assert bankroll is None


@pytest.mark.asyncio
async def test_exposure_not_self_divide_with_single_fill(monkeypatch):
    """Regression: 1 живой fill $5.47 при банке $5000 → exposure ~0.17%, НЕ 100%.

    Баг 2026-06-28: знаменатель брал SUM(stake) тех же live-ордеров, что и
    числитель → 5.47/5.47 = 100% > 60% → ложный kill при первой live-ставке.
    """
    # Реальный банк из Redis.
    monkeypatch.setattr("scripts.risk_daemon.get_peak_bank", AsyncMock(return_value=5000.0))
    # Числитель check_exposure: открытый stake $5.47.
    monkeypatch.setattr("scripts.risk_daemon.fetchval", AsyncMock(return_value=5.47))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    bankroll = await get_bankroll_for_daemon()
    ok, reason = await check_exposure(bankroll_usd=bankroll)
    assert ok, f"exposure ложно сработал: {reason}"


# --- check_recon_strikes -------------------------------------------------


@pytest.mark.asyncio
async def test_recon_strikes_3_kills(monkeypatch):
    """recon_strikes=3 ≥ threshold → kill."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value="3")
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, reason = await check_recon_strikes()
    assert not ok
    assert "recon_strikes:3" in reason


@pytest.mark.asyncio
async def test_recon_strikes_below_threshold_ok(monkeypatch):
    """recon_strikes=2 < 3 → ok."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value="2")
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, _ = await check_recon_strikes()
    assert ok


@pytest.mark.asyncio
async def test_recon_strikes_no_key_ok(monkeypatch):
    """Key отсутствует (clean state) → ok."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, _ = await check_recon_strikes()
    assert ok


# --- check_anomalies -------------------------------------------------------
# Per-detector атрибуция (GROUP BY eo.detector) + Redis-пауза одиночного
# нарушителя — см. tests/test_risk_daemon_anomalies.py (Task 4, audit-fixes
# phase1). Старые single-row тесты этого блока перенесены туда.


# --- check_daily_pnl_mtm -------------------------------------------------


@pytest.mark.asyncio
async def test_daily_pnl_hard_kill_when_below_5pct(monkeypatch):
    """realized=-40, mtm=-15, bankroll=1000, threshold=-50 → -55 < -50 → kill."""
    monkeypatch.setattr(
        "scripts.risk_daemon.fetch",
        AsyncMock(return_value=[{"realized": -40.0, "mtm": -15.0}]),
    )
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, reason = await check_daily_pnl_mtm(bankroll_usd=1000.0)
    assert not ok
    assert "daily_loss_hard_kill" in reason


@pytest.mark.asyncio
async def test_daily_pnl_ok_when_above_threshold(monkeypatch):
    """realized=-30, mtm=-10, threshold=-50 → -40 > -50 → ok."""
    monkeypatch.setattr(
        "scripts.risk_daemon.fetch",
        AsyncMock(return_value=[{"realized": -30.0, "mtm": -10.0}]),
    )
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, _ = await check_daily_pnl_mtm(bankroll_usd=1000.0)
    assert ok


@pytest.mark.asyncio
async def test_daily_pnl_mtm_winning_offsets_realized(monkeypatch):
    """realized=-60 (под threshold), mtm=+30 → total=-30 > threshold → ok."""
    monkeypatch.setattr(
        "scripts.risk_daemon.fetch",
        AsyncMock(return_value=[{"realized": -60.0, "mtm": 30.0}]),
    )
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, _ = await check_daily_pnl_mtm(bankroll_usd=1000.0)
    assert ok
