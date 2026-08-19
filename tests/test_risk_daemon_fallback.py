"""Risk daemon — exposure/daily-pnl fallback без самоделения (audit топ-6, Task 3).

get_bankroll_for_daemon() раньше при отсутствии peak_bank в Redis (рестарт/евикт)
падал на SUM(stake) live placed/filled — тот же числитель, что и у check_exposure,
т.е. self-divide → ложный ~100% exposure kill (#621 сменил только знаменатель,
источник остался тот же). Теперь get_bankroll_for_daemon() возвращает None, а
bankroll-зависимые чеки (check_exposure/check_daily_pnl_mtm) пропускают себя с
debounced (раз в час) warn вместо ложного kill.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scripts.risk_daemon import (
    check_daily_pnl_mtm,
    check_exposure,
    get_bankroll_for_daemon,
)


def _fake_settings(**overrides):
    defaults = {
        "risk_daemon_daily_loss_hard_kill_pct": 0.05,
        "risk_daemon_exposure_hard_cap_pct": 0.60,
    }
    defaults.update(overrides)
    return type("S", (), defaults)()


@pytest.mark.asyncio
async def test_bankroll_none_when_peak_bank_missing(monkeypatch):
    """peak_bank отсутствует в Redis → None, без proxy-fallback на SUM(stake)."""
    monkeypatch.setattr("scripts.risk_daemon.get_peak_bank", AsyncMock(return_value=None))
    fetchval_mock = AsyncMock(return_value=999.0)
    monkeypatch.setattr("scripts.risk_daemon.fetchval", fetchval_mock)

    bankroll = await get_bankroll_for_daemon()

    assert bankroll is None
    fetchval_mock.assert_not_called()


@pytest.mark.asyncio
async def test_check_exposure_skips_when_bankroll_none(monkeypatch):
    """bankroll=None → check_exposure возвращает (True, ''), НЕ kill."""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)  # NX EX захвачен → warn отправлен
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("scripts.risk_daemon._tg_send", AsyncMock())
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())
    fetchval_mock = AsyncMock(return_value=100.0)
    monkeypatch.setattr("scripts.risk_daemon.fetchval", fetchval_mock)

    ok, reason = await check_exposure(None)

    assert ok
    assert reason == ""
    fetchval_mock.assert_not_called()  # без знаменателя SQL за числителем даже не идёт


@pytest.mark.asyncio
async def test_check_exposure_warns_once_per_hour(monkeypatch):
    """Debounce: SET NX EX 3600 на bot:risk:warn:exposure_no_denominator."""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))
    tg_send_mock = AsyncMock()
    monkeypatch.setattr("scripts.risk_daemon._tg_send", tg_send_mock)
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    await check_exposure(None)

    mock_redis.set.assert_awaited_once_with(
        "bot:risk:warn:exposure_no_denominator", "1", nx=True, ex=3600
    )
    tg_send_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_exposure_no_warn_spam_within_hour(monkeypatch):
    """Второй вызов в течение часа: SET NX не захватывает ключ → TG не шлётся повторно."""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=False)  # уже установлен другим циклом
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))
    tg_send_mock = AsyncMock()
    monkeypatch.setattr("scripts.risk_daemon._tg_send", tg_send_mock)
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, reason = await check_exposure(None)

    assert ok
    assert reason == ""
    tg_send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_daily_pnl_mtm_skips_when_bankroll_none(monkeypatch):
    """bankroll=None → daily_pnl-чек тоже пропускается (тот же класс риска, что exposure)."""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("scripts.risk_daemon._tg_send", AsyncMock())
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())
    fetch_mock = AsyncMock(return_value=[{"realized": -9999.0, "mtm": 0.0}])
    monkeypatch.setattr("scripts.risk_daemon.fetch", fetch_mock)

    ok, reason = await check_daily_pnl_mtm(None)

    assert ok
    assert reason == ""
    fetch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_check_exposure_still_kills_with_real_bankroll(monkeypatch):
    """Regression guard: реальный bankroll (не None) — self-divide-фикс #621 не сломан."""
    monkeypatch.setattr("scripts.risk_daemon.fetchval", AsyncMock(return_value=650.0))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, reason = await check_exposure(1000.0)

    assert not ok
    assert "exposure_hard_cap" in reason
