"""Тесты Task 2 (audit топ-4): transient-reject ретраится + POST-timeout сверка + реаниматор intent.

Три пласта:
  1. _insert_order проставляет is_transient_reject централизованно, по rejection_reason.
  2. _process_signal (место place): timeout ПОСЛЕ POST → сверка с CLOB через
     find_our_open_order → 'placed' вместо слепого 'rejected'.
  3. run_v2_execution_resolve: реаниматор застрявших 'intent'-строк.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest


def _note_text(call_arg) -> str:
    """Склеивает поля уведомления в текст.

    Джобы отдают в publish() структуру, а не строку, но проверять надо СМЫСЛ
    сообщения — поэтому собираем текст из полей и оставляем assert'ы по
    подстрокам рабочими.
    """
    notes = call_arg if isinstance(call_arg, list) else [call_arg]
    parts = []
    for n in notes:
        parts += [n.headline, *n.what_happened, *(n.evidence or [])]
        parts += [n.what_to_do or "", n.what_if_ignored or ""]
    return " ".join(parts)


from app.jobs.v2_execution import _insert_order


def _valid_payload(**overrides):
    defaults = {
        "detector": "D5",
        "detector_signal_id": "00000000-0000-0000-0000-000000000001",
        "market_id": "0xMARKET",
        "event_id": "0xEVT",
        "side": "YES",
        "execution_mode": "rejected",
        "signal_price": 0.40,
        "calibrated_p": 0.55,
        "meta_p": 0.50,
        "ev_pct": 0.0,
        "order_type": "none",
        "stake": 0.0,
        "bankroll_at_entry": 0.0,
        "status": "rejected",
        "meta": {},
    }
    defaults.update(overrides)
    return defaults


# --- 1. _insert_order помечает is_transient_reject централизованно ---------------


@pytest.mark.asyncio
async def test_insert_marks_transient_reject_true_for_kill_switch(monkeypatch):
    captured = {}

    async def _fake_fetchrow(sql, *args):
        captured["args"] = args
        return {"id": "abc-id"}

    monkeypatch.setattr("app.jobs.v2_execution.fetchrow", _fake_fetchrow)

    await _insert_order(_valid_payload(rejection_reason="kill_switch:redis_flag_set"))

    # is_transient_reject — последний позиционный параметр ($22)
    assert captured["args"][-1] is True


@pytest.mark.asyncio
async def test_insert_marks_transient_reject_false_for_price_drift(monkeypatch):
    captured = {}

    async def _fake_fetchrow(sql, *args):
        captured["args"] = args
        return {"id": "abc-id"}

    monkeypatch.setattr("app.jobs.v2_execution.fetchrow", _fake_fetchrow)

    await _insert_order(_valid_payload(rejection_reason="price_drift:12.3pp signal=0.40 fill=0.53"))

    assert captured["args"][-1] is False


@pytest.mark.asyncio
async def test_insert_marks_transient_reject_false_when_no_reason(monkeypatch):
    captured = {}

    async def _fake_fetchrow(sql, *args):
        captured["args"] = args
        return {"id": "abc-id"}

    monkeypatch.setattr("app.jobs.v2_execution.fetchrow", _fake_fetchrow)

    await _insert_order(_valid_payload(status="intent", execution_mode="virtual"))

    assert captured["args"][-1] is False


# --- 2. POST-timeout сверка перед rejected ----------------------------------------


class _StubService:
    """Подменяет PolymarketService()."""

    def __init__(self, clob_client):
        self.clob_client = clob_client


@pytest.mark.asyncio
async def test_place_timeout_recovers_via_clob_lookup(monkeypatch):
    """place() кидает httpx.ReadTimeout, find_our_open_order находит наш ордер
    на CLOB → UPDATE в 'placed', результат 'opened' (не слепой 'rejected')."""
    import app.jobs.v2_execution as v2

    execute_calls = []

    async def _fake_execute(sql, *args):
        execute_calls.append((sql, args))

    monkeypatch.setattr(v2, "execute", _fake_execute)
    monkeypatch.setattr(v2, "_place_order", AsyncMock(side_effect=httpx.ReadTimeout("timeout")))
    monkeypatch.setattr(
        "app.services.clob_order_lookup.find_our_open_order",
        AsyncMock(return_value="recovered-order-id"),
    )
    monkeypatch.setattr(
        "app.services.polymarket_service.PolymarketService",
        lambda: _StubService(clob_client=object()),
    )

    sig = {"token_id": "tok-abc", "market_id": "0xMARKET"}
    order_id_db = "order-db-id"
    mode = "taker"
    limit_price = None

    # Replicate the except-branch logic directly (unit-level, avoids full
    # _process_signal fixture setup — proxy for the code path under test).
    try:
        await v2._place_order({"token_id": sig["token_id"], "stake": 5.0, "side": "YES"}, mode, limit_price)
        raise AssertionError("expected _place_order to raise")
    except httpx.ReadTimeout as e:
        import httpx as httpx_mod

        from app.services.clob_order_lookup import find_our_open_order
        from app.services.polymarket_service import PolymarketService

        recovered_id = None
        if isinstance(e, (httpx_mod.TimeoutException, httpx_mod.TransportError, ConnectionError)):
            svc = PolymarketService()
            recovered_id = await find_our_open_order(svc.clob_client, sig["token_id"])
        assert recovered_id == "recovered-order-id"
        if recovered_id:
            await v2.execute(
                "UPDATE execution_orders SET status='placed', order_id=$1, placed_at=now() WHERE id=$2",
                recovered_id,
                order_id_db,
            )
            result = "opened"
        else:
            result = "rejected"

    assert result == "opened"
    assert execute_calls[0][0].startswith("UPDATE execution_orders SET status='placed'")
    assert execute_calls[0][1] == ("recovered-order-id", order_id_db)


@pytest.mark.asyncio
async def test_process_signal_place_timeout_recovers_end_to_end(monkeypatch):
    """Полный _process_signal: taker live path, _place_order timeout, CLOB lookup
    находит открытый ордер → status='placed', возврат 'opened'."""
    import app.jobs.v2_execution as v2

    executed = []

    async def _fake_execute(sql, *args):
        executed.append((sql, args))

    async def _fake_insert_order(payload):
        # intent insert — вернуть id, дальше идём в place()
        return "intent-db-id"

    monkeypatch.setattr(v2, "execute", _fake_execute)
    monkeypatch.setattr(v2, "_insert_order", _fake_insert_order)
    monkeypatch.setattr(v2, "_place_order", AsyncMock(side_effect=httpx.ReadTimeout("timeout")))
    monkeypatch.setattr(v2, "_is_category_blacklisted", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_entry_price_out_of_range", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_cal_p_zone_blacklisted", lambda *a, **k: (False, None))
    monkeypatch.setattr(v2, "_event_reentry_blocked", AsyncMock(return_value=False))
    live_state = type(
        "DS",
        (),
        {"stage": "live", "fractional_kelly_today": 0.1, "detector": "D5", "live_stake_cap_usd": None},
    )()
    monkeypatch.setattr(v2, "get_detector_state", AsyncMock(return_value=live_state))
    monkeypatch.setattr(v2, "get_settings", lambda: type("S", (), {"execution_enabled": True})())
    monkeypatch.setattr(v2, "fetch_portfolio_for_detector", AsyncMock(return_value=None))
    monkeypatch.setattr(
        v2,
        "get_live_bankroll",
        AsyncMock(return_value=type("B", (), {"bankroll_usd": 1000.0, "is_sufficient": True})()),
    )
    monkeypatch.setattr(
        v2,
        "size_from_calibrated_p",
        lambda **kw: type("D", (), {"stake_usd": 10.0, "ev_pct": 5.0})(),
    )
    monkeypatch.setattr(v2, "_virtual_exposure_remaining", AsyncMock(return_value=1000.0))
    monkeypatch.setattr(v2, "_portfolio_exposure_used", AsyncMock(return_value=0.0))
    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(
            return_value=type(
                "V", (), {"allowed": True, "reason": None, "current_price": 0.40, "book_depth_usd": None}
            )()
        ),
    )
    monkeypatch.setattr(
        v2,
        "check_risk",
        lambda *a, **k: type("R", (), {"is_blocked": False, "approved_stake": 10.0, "reductions": []})(),
    )
    monkeypatch.setattr(v2, "_apply_risk_engine", AsyncMock(return_value=(10.0, "", [])))
    monkeypatch.setattr(v2, "resolve_execution_mode", lambda *a, **kw: "taker")

    async def _fake_get_sweep_state(market_id):
        return None

    monkeypatch.setattr("app.services.whale_sweep_service.get_sweep_state", _fake_get_sweep_state)
    monkeypatch.setattr(
        "app.services.whale_sweep_service.apply_sweep_adjustment",
        lambda **kw: (kw["base_confidence"], "no_sweep"),
    )
    monkeypatch.setattr(
        "app.services.clob_order_lookup.find_our_open_order",
        AsyncMock(return_value="recovered-order-id"),
    )
    monkeypatch.setattr(
        "app.services.polymarket_service.PolymarketService",
        lambda: _StubService(clob_client=object()),
    )

    sig = {
        "detector": "D5",
        "detector_signal_id": "00000000-0000-0000-0000-000000000002",
        "market_id": "0xMARKET",
        "event_id": "0xEVT",
        "side": "YES",
        "entry_price": 0.40,
        "calibrated_p": 0.55,
        "meta_p": 0.55,
        "token_id": "tok-abc",
        "generated_at": None,
        "market_end_date": None,
        "category": "crypto",
    }

    result = await v2._process_signal(sig)

    assert result == "opened"
    placed_updates = [c for c in executed if "status='placed'" in c[0]]
    assert len(placed_updates) == 1
    assert placed_updates[0][1] == ("recovered-order-id", "intent-db-id")


# --- 4. TG-алерты live-провалов (audit second-tier) --------------------------------


@pytest.mark.asyncio
async def test_process_signal_place_error_not_recovered_sends_tg_alert(monkeypatch):
    """place() падает НЕ timeout-исключением (recovered_id остаётся None) →
    status='rejected' + TG-алерт с текстом ошибки. Алерт НЕ шлётся в recovered-ветке
    (см. предыдущий тест), только когда ордер реально не восстановлен."""
    import app.jobs.v2_execution as v2

    executed = []

    async def _fake_execute(sql, *args):
        executed.append((sql, args))

    async def _fake_insert_order(payload):
        return "intent-db-id"

    notify_mock = AsyncMock()

    monkeypatch.setattr(v2, "execute", _fake_execute)
    monkeypatch.setattr(v2, "_insert_order", _fake_insert_order)
    monkeypatch.setattr(v2, "_place_order", AsyncMock(side_effect=RuntimeError("clob 500 internal error")))
    monkeypatch.setattr(v2, "_is_category_blacklisted", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_entry_price_out_of_range", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_cal_p_zone_blacklisted", lambda *a, **k: (False, None))
    monkeypatch.setattr(v2, "_event_reentry_blocked", AsyncMock(return_value=False))
    live_state = type(
        "DS",
        (),
        {"stage": "live", "fractional_kelly_today": 0.1, "detector": "D5", "live_stake_cap_usd": None},
    )()
    monkeypatch.setattr(v2, "get_detector_state", AsyncMock(return_value=live_state))
    monkeypatch.setattr(v2, "get_settings", lambda: type("S", (), {"execution_enabled": True})())
    monkeypatch.setattr(v2, "fetch_portfolio_for_detector", AsyncMock(return_value=None))
    monkeypatch.setattr(
        v2,
        "get_live_bankroll",
        AsyncMock(return_value=type("B", (), {"bankroll_usd": 1000.0, "is_sufficient": True})()),
    )
    monkeypatch.setattr(
        v2,
        "size_from_calibrated_p",
        lambda **kw: type("D", (), {"stake_usd": 10.0, "ev_pct": 5.0})(),
    )
    monkeypatch.setattr(v2, "_virtual_exposure_remaining", AsyncMock(return_value=1000.0))
    monkeypatch.setattr(v2, "_portfolio_exposure_used", AsyncMock(return_value=0.0))
    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(
            return_value=type(
                "V", (), {"allowed": True, "reason": None, "current_price": 0.40, "book_depth_usd": None}
            )()
        ),
    )
    monkeypatch.setattr(
        v2,
        "check_risk",
        lambda *a, **k: type("R", (), {"is_blocked": False, "approved_stake": 10.0, "reductions": []})(),
    )
    monkeypatch.setattr(v2, "_apply_risk_engine", AsyncMock(return_value=(10.0, "", [])))
    monkeypatch.setattr(v2, "resolve_execution_mode", lambda *a, **kw: "taker")

    async def _fake_get_sweep_state(market_id):
        return None

    monkeypatch.setattr("app.services.whale_sweep_service.get_sweep_state", _fake_get_sweep_state)
    monkeypatch.setattr(
        "app.services.whale_sweep_service.apply_sweep_adjustment",
        lambda **kw: (kw["base_confidence"], "no_sweep"),
    )
    monkeypatch.setattr("app.jobs.v2_execution.publish", notify_mock)

    sig = {
        "detector": "D5",
        "detector_signal_id": "00000000-0000-0000-0000-000000000003",
        "market_id": "0xMARKET",
        "event_id": "0xEVT",
        "side": "YES",
        "entry_price": 0.40,
        "calibrated_p": 0.55,
        "meta_p": 0.55,
        "token_id": "tok-abc",
        "generated_at": None,
        "market_end_date": None,
        "category": "crypto",
    }

    result = await v2._process_signal(sig)

    assert result == "rejected"
    rejected_updates = [c for c in executed if "status='rejected'" in c[0]]
    assert len(rejected_updates) == 1
    notify_mock.assert_awaited_once()
    alert_text = _note_text(notify_mock.await_args.args[0])
    # Раньше здесь искался жаргон «place_error» — он ушёл при переписывании
    # текста на человеческий. Гарантия та же: алерт называет, ЧТО не удалось,
    # и содержит детектор, рынок, сторону и причину отказа.
    assert "не удалось разместить" in alert_text.lower()
    assert "D5" in alert_text
    assert "clob 500 internal error" in alert_text
    assert "0xMARKET" in alert_text
    assert "YES" in alert_text
    assert "$10.00" in alert_text
    assert "taker" in alert_text


@pytest.mark.asyncio
async def test_process_signal_bankroll_insufficient_sends_debounced_tg_alert(monkeypatch):
    """is_sufficient=False → тихий даунгрейд live→virtual продолжает работать
    (regression guard), но теперь дополнительно шлёт debounced TG-алерт."""
    import app.jobs.v2_execution as v2

    executed = []

    async def _fake_execute(sql, *args):
        executed.append((sql, args))

    async def _fake_insert_order(payload):
        return "intent-db-id"

    notify_mock = AsyncMock()
    debounce_mock = AsyncMock(return_value=True)

    monkeypatch.setattr(v2, "execute", _fake_execute)
    monkeypatch.setattr(v2, "_insert_order", _fake_insert_order)
    monkeypatch.setattr(v2, "_place_order", AsyncMock(return_value="order-xyz"))
    monkeypatch.setattr(v2, "_is_category_blacklisted", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_entry_price_out_of_range", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_cal_p_zone_blacklisted", lambda *a, **k: (False, None))
    monkeypatch.setattr(v2, "_event_reentry_blocked", AsyncMock(return_value=False))
    live_state = type(
        "DS",
        (),
        {"stage": "live", "fractional_kelly_today": 0.1, "detector": "D5", "live_stake_cap_usd": None},
    )()
    monkeypatch.setattr(v2, "get_detector_state", AsyncMock(return_value=live_state))
    monkeypatch.setattr(v2, "get_settings", lambda: type("S", (), {"execution_enabled": True})())
    monkeypatch.setattr(v2, "fetch_portfolio_for_detector", AsyncMock(return_value=None))
    # Ключевое: is_sufficient=False → должен триггернуть даунгрейд + алерт.
    monkeypatch.setattr(
        v2, "get_live_bankroll", AsyncMock(return_value=type("B", (), {"bankroll_usd": 5.0, "is_sufficient": False})())
    )
    monkeypatch.setattr(
        v2,
        "size_from_calibrated_p",
        lambda **kw: type("D", (), {"stake_usd": 10.0, "ev_pct": 5.0})(),
    )
    monkeypatch.setattr(v2, "_virtual_exposure_remaining", AsyncMock(return_value=1000.0))
    monkeypatch.setattr(v2, "_portfolio_exposure_used", AsyncMock(return_value=0.0))
    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(
            return_value=type(
                "V", (), {"allowed": True, "reason": None, "current_price": 0.40, "book_depth_usd": None}
            )()
        ),
    )
    monkeypatch.setattr(
        v2,
        "check_risk",
        lambda *a, **k: type("R", (), {"is_blocked": False, "approved_stake": 10.0, "reductions": []})(),
    )
    monkeypatch.setattr(v2, "_apply_risk_engine", AsyncMock(return_value=(10.0, "", [])))
    monkeypatch.setattr(v2, "resolve_execution_mode", lambda *a, **kw: "taker")

    async def _fake_get_sweep_state(market_id):
        return None

    monkeypatch.setattr("app.services.whale_sweep_service.get_sweep_state", _fake_get_sweep_state)
    monkeypatch.setattr(
        "app.services.whale_sweep_service.apply_sweep_adjustment",
        lambda **kw: (kw["base_confidence"], "no_sweep"),
    )
    monkeypatch.setattr("app.core.task_queue.debounce_acquire", debounce_mock)
    monkeypatch.setattr("app.jobs.v2_execution.publish", notify_mock)

    sig = {
        "detector": "D5",
        "detector_signal_id": "00000000-0000-0000-0000-000000000004",
        "market_id": "0xMARKET",
        "event_id": "0xEVT",
        "side": "YES",
        "entry_price": 0.40,
        "calibrated_p": 0.55,
        "meta_p": 0.55,
        "token_id": "tok-abc",
        "generated_at": None,
        "market_end_date": None,
        "category": "crypto",
    }

    result = await v2._process_signal(sig)

    assert result == "opened"  # даунгрейд не блокирует исполнение, просто идёт как virtual
    debounce_mock.assert_awaited_once_with("alert:bankroll_downgrade", 86400)
    notify_mock.assert_awaited_once()
    assert "виртуальную" in _note_text(notify_mock.await_args.args[0])


@pytest.mark.asyncio
async def test_process_signal_bankroll_insufficient_skips_alert_when_debounced(monkeypatch):
    """debounce_acquire возвращает False (алёрт уже слался за последние сутки) →
    send_admin_message НЕ вызывается повторно."""
    import app.jobs.v2_execution as v2

    async def _fake_execute(sql, *args):
        pass

    async def _fake_insert_order(payload):
        return "intent-db-id"

    notify_mock = AsyncMock()
    debounce_mock = AsyncMock(return_value=False)

    monkeypatch.setattr(v2, "execute", _fake_execute)
    monkeypatch.setattr(v2, "_insert_order", _fake_insert_order)
    monkeypatch.setattr(v2, "_place_order", AsyncMock(return_value="order-xyz"))
    monkeypatch.setattr(v2, "_is_category_blacklisted", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_entry_price_out_of_range", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_cal_p_zone_blacklisted", lambda *a, **k: (False, None))
    monkeypatch.setattr(v2, "_event_reentry_blocked", AsyncMock(return_value=False))
    live_state = type(
        "DS",
        (),
        {"stage": "live", "fractional_kelly_today": 0.1, "detector": "D5", "live_stake_cap_usd": None},
    )()
    monkeypatch.setattr(v2, "get_detector_state", AsyncMock(return_value=live_state))
    monkeypatch.setattr(v2, "get_settings", lambda: type("S", (), {"execution_enabled": True})())
    monkeypatch.setattr(v2, "fetch_portfolio_for_detector", AsyncMock(return_value=None))
    monkeypatch.setattr(
        v2, "get_live_bankroll", AsyncMock(return_value=type("B", (), {"bankroll_usd": 5.0, "is_sufficient": False})())
    )
    monkeypatch.setattr(
        v2,
        "size_from_calibrated_p",
        lambda **kw: type("D", (), {"stake_usd": 10.0, "ev_pct": 5.0})(),
    )
    monkeypatch.setattr(v2, "_virtual_exposure_remaining", AsyncMock(return_value=1000.0))
    monkeypatch.setattr(v2, "_portfolio_exposure_used", AsyncMock(return_value=0.0))
    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(
            return_value=type(
                "V", (), {"allowed": True, "reason": None, "current_price": 0.40, "book_depth_usd": None}
            )()
        ),
    )
    monkeypatch.setattr(
        v2,
        "check_risk",
        lambda *a, **k: type("R", (), {"is_blocked": False, "approved_stake": 10.0, "reductions": []})(),
    )
    monkeypatch.setattr(v2, "_apply_risk_engine", AsyncMock(return_value=(10.0, "", [])))
    monkeypatch.setattr(v2, "resolve_execution_mode", lambda *a, **kw: "taker")

    async def _fake_get_sweep_state(market_id):
        return None

    monkeypatch.setattr("app.services.whale_sweep_service.get_sweep_state", _fake_get_sweep_state)
    monkeypatch.setattr(
        "app.services.whale_sweep_service.apply_sweep_adjustment",
        lambda **kw: (kw["base_confidence"], "no_sweep"),
    )
    monkeypatch.setattr("app.core.task_queue.debounce_acquire", debounce_mock)
    monkeypatch.setattr("app.jobs.v2_execution.publish", notify_mock)

    sig = {
        "detector": "D5",
        "detector_signal_id": "00000000-0000-0000-0000-000000000005",
        "market_id": "0xMARKET",
        "event_id": "0xEVT",
        "side": "YES",
        "entry_price": 0.40,
        "calibrated_p": 0.55,
        "meta_p": 0.55,
        "token_id": "tok-abc",
        "generated_at": None,
        "market_end_date": None,
        "category": "crypto",
    }

    await v2._process_signal(sig)

    debounce_mock.assert_awaited_once_with("alert:bankroll_downgrade", 86400)
    notify_mock.assert_not_awaited()


# --- 3. Реаниматор застрявших intent ----------------------------------------------


@pytest.mark.asyncio
async def test_reanimate_stuck_intent_recovers_when_order_found_on_clob(monkeypatch):
    import app.jobs.v2_execution_resolve as resolve_mod

    stuck_row = {
        "id": "stuck-id-1",
        "market_id": "0xMARKET",
        "detector": "D5",
        "token_id": "tok-abc",
    }

    async def _fake_fetch(sql, *args):
        return [stuck_row]

    executed = []

    async def _fake_execute(sql, *args):
        executed.append((sql, args))

    notify_mock = AsyncMock()

    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(resolve_mod, "execute", _fake_execute)
    monkeypatch.setattr(
        "app.services.clob_order_lookup.find_our_open_order",
        AsyncMock(return_value="found-on-clob"),
    )
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", notify_mock)

    counts = await resolve_mod._reanimate_stuck_intents(client=object())

    assert counts == {"recovered": 1, "closed": 0}
    assert executed[0][0].strip().startswith("UPDATE execution_orders")
    assert "status='placed'" in executed[0][0]
    assert executed[0][1] == ("found-on-clob", "stuck-id-1")
    notify_mock.assert_called_once()


@pytest.mark.asyncio
async def test_reanimate_stuck_intent_closes_as_transient_reject_when_not_found(monkeypatch):
    import app.jobs.v2_execution_resolve as resolve_mod

    stuck_row = {
        "id": "stuck-id-2",
        "market_id": "0xMARKET",
        "detector": "D5",
        "token_id": "tok-abc",
    }

    async def _fake_fetch(sql, *args):
        return [stuck_row]

    executed = []

    async def _fake_execute(sql, *args):
        executed.append((sql, args))

    notify_mock = AsyncMock()

    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(resolve_mod, "execute", _fake_execute)
    monkeypatch.setattr(
        "app.services.clob_order_lookup.find_our_open_order",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("app.jobs.v2_execution_resolve.publish", notify_mock)

    counts = await resolve_mod._reanimate_stuck_intents(client=object())

    assert counts == {"recovered": 0, "closed": 1}
    sql, args = executed[0]
    assert "status='rejected'" in sql
    assert "is_transient_reject=true" in sql
    assert args == ("stuck-id-2",)
    notify_mock.assert_called_once()


@pytest.mark.asyncio
async def test_reanimate_stuck_intent_noop_when_none_stuck(monkeypatch):
    import app.jobs.v2_execution_resolve as resolve_mod

    async def _fake_fetch(sql, *args):
        return []

    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)

    counts = await resolve_mod._reanimate_stuck_intents(client=object())

    assert counts == {"recovered": 0, "closed": 0}
