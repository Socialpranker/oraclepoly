"""Тесты redeem_job — on-chain redeem (claim выигрыша) для resolved live-позиций.

v2_execution_resolve.settle_filled() только пишет pnl в БД (как если бы shares
выкупились по $1) — реального redeemPositions вызова там нет. redeem_job закрывает
разрыв через poly-web3 SDK (ProxyWeb3Service.redeem_all(), Polymarket Relayer).

REDEEM_ENABLED=false (default) → dry-run: логируем/алертим кандидатов, ничего не
отправляем. REDEEM_ENABLED=true → боевой вызов svc.redeem_all(), маппинг
RedeemResult.success_list/error_list обратно на execution_orders по condition_id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.jobs import redeem_job as rj


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


CANDIDATE_ROW = {
    "id": "order-1",
    "market_id": "0xmarket1",
    "stake": 100.0,
    "outcome": "WIN",
    "condition_id": "0xcondition1",
    "net_pnl": 42.5,
}


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _mock_get_redis(monkeypatch, fake_redis=None):
    fake = fake_redis or _FakeRedis()

    async def _get():
        return fake

    monkeypatch.setattr("app.core.cache.get_redis", _get)
    return fake


def _fetch_with_zero_loss(candidates_fetch):
    async def mock_fetch(sql, *args):
        if sql is rj._FETCH_LOSS_SUM_SQL:
            return [{"loss_sum": 0}]
        return await candidates_fetch(sql, *args)

    return mock_fetch


def test_candidates_sql_filters_real_execution_mode_values():
    """execution_orders не имеет execution_mode='live' — реальные live-ордера это
    'maker'/'taker' (virtual-позиции — 'virtual'). Регрессия: первая версия фильтровала
    по несуществующему 'live' и всегда возвращала 0 кандидатов на проде."""

    assert "execution_mode = 'live'" not in rj._FETCH_CANDIDATES_SQL
    assert "execution_mode IN ('maker', 'taker')" in rj._FETCH_CANDIDATES_SQL
    assert "execution_mode = 'live'" not in rj._MARK_REDEEMED_SQL
    assert "execution_mode = 'live'" not in rj._MARK_FAILED_SQL


@pytest.mark.asyncio
async def test_no_candidates_returns_zero(monkeypatch):
    async def mock_fetch(_sql):
        return []

    monkeypatch.setattr(rj, "fetch", mock_fetch)

    result = await rj.run_redeem()

    assert result == {"status": "ok", "candidates": 0}


@pytest.mark.asyncio
async def test_dry_run_when_redeem_disabled_does_not_call_sdk(monkeypatch):
    """REDEEM_ENABLED=false (default) → сообщаем кандидатов, SDK не вызываем."""

    async def mock_fetch(_sql):
        return [dict(CANDIDATE_ROW)]

    notify_calls: list[str] = []

    async def mock_send_admin_message(text):
        notify_calls.append(text)

    build_svc_called = []

    def mock_build_svc():
        build_svc_called.append(True)
        raise AssertionError("SDK не должен вызываться в dry-run")

    monkeypatch.setattr(rj, "fetch", mock_fetch)
    monkeypatch.setattr(rj, "_build_proxy_web3_service", mock_build_svc)
    monkeypatch.setattr(rj, "publish", _bus_mock(mock_send_admin_message))
    monkeypatch.setattr(rj.get_settings(), "redeem_enabled", False)

    result = await rj.run_redeem()

    assert result == {"status": "dry_run", "candidates": 1}
    assert build_svc_called == []
    assert len(notify_calls) == 1
    assert "1" in notify_calls[0]


@pytest.mark.asyncio
async def test_dry_run_throttled_when_candidate_count_unchanged(monkeypatch):
    """Второй прогон с тем же числом кандидатов не шлёт повторный алерт (throttle)."""

    async def mock_fetch(_sql):
        return [dict(CANDIDATE_ROW)]

    notify_calls: list[str] = []

    async def mock_send_admin_message(text):
        notify_calls.append(text)

    fake_redis = _mock_get_redis(monkeypatch)

    monkeypatch.setattr(rj, "fetch", mock_fetch)
    monkeypatch.setattr(rj, "_build_proxy_web3_service", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(rj, "publish", _bus_mock(mock_send_admin_message))
    monkeypatch.setattr(rj.get_settings(), "redeem_enabled", False)

    await rj.run_redeem()
    await rj.run_redeem()

    assert len(notify_calls) == 1
    assert fake_redis.store[rj._DRY_RUN_DEDUP_KEY] == "1"


@pytest.mark.asyncio
async def test_dry_run_notifies_again_when_candidate_count_grows(monkeypatch):
    """Рост числа кандидатов (эскалация риска) пробивает throttle."""

    call_count = 0

    async def mock_fetch(_sql):
        nonlocal call_count
        call_count += 1
        return [dict(CANDIDATE_ROW)] * call_count

    notify_calls: list[str] = []

    async def mock_send_admin_message(text):
        notify_calls.append(text)

    fake_redis = _mock_get_redis(monkeypatch)

    monkeypatch.setattr(rj, "fetch", mock_fetch)
    monkeypatch.setattr(rj, "_build_proxy_web3_service", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(rj, "publish", _bus_mock(mock_send_admin_message))
    monkeypatch.setattr(rj.get_settings(), "redeem_enabled", False)

    await rj.run_redeem()
    await rj.run_redeem()

    assert len(notify_calls) == 2
    assert fake_redis.store[rj._DRY_RUN_DEDUP_KEY] == "2"


@pytest.mark.asyncio
async def test_redeem_enabled_success_updates_db_and_notifies(monkeypatch):
    """REDEEM_ENABLED=true, redeem_all() успех → redeem_status='redeemed', TG-алерт.

    poly-web3 SDK группирует redeem по батчам condition_id (до batch_size за раз) и
    добавляет в success_list ОДИН результат транзакции НА ВЕСЬ БАТЧ — там нет
    per-position поля condition_id (регрессия: первая версия ошибочно считала
    success_list элементы dict'ами с ключом "conditionId", маппинг всегда давал 0
    обновлённых строк несмотря на реальный успешный on-chain redeem). Тут success_list
    элемент — строка tx hash, как в реальном SDK, а не выдуманный dict.
    """

    async def _candidates_fetch(_sql, *_args):
        return [dict(CANDIDATE_ROW)]

    mock_fetch = _fetch_with_zero_loss(_candidates_fetch)

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append(args)
        return "UPDATE 1"

    async def mock_debounce_acquire(_key, ttl):
        return True

    notify_calls: list[str] = []

    async def mock_send_admin_message(text):
        notify_calls.append(text)

    fake_result = SimpleNamespace(success_list=["0xabc123"], error_list=[])

    class _FakeSvc:
        def redeem_all(self, batch_size=10):
            return fake_result

    _mock_get_redis(monkeypatch)
    monkeypatch.setattr(rj, "fetch", mock_fetch)
    monkeypatch.setattr(rj, "execute", mock_execute)
    monkeypatch.setattr(rj, "_build_proxy_web3_service", lambda: _FakeSvc())
    monkeypatch.setattr(rj, "debounce_acquire", mock_debounce_acquire)
    monkeypatch.setattr(rj, "publish", _bus_mock(mock_send_admin_message))
    monkeypatch.setattr(rj.get_settings(), "redeem_enabled", True)

    result = await rj.run_redeem()

    assert result == {"status": "ok", "redeemed": 1, "errors": 0}
    assert len(captured_updates) == 1
    condition_ids_arg = captured_updates[0][0]
    assert condition_ids_arg == ["0xcondition1"]
    assert len(notify_calls) == 1


@pytest.mark.asyncio
async def test_redeem_enabled_partial_error_excludes_failed_from_redeemed(monkeypatch):
    """Один кандидат в error_list, другой нет → redeemed только тот, что не failed."""

    other_candidate = {**CANDIDATE_ROW, "id": "order-2", "condition_id": "0xcondition2"}

    async def _candidates_fetch(_sql, *_args):
        return [dict(CANDIDATE_ROW), dict(other_candidate)]

    mock_fetch = _fetch_with_zero_loss(_candidates_fetch)

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append(args)
        return "UPDATE 1"

    async def mock_debounce_acquire(_key, ttl):
        return True

    async def mock_send_admin_message(_text):
        pass

    fake_error = SimpleNamespace(condition_id="0xcondition2", market_slug="some-market", error="insufficient balance")
    fake_result = SimpleNamespace(success_list=["0xabc123"], error_list=[fake_error])

    class _FakeSvc:
        def redeem_all(self, batch_size=10):
            return fake_result

    _mock_get_redis(monkeypatch)
    monkeypatch.setattr(rj, "fetch", mock_fetch)
    monkeypatch.setattr(rj, "execute", mock_execute)
    monkeypatch.setattr(rj, "_build_proxy_web3_service", lambda: _FakeSvc())
    monkeypatch.setattr(rj, "debounce_acquire", mock_debounce_acquire)
    monkeypatch.setattr(rj, "publish", _bus_mock(mock_send_admin_message))
    monkeypatch.setattr(rj.get_settings(), "redeem_enabled", True)

    result = await rj.run_redeem()

    assert result == {"status": "ok", "redeemed": 1, "errors": 1}
    redeemed_call = next(args for args in captured_updates if args[0] == ["0xcondition1"])
    assert redeemed_call == (["0xcondition1"],)
    failed_call = next(args for args in captured_updates if args[0] == "0xcondition2")
    assert "insufficient balance" in failed_call[1]


@pytest.mark.asyncio
async def test_redeem_enabled_error_marks_failed(monkeypatch):
    """redeem_all() возвращает error_list → redeem_status='failed', redeem_error записан."""

    async def _candidates_fetch(_sql, *_args):
        return [dict(CANDIDATE_ROW)]

    mock_fetch = _fetch_with_zero_loss(_candidates_fetch)

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append(args)
        return "UPDATE 1"

    async def mock_debounce_acquire(_key, ttl):
        return True

    async def mock_send_admin_message(_text):
        pass

    fake_error = SimpleNamespace(condition_id="0xcondition1", market_slug="some-market", error="insufficient balance")
    fake_result = SimpleNamespace(success_list=[], error_list=[fake_error])

    class _FakeSvc:
        def redeem_all(self, batch_size=10):
            return fake_result

    _mock_get_redis(monkeypatch)
    monkeypatch.setattr(rj, "fetch", mock_fetch)
    monkeypatch.setattr(rj, "execute", mock_execute)
    monkeypatch.setattr(rj, "_build_proxy_web3_service", lambda: _FakeSvc())
    monkeypatch.setattr(rj, "debounce_acquire", mock_debounce_acquire)
    monkeypatch.setattr(rj, "publish", _bus_mock(mock_send_admin_message))
    monkeypatch.setattr(rj.get_settings(), "redeem_enabled", True)

    result = await rj.run_redeem()

    assert result == {"status": "ok", "redeemed": 0, "errors": 1}
    assert len(captured_updates) == 1
    condition_id_arg, error_arg = captured_updates[0]
    assert condition_id_arg == "0xcondition1"
    assert "insufficient balance" in error_arg


@pytest.mark.asyncio
async def test_redeem_skipped_when_debounce_locked(monkeypatch):
    """Предыдущий тик ещё не отпустил лок → пропускаем, SDK не вызываем."""

    async def mock_fetch(_sql):
        return [dict(CANDIDATE_ROW)]

    async def mock_debounce_acquire(_key, ttl):
        return False

    build_svc_called = []

    def mock_build_svc():
        build_svc_called.append(True)
        raise AssertionError("SDK не должен вызываться если лок занят")

    monkeypatch.setattr(rj, "fetch", mock_fetch)
    monkeypatch.setattr(rj, "_build_proxy_web3_service", mock_build_svc)
    monkeypatch.setattr(rj, "debounce_acquire", mock_debounce_acquire)
    monkeypatch.setattr(rj.get_settings(), "redeem_enabled", True)

    result = await rj.run_redeem()

    assert result == {"status": "skipped", "reason": "already_running_or_recent"}
    assert build_svc_called == []


@pytest.mark.asyncio
async def test_sdk_exception_reports_error_and_notifies_admin(monkeypatch):
    """_build_proxy_web3_service или redeem_all кидает исключение → status=error, TG-алерт."""

    async def mock_fetch(_sql):
        return [dict(CANDIDATE_ROW)]

    async def mock_debounce_acquire(_key, ttl):
        return True

    notify_calls: list[str] = []

    async def mock_send_admin_message(text):
        notify_calls.append(text)

    def mock_build_svc():
        raise RuntimeError("relayer unreachable")

    monkeypatch.setattr(rj, "fetch", mock_fetch)
    monkeypatch.setattr(rj, "_build_proxy_web3_service", mock_build_svc)
    monkeypatch.setattr(rj, "debounce_acquire", mock_debounce_acquire)
    monkeypatch.setattr(rj, "publish", _bus_mock(mock_send_admin_message))
    monkeypatch.setattr(rj.get_settings(), "redeem_enabled", True)

    result = await rj.run_redeem()

    assert result["status"] == "error"
    assert "relayer unreachable" in result["error"]
    assert len(notify_calls) == 1


@pytest.mark.asyncio
async def test_get_watermark_falls_back_to_one_hour_ago_when_missing(monkeypatch):
    class _FakeRedis:
        async def get(self, _key):
            return None

    async def mock_get_redis():
        return _FakeRedis()

    monkeypatch.setattr("app.core.cache.get_redis", mock_get_redis)

    before = datetime.now(UTC) - timedelta(hours=1, seconds=5)
    result = await rj._get_watermark()
    after = datetime.now(UTC) - timedelta(hours=1) + timedelta(seconds=5)

    assert before <= result <= after


@pytest.mark.asyncio
async def test_get_watermark_reads_stored_value(monkeypatch):
    stored = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

    class _FakeRedis:
        async def get(self, _key):
            return stored.isoformat()

    async def mock_get_redis():
        return _FakeRedis()

    monkeypatch.setattr("app.core.cache.get_redis", mock_get_redis)

    result = await rj._get_watermark()

    assert result == stored


@pytest.mark.asyncio
async def test_notify_result_computes_three_sums(monkeypatch):
    redeemed = [{**CANDIDATE_ROW, "net_pnl": 42.5}]
    fake_result = SimpleNamespace(success_list=["0xabc"], error_list=[])

    async def mock_fetch(sql, *args):
        assert sql == rj._FETCH_LOSS_SUM_SQL
        return [{"loss_sum": -18.2}]

    notify_calls: list[str] = []

    async def mock_send_admin_message(text):
        notify_calls.append(text)

    fake_redis = _mock_get_redis(monkeypatch)

    monkeypatch.setattr(rj, "fetch", mock_fetch)
    monkeypatch.setattr(rj, "publish", _bus_mock(mock_send_admin_message))

    await rj._notify_result(fake_result, redeemed)

    assert len(notify_calls) == 1
    msg = notify_calls[0]
    assert "+42.50$" in msg
    assert "-18.20$" in msg
    assert "+24.30$" in msg
    assert rj._WATERMARK_KEY in fake_redis.store


@pytest.mark.asyncio
async def test_notify_result_silent_when_no_activity(monkeypatch):
    fake_result = SimpleNamespace(success_list=[], error_list=[])

    async def mock_fetch(sql, *args):
        return [{"loss_sum": 0}]

    notify_calls: list[str] = []

    async def mock_send_admin_message(text):
        notify_calls.append(text)

    fake_redis = _mock_get_redis(monkeypatch)

    monkeypatch.setattr(rj, "fetch", mock_fetch)
    monkeypatch.setattr(rj, "publish", _bus_mock(mock_send_admin_message))

    await rj._notify_result(fake_result, [])

    assert notify_calls == []
    assert rj._WATERMARK_KEY not in fake_redis.store


def test_failed_batches_escalate_severity_and_name_the_cost():
    """Часть батчей упала → ACTION_REQUIRED + названа цена бездействия.

    Успешное погашение — FYI: деньги уже на кошельке, делать нечего. А вот
    непогашенные батчи это застрявшие деньги, и про них надо сказать.
    """
    from app.analytics.notification import Severity

    ok = rj.result_notification(success_count=3, error_count=0, redeemed_pnl=12.5, loss_sum=-2.0)
    assert ok.severity is Severity.FYI
    assert ok.what_if_ignored is None

    failed = rj.result_notification(success_count=1, error_count=2, redeemed_pnl=4.0, loss_sum=0.0)
    assert failed.severity is Severity.ACTION_REQUIRED
    assert failed.what_if_ignored is not None
    assert "2" in failed.what_if_ignored
    assert failed.kind != ok.kind


def test_dry_run_names_frozen_money_only_when_it_exists():
    """Сумма замороженных денег — только когда она реально есть (§3)."""
    with_money = rj.dry_run_notification([{"net_pnl": 10.0}, {"net_pnl": 5.5}])
    assert with_money.what_if_ignored is not None
    assert "15.50" in with_money.what_if_ignored

    no_money = rj.dry_run_notification([{"net_pnl": 0.0}])
    assert no_money.what_if_ignored is None
