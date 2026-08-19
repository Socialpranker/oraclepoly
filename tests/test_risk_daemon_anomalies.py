"""check_anomalies per-detector attribution — Task 4 (audit-fixes phase1).

Раньше check_anomalies считал COUNT(*) без GROUP BY → шум одного детектора
глушил весь live-банк глобальным kill_switch. Теперь: одиночный нарушитель
изолируется через Redis-пауза (bot:daemon_paused_detectors), читаемую
execution_safety.check_daemon_pause — демон НЕ пишет в БД (архитектурный
контракт), поэтому пауза только через Redis-set.

Множественные нарушители или суммарный runaway (orders/min) — по-прежнему
глобальный kill (return False, ...) — паттерн опаснее одиночного шумного
детектора.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scripts.risk_daemon import check_anomalies


def _fake_settings(**overrides):
    defaults = {
        "risk_daemon_anomaly_orders_per_min": 10,
        "risk_daemon_anomaly_rejections_per_min": 5,
    }
    defaults.update(overrides)
    return type("S", (), defaults)()


def _mock_redis(*, is_member: bool = False):
    redis = AsyncMock()
    redis.sismember = AsyncMock(return_value=is_member)
    redis.sadd = AsyncMock(return_value=1)
    redis.set = AsyncMock(return_value=True)
    return redis


# --- (а) один нарушитель → изолируется через Redis-паузу, банк жив ---------


@pytest.mark.asyncio
async def test_single_offender_paused_via_redis_not_global_kill(monkeypatch):
    """D_noisy: 7 rejected/min (>5 threshold), D_quiet: 1 rejected/min.

    Итог: (True, "") — глобальный банк НЕ глушим. redis SADD вызван для
    D_noisy на bot:daemon_paused_detectors.
    """
    rows = [
        {"detector": "D_noisy", "recent": 7, "rejected_recent": 7},
        {"detector": "D_quiet", "recent": 1, "rejected_recent": 1},
    ]
    monkeypatch.setattr("scripts.risk_daemon.fetch", AsyncMock(return_value=rows))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    mock_redis = _mock_redis(is_member=False)
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr("scripts.risk_daemon._tg_send_with_button", AsyncMock())

    ok, reason = await check_anomalies()

    assert ok
    assert reason == ""
    mock_redis.sadd.assert_awaited_once_with("bot:daemon_paused_detectors", "D_noisy")


@pytest.mark.asyncio
async def test_single_offender_skips_sadd_if_already_paused(monkeypatch):
    """Идемпотентность: если детектор уже в паузе — повторный SADD/alert не шлём."""
    rows = [{"detector": "D_noisy", "recent": 7, "rejected_recent": 7}]
    monkeypatch.setattr("scripts.risk_daemon.fetch", AsyncMock(return_value=rows))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    mock_redis = _mock_redis(is_member=True)
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))
    mock_tg = AsyncMock()
    monkeypatch.setattr("scripts.risk_daemon._tg_send_with_button", mock_tg)

    ok, reason = await check_anomalies()

    assert ok
    assert reason == ""
    mock_redis.sadd.assert_not_awaited()
    mock_tg.assert_not_awaited()


# --- (б) два нарушителя → глобальный kill -----------------------------------


@pytest.mark.asyncio
async def test_multi_detector_offenders_global_kill(monkeypatch):
    """Два детектора одновременно > rejections threshold (при order-rate ниже
    threshold — high orders_per_min override) → (False, anomaly_multi_detector:...)."""
    rows = [
        {"detector": "D_a", "recent": 6, "rejected_recent": 6},
        {"detector": "D_b", "recent": 6, "rejected_recent": 6},
    ]
    monkeypatch.setattr("scripts.risk_daemon.fetch", AsyncMock(return_value=rows))
    monkeypatch.setattr(
        "scripts.risk_daemon.get_settings",
        lambda: _fake_settings(risk_daemon_anomaly_orders_per_min=100),
    )
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=_mock_redis()))

    ok, reason = await check_anomalies()

    assert not ok
    assert reason.startswith("anomaly_multi_detector:")
    assert "D_a" in reason
    assert "D_b" in reason


# --- (в) суммарный order-rate → глобальный kill ------------------------------


@pytest.mark.asyncio
async def test_total_order_rate_runaway_global_kill(monkeypatch):
    """Суммарный order-rate 15/min > 10 threshold → (False, anomaly_high_order_rate:15/min)."""
    rows = [
        {"detector": "D_a", "recent": 8, "rejected_recent": 1},
        {"detector": "D_b", "recent": 7, "rejected_recent": 1},
    ]
    monkeypatch.setattr("scripts.risk_daemon.fetch", AsyncMock(return_value=rows))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=_mock_redis()))

    ok, reason = await check_anomalies()

    assert not ok
    assert reason == "anomaly_high_order_rate:15/min"


# --- edge cases ---------------------------------------------------------


@pytest.mark.asyncio
async def test_no_rows_ok(monkeypatch):
    """Нет live-ордеров вообще → ok."""
    monkeypatch.setattr("scripts.risk_daemon.fetch", AsyncMock(return_value=[]))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    ok, reason = await check_anomalies()

    assert ok
    assert reason == ""


@pytest.mark.asyncio
async def test_no_offenders_ok(monkeypatch):
    """Все детекторы в норме → ok, без Redis-паузы."""
    rows = [
        {"detector": "D_a", "recent": 2, "rejected_recent": 1},
        {"detector": "D_b", "recent": 1, "rejected_recent": 0},
    ]
    monkeypatch.setattr("scripts.risk_daemon.fetch", AsyncMock(return_value=rows))
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())
    mock_redis = _mock_redis()
    monkeypatch.setattr("scripts.risk_daemon.get_redis", AsyncMock(return_value=mock_redis))

    ok, reason = await check_anomalies()

    assert ok
    assert reason == ""
    mock_redis.sadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_still_joins_live_detector_stage(monkeypatch):
    """Regression guard (D38 2026-06-29): SQL по-прежнему джойнит detector_state
    stage='live' и теперь GROUP BY eo.detector для per-detector атрибуции."""
    captured: dict[str, str] = {}

    async def _capture_fetch(sql, *args):
        captured["sql"] = sql
        return [{"detector": "D3", "recent": 1, "rejected_recent": 0}]

    monkeypatch.setattr("scripts.risk_daemon.fetch", _capture_fetch)
    monkeypatch.setattr("scripts.risk_daemon.get_settings", lambda: _fake_settings())

    await check_anomalies()

    sql = captured["sql"].lower()
    assert "detector_state" in sql
    assert "stage" in sql and "live" in sql
    assert "group by" in sql
