"""Тесты edge-triggered дедупа алертов proxy_health_check.

Жалоба 2026-08-01: «IPRoyal Proxy Monitor» слал одно и то же сообщение про
407 на КАЖДОМ тике джобы (раз в 5 мин = 288 раз в сутки), пока прокси был сломан.
Раньше cooldown был только у expiry-алерта; у 407/TCP/трафика дедупа не было вовсе.

Фикс: _alert_on_edge — сообщение уходит только при СМЕНЕ состояния условия
(ok→fail и fail→ok). Состояние хранится в Redis без TTL: протухание ключа при
всё ещё сломанном прокси выглядело бы как новое событие и вернуло бы спам.
"""

from __future__ import annotations

import pytest

from app.jobs import proxy_health_check
from app.jobs.proxy_health_check import _alert_on_edge

FAIL = "🔑 сломалось"
OK = "✅ починилось"


class FakeRedis:
    """Минимальный Redis: помнит значения и записанные TTL."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        self.ttls[key] = ex


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.core.cache.get_redis", _get_redis)
    return fake


async def _edge(failing: bool) -> str | None:
    """Только текст перехода — направление проверяется отдельными тестами ниже
    (`test_recovery_and_failure_use_different_dedup_keys` и соседние)."""
    result = await _alert_on_edge("auth", failing=failing, fail_msg=FAIL, ok_msg=OK)
    return None if result is None else result[0]


@pytest.mark.asyncio
async def test_first_failure_alerts(redis):
    """Первая поломка → алерт уходит."""
    assert await _edge(failing=True) == FAIL


@pytest.mark.asyncio
async def test_repeated_failure_is_silent(redis):
    """Ядро жалобы: прокси сломан 20 тиков подряд → ровно ОДНО сообщение."""
    sent = [await _edge(failing=True) for _ in range(20)]
    assert sent[0] == FAIL
    assert all(msg is None for msg in sent[1:]), "повторные тики обязаны молчать"


@pytest.mark.asyncio
async def test_recovery_alerts_once(redis):
    """Починка → одно recovery-сообщение, дальше тишина."""
    await _edge(failing=True)
    assert await _edge(failing=False) == OK
    assert await _edge(failing=False) is None
    assert await _edge(failing=False) is None


@pytest.mark.asyncio
async def test_healthy_from_the_start_is_silent(redis):
    """Прокси здоров с самого начала → никакого «✅» на пустом месте."""
    assert await _edge(failing=False) is None
    assert await _edge(failing=False) is None


@pytest.mark.asyncio
async def test_flapping_alerts_each_real_transition(redis):
    """Реальные переходы не глотаются: сломалось→починилось→сломалось = 3 сообщения."""
    assert await _edge(failing=True) == FAIL
    assert await _edge(failing=False) == OK
    assert await _edge(failing=True) == FAIL


@pytest.mark.asyncio
async def test_state_key_has_no_ttl(redis):
    """Ключ состояния без TTL — иначе протухание = мнимое новое событие = спам."""
    await _edge(failing=True)
    assert redis.ttls["proxy:alert_state:auth"] is None


@pytest.mark.asyncio
async def test_conditions_are_independent(redis):
    """Разные условия не затирают состояние друг друга."""
    assert await _alert_on_edge("auth", failing=True, fail_msg=FAIL, ok_msg=OK) == (FAIL, True)
    # tcp ещё не ломался — его собственная поломка обязана дать алерт
    assert await _alert_on_edge("tcp", failing=True, fail_msg=FAIL, ok_msg=OK) == (FAIL, True)
    # а повтор по auth — молчит
    assert await _alert_on_edge("auth", failing=True, fail_msg=FAIL, ok_msg=OK) is None


@pytest.mark.asyncio
async def test_bytes_state_from_redis_is_handled(redis):
    """Реальный redis-клиент может вернуть bytes — не должно читаться как новое состояние."""
    redis.store["proxy:alert_state:auth"] = b"fail"
    assert await _edge(failing=True) is None


@pytest.mark.asyncio
async def test_without_redis_failure_still_alerts(monkeypatch):
    """Redis недоступен → сигнал о поломке не теряем (дедуп деградирует, тишина — нет)."""

    async def _no_redis():
        return None

    monkeypatch.setattr("app.core.cache.get_redis", _no_redis)
    assert await _edge(failing=True) == FAIL


@pytest.mark.asyncio
async def test_without_redis_recovery_is_silent(monkeypatch):
    """Без состояния recovery слать нельзя — иначе «✅» на каждом здоровом тике."""

    async def _no_redis():
        return None

    monkeypatch.setattr("app.core.cache.get_redis", _no_redis)
    assert await _edge(failing=False) is None


@pytest.mark.asyncio
async def test_redis_read_error_does_not_swallow_failure(monkeypatch, redis):
    """Ошибка чтения Redis не должна глушить сообщение о поломке."""

    async def _boom(key: str):
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis, "get", _boom)
    assert await _edge(failing=True) == FAIL


@pytest.mark.asyncio
async def test_status_degraded_while_auth_broken_even_without_alert(redis, monkeypatch):
    """Статус = здоровье прокси, а не факт отправки сообщения.

    Прод-прогон 2026-08-01 07:08: 407 держится, дедуп молчит (alerts_sent=0) —
    и джоба отрапортовала status="ok" при сломанном прокси, потому что старое
    условие смотрело на `alerts`. До дедупа алерт летел на каждом тике и баг не
    проявлялся.
    """
    from app.jobs.proxy_health_check import run_proxy_health_check

    class _Settings:
        polymarket_proxy = "http://user:pass@proxy.example.com:8080"
        execution_enabled = False
        proxy_expires_at = ""
        iproyal_api_token = ""

    monkeypatch.setattr("app.core.config.get_settings", lambda: _Settings())
    # TCP жив, auth сломан — ровно прод-ситуация
    monkeypatch.setattr(proxy_health_check, "_tcp_check_cached", lambda _u: _async(True))
    monkeypatch.setattr(proxy_health_check, "_proxy_auth_check_cached", lambda _u: _async(False))
    monkeypatch.setattr(proxy_health_check, "_send_alerts", lambda _a: _async(None))

    first = await run_proxy_health_check()
    assert first["alerts_sent"] == 1, "первая поломка обязана дать алерт"
    assert first["status"] == "degraded"

    second = await run_proxy_health_check()
    assert second["alerts_sent"] == 0, "повтор молчит — это и есть дедуп"
    assert second["proxy_auth_ok"] is False
    assert second["status"] == "degraded", "молчание дедупа ≠ здоровый прокси"


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_module_defines_no_stale_cooldown():
    """Старый time-cooldown удалён — дедуп теперь по событию, не по таймеру."""
    assert not hasattr(proxy_health_check, "SESSION_ALERT_COOLDOWN")
    assert not hasattr(proxy_health_check, "_session_alert_cooldown_ok")


@pytest.mark.asyncio
async def test_recovery_and_failure_use_different_dedup_keys(redis):
    """Recovery обязан иметь ДРУГОЙ kind, иначе cooldown шины его подавит.

    Дедуп шины — 25 часов по ключу (subject, kind). Если поломка и восстановление
    делят один kind, то «прокси починился» в течение суток после «прокси сломался»
    будет молча подавлено: владелец узнает о поломке и никогда об устранении.
    Прямой рецидив риска №2 спеки §10, только по другой оси.
    """
    from app.jobs.proxy_health_check import to_notifications

    fail = to_notifications([("🔴 Прокси недоступен", True)])
    recovery = to_notifications([("✅ Прокси снова доступен", False)])

    assert fail[0].kind != recovery[0].kind
    assert fail[0].dedup_key() != recovery[0].dedup_key()


@pytest.mark.asyncio
async def test_recovery_is_not_action_required(redis):
    """У «починилось» нет следующего шага — по §4 это FYI, не ACTION_REQUIRED."""
    from app.analytics.notification import Severity
    from app.jobs.proxy_health_check import to_notifications

    recovery = to_notifications([("✅ Прокси снова доступен", False)])

    assert recovery[0].severity == Severity.FYI
    assert recovery[0].what_to_do is None


@pytest.mark.asyncio
async def test_mixed_failure_and_recovery_split_into_two_notifications(redis):
    """Один прогон может дать и поломку, и восстановление разных условий
    (TCP починился, auth сломался) — их нельзя склеивать в одно сообщение."""
    from app.analytics.notification import Severity
    from app.jobs.proxy_health_check import to_notifications

    ns = to_notifications([("✅ TCP снова доступен", False), ("🔑 407 на auth", True)])

    assert len(ns) == 2
    severities = {n.severity for n in ns}
    assert severities == {Severity.FYI, Severity.ACTION_REQUIRED}
