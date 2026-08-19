"""Drift strike pattern for execution_reconcile — Phase 1 Stage 4.

Тестируем чистые helper-функции:
  - _handle_drift_strike: increment counter, при threshold → kill switch.
  - _reset_drift_strikes: clean exit.
  - _drift_detected: classify drift signal (orphans count > 0 или wallet error).

Полная reconciliation pipeline уже tested через existing test_reconciliation*.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.jobs import execution_reconcile as er


def _bus_mock(text_sink):
    """Мок шины, отдающий тесту РЕНДЕР уведомления, а не транспортную строку.

    Джобы больше не зовут send_admin_message — они отдают структуру в publish().
    Проверять смысл сообщения всё ещё надо, поэтому склеиваем поля в текст:
    assert'ы по подстрокам остаются валидными и продолжают ловить регрессии.
    """

    async def _publish(notifications, actor=None):
        for n in notifications:
            blob = " ".join(
                [n.headline, *n.what_happened, *(n.evidence or []), n.what_to_do or "", n.what_if_ignored or ""]
            )
            await text_sink(blob)
        return f"PublishResult(sent={len(notifications)})"

    return _publish


from app.jobs.execution_reconcile import (
    RECONCILIATION_STRIKE_THRESHOLD,
    _drift_detected,
    _handle_drift_strike,
    _reset_drift_strikes,
)


@pytest.mark.asyncio
async def test_reset_strikes_deletes_redis_key(monkeypatch):
    """Drift в норме → delete counter, начнём заново."""
    mock_redis = AsyncMock()
    monkeypatch.setattr("app.jobs.execution_reconcile.get_redis", AsyncMock(return_value=mock_redis))
    await _reset_drift_strikes()
    mock_redis.delete.assert_called_once_with("bot:reconciliation_strikes")


@pytest.mark.asyncio
async def test_reset_strikes_redis_down_silent(monkeypatch):
    """Redis недоступен → silent no-op."""
    monkeypatch.setattr("app.jobs.execution_reconcile.get_redis", AsyncMock(return_value=None))
    # Не должно крашить
    await _reset_drift_strikes()


@pytest.mark.asyncio
async def test_first_strike_warns_no_kill(monkeypatch):
    """1st strike: increment counter, TG warn, kill_switch не set."""
    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=1)
    monkeypatch.setattr("app.jobs.execution_reconcile.get_redis", AsyncMock(return_value=mock_redis))
    mock_notify = AsyncMock()
    monkeypatch.setattr("app.jobs.execution_reconcile._notify_drift_warning", mock_notify)

    stats: dict = {}
    await _handle_drift_strike(reason="orphans=2", stats=stats)

    assert stats["strike_count"] == 1
    assert not stats.get("kill_switch_set", False)
    # set вызывается только для kill_switch, не вызывается на 1st strike
    mock_redis.set.assert_not_called()
    mock_notify.assert_called_once()


@pytest.mark.asyncio
async def test_third_strike_sets_kill(monkeypatch):
    """3rd strike → SET bot:kill_switch, TG kill alert."""
    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=RECONCILIATION_STRIKE_THRESHOLD)
    monkeypatch.setattr("app.jobs.execution_reconcile.get_redis", AsyncMock(return_value=mock_redis))
    mock_notify_kill = AsyncMock()
    monkeypatch.setattr("app.jobs.execution_reconcile._notify_kill_switch_triggered", mock_notify_kill)

    stats: dict = {}
    await _handle_drift_strike(reason="orphans=5", stats=stats)

    assert stats["kill_switch_set"] is True
    # SET был вызван для kill_switch (один из вызовов)
    set_calls = [c for c in mock_redis.set.call_args_list if c[0][0] == "bot:kill_switch"]
    assert len(set_calls) == 1
    assert "reconciliation_drift" in set_calls[0][0][1]
    assert "orphans=5" in set_calls[0][0][1]
    mock_notify_kill.assert_called_once()


@pytest.mark.asyncio
async def test_strike_redis_down_logs_no_crash(monkeypatch):
    """Redis down → не крашим, не set kill (нет где хранить counter)."""
    monkeypatch.setattr("app.jobs.execution_reconcile.get_redis", AsyncMock(return_value=None))
    stats: dict = {}
    await _handle_drift_strike(reason="orphans=3", stats=stats)
    assert not stats.get("kill_switch_set", False)


# --- _drift_detected classifier ---


def test_drift_detected_orphans_positive():
    """orphans > 0 → drift."""
    detected, reason = _drift_detected(orphans=2, wallet_error=None)
    assert detected is True
    assert "orphans=2" in reason


def test_drift_detected_wallet_error():
    """wallet API error → drift (cannot verify state)."""
    detected, reason = _drift_detected(orphans=0, wallet_error="401 Unauthorized")
    assert detected is True
    assert "wallet_error" in reason


def test_drift_detected_clean_state():
    """0 orphans + wallet OK → no drift."""
    detected, _ = _drift_detected(orphans=0, wallet_error=None)
    assert detected is False


def test_drift_detected_trades_error():
    """trades feed недоступен → drift (cannot detect orphans)."""
    detected, reason = _drift_detected(orphans=0, wallet_error=None, trades_error="401 Unauthorized")
    assert detected is True
    assert "trades_error" in reason


def test_drift_detected_orphans_priority_over_errors():
    """orphans>0 → reason='orphans=N' даже если есть errors (главный signal)."""
    detected, reason = _drift_detected(orphans=2, wallet_error="x", trades_error="y")
    assert detected is True
    assert "orphans=2" in reason
    assert "trades_error" not in reason


# --- run_reconciliation skip when execution disabled ---


def _fake_settings(**overrides):
    """Builder для settings stub с разумными defaults."""
    defaults = {
        "execution_enabled": False,
        "reconciliation_skip_when_execution_disabled": True,
        "reconciliation_strike_threshold": 3,
        "reconciliation_strike_ttl_sec": 300,
    }
    defaults.update(overrides)
    return type("S", (), defaults)()


@pytest.mark.asyncio
async def test_run_reconciliation_skips_when_execution_disabled(monkeypatch):
    """EXECUTION_ENABLED=false + skip_when_disabled=True → skip целиком."""
    from app.jobs.execution_reconcile import run_reconciliation

    monkeypatch.setattr("app.jobs.execution_reconcile.get_settings", lambda: _fake_settings())

    def _fail(*args, **kwargs):
        raise AssertionError("build_clob_client должен быть skipped")

    monkeypatch.setattr("app.services.wallet_resolver.build_clob_client", _fail)

    result = await run_reconciliation()
    assert result.get("skipped") == "execution_disabled"
    assert result["polymarket_trades"] == 0


# --- Phase 1 Stage 4 gap closure: config-driven constants ---


@pytest.mark.asyncio
async def test_handle_drift_uses_threshold_from_settings(monkeypatch):
    """Settings.reconciliation_strike_threshold=2 → kill на 2-м strike, не 3."""
    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=2)
    monkeypatch.setattr("app.jobs.execution_reconcile.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(
        "app.jobs.execution_reconcile.get_settings",
        lambda: _fake_settings(reconciliation_strike_threshold=2),
    )
    monkeypatch.setattr("app.jobs.execution_reconcile._notify_kill_switch_triggered", AsyncMock())

    stats: dict = {}
    await _handle_drift_strike(reason="test", stats=stats)

    # На 2-м strike с threshold=2 → kill switch SET
    assert stats["kill_switch_set"] is True


@pytest.mark.asyncio
async def test_handle_drift_uses_ttl_from_settings(monkeypatch):
    """Settings.reconciliation_strike_ttl_sec → expire() вызван с этим значением."""
    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=1)
    monkeypatch.setattr("app.jobs.execution_reconcile.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(
        "app.jobs.execution_reconcile.get_settings",
        lambda: _fake_settings(reconciliation_strike_ttl_sec=120),
    )
    monkeypatch.setattr("app.jobs.execution_reconcile._notify_drift_warning", AsyncMock())

    await _handle_drift_strike(reason="test", stats={})

    # expire вызван с TTL=120 из settings, не hardcoded 300
    mock_redis.expire.assert_called_once_with("bot:reconciliation_strikes", 120)


@pytest.mark.asyncio
async def test_run_reconciliation_skip_flag_can_be_disabled(monkeypatch):
    """reconciliation_skip_when_execution_disabled=False → run даже с EXECUTION=false.
    Полезно для testing/staging без live wallet."""
    from app.jobs.execution_reconcile import run_reconciliation

    monkeypatch.setattr(
        "app.jobs.execution_reconcile.get_settings",
        lambda: _fake_settings(
            execution_enabled=False,
            reconciliation_skip_when_execution_disabled=False,
        ),
    )

    # build_clob_client should be вызван (skip отключен) → мокаем чтобы не упало на API
    mock_client = type("C", (), {"get_trades": lambda self: []})()
    monkeypatch.setattr("app.services.wallet_resolver.build_clob_client", lambda: mock_client)
    monkeypatch.setattr("app.services.wallet_resolver.fetch_balance", AsyncMock(return_value=100.0))
    monkeypatch.setattr("app.services.wallet_resolver.get_signature_type", lambda: "EOA")
    # БД mocks
    monkeypatch.setattr("app.jobs.execution_reconcile.fetch", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.jobs.execution_reconcile.fetchval", AsyncMock(return_value=0))
    # Redis для drift reset (no orphans, no errors → clean)
    monkeypatch.setattr("app.jobs.execution_reconcile.get_redis", AsyncMock(return_value=None))

    result = await run_reconciliation()
    # Skip отключен → job actually ran
    assert "skipped" not in result
    assert result["polymarket_trades"] == 0


# --- Auto-clear integration (инцидент 2026-06-28: sticky kill_switch) ------


class _StreakRedis:
    """Stateful Redis-заглушка для streak/flag авто-сброса."""

    def __init__(self, initial=None):
        self.store = dict(initial or {})

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, 0)) + 1)
        return int(self.store[key])

    async def expire(self, key, ttl):
        pass

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


@pytest.mark.asyncio
async def test_auto_clear_removes_transient_drift_flag(monkeypatch):
    """N чистых циклов снимают reconciliation_drift: флаг."""
    from app.jobs.execution_reconcile import _maybe_auto_clear_kill_switch

    r = _StreakRedis({"bot:kill_switch": "reconciliation_drift:trades_error=x:strikes=3"})
    notify_mock = AsyncMock()
    monkeypatch.setattr("app.jobs.execution_reconcile.get_redis", AsyncMock(return_value=r))
    monkeypatch.setattr("app.jobs.execution_reconcile._notify_kill_switch_auto_cleared", notify_mock)
    # 5 чистых циклов (дефолтный порог)
    for _ in range(5):
        await _maybe_auto_clear_kill_switch()
    assert "bot:kill_switch" not in r.store
    notify_mock.assert_called_once_with(clean_cycles=5)


@pytest.mark.asyncio
async def test_auto_cleared_message_contains_actual_cycle_count(monkeypatch):
    """Регрессия: TG-сообщение должно содержать реальное число циклов, не букву 'N'."""
    from app.jobs.execution_reconcile import _notify_kill_switch_auto_cleared

    sent: list[str] = []

    async def mock_send_admin_message(text):
        sent.append(text)

    monkeypatch.setattr(er, "publish", _bus_mock(mock_send_admin_message))

    await _notify_kill_switch_auto_cleared(clean_cycles=5)

    assert len(sent) == 1
    # Проверяем подстановку РЕАЛЬНОГО числа, а не конкретную формулировку:
    # текст переписывался при миграции на шину, гарантия же осталась прежней.
    assert "5" in sent[0]
    assert "N чистых" not in sent[0]


@pytest.mark.asyncio
async def test_auto_clear_does_not_touch_daemon_flag(monkeypatch):
    """Reconcile-джоба НЕ снимает чужой daemon: флаг даже при чистых циклах."""
    from app.jobs.execution_reconcile import _maybe_auto_clear_kill_switch

    r = _StreakRedis({"bot:kill_switch": "daemon:main_alive:main_heartbeat_stale:age=400s"})
    monkeypatch.setattr("app.jobs.execution_reconcile.get_redis", AsyncMock(return_value=r))
    for _ in range(10):
        await _maybe_auto_clear_kill_switch()
    assert r.store["bot:kill_switch"] == "daemon:main_alive:main_heartbeat_stale:age=400s"


def test_three_incident_states_have_distinct_kinds():
    """warn / kill / recovery — три РАЗНЫХ kind.

    Общий kind означал бы, что cooldown шины подавляет «починилось» после
    «сломалось», и владелец остаётся с картиной «торговля стоит», хотя она
    давно идёт. Урок пачки B (PR #739/#750).
    """
    warn = er.drift_warning_notification(reason="orphans=2", strike=1, threshold=3)
    kill = er.kill_switch_notification(reason="orphans=2", strike=3)
    cleared = er.kill_switch_cleared_notification(clean_cycles=5)

    kinds = {warn.kind, kill.kind, cleared.kind}
    assert len(kinds) == 3, f"kind'ы обязаны различаться, получили: {kinds}"
    # Subject общий — это один и тот же инцидент, склейка по нему правильная.
    assert warn.subject == kill.subject == cleared.subject


def test_recovery_is_fyi_not_action_required():
    """Восстановление не требует действий → FYI.

    ACTION_REQUIRED на «всё починилось само» обесценивает сам уровень (§4).
    """
    from app.analytics.notification import Severity

    cleared = er.kill_switch_cleared_notification(clean_cycles=5)
    assert cleared.severity is Severity.FYI
    assert cleared.what_if_ignored is None
