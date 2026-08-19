"""FOK-reanimate (audit d3-funnel 2026-07-16 топ-фикс): taker FOK отбит биржей
с "order couldn't be fully filled" на тонком стакане → один ретрай уменьшенным
стейком по реальной book depth, вместо мгновенного rejected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.calibration.ev_sizer import SizingDecision
from app.jobs.v2_execution import _is_fok_not_filled_error


def test_is_fok_not_filled_error_matches_clob_message():
    e = RuntimeError(
        "PolyApiException[status_code=400, error_message={'error': "
        "\"order couldn't be fully filled. FOK orders are fully filled or killed.\", "
        "'orderID': '0x123'}]"
    )
    assert _is_fok_not_filled_error(e) is True


def test_is_fok_not_filled_error_false_for_other_errors():
    assert _is_fok_not_filled_error(RuntimeError("clob 500 internal error")) is False
    assert _is_fok_not_filled_error(ValueError("maker mode requires limit_price")) is False


@pytest.mark.asyncio
async def test_process_signal_fok_reject_retries_with_book_depth_stake(monkeypatch):
    """Первая FOK-попытка отбита ("not fully filled"), book depth = $6 usable.
    Второй _place_order вызывается уменьшенным стейком (<= book depth), успех → 'opened'."""
    import app.jobs.v2_execution as v2

    executed = []

    async def _fake_execute(sql, *args):
        executed.append((sql, args))

    async def _fake_insert_order(payload):
        return "intent-db-id"

    place_calls = []

    async def _fake_place_order(signal, mode, limit_price, price_cap=None):
        place_calls.append(dict(signal))
        if len(place_calls) == 1:
            raise RuntimeError(
                "PolyApiException[status_code=400, error_message={'error': "
                "\"order couldn't be fully filled. FOK orders are fully filled or killed.\", "
                "'orderID': '0xabc'}]"
            )
        return "order-retry-ok"

    monkeypatch.setattr(v2, "execute", _fake_execute)
    monkeypatch.setattr(v2, "_insert_order", _fake_insert_order)
    monkeypatch.setattr(v2, "_place_order", _fake_place_order)
    monkeypatch.setattr(
        "app.services.virtual_execution.fetch_full_book",
        AsyncMock(
            return_value={
                "asks": [{"price": "0.41", "size": "10"}],
                "bids": [{"price": "0.40", "size": "10"}],
            }
        ),
    )
    monkeypatch.setattr(v2, "_is_category_blacklisted", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_entry_price_out_of_range", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_cal_p_zone_blacklisted", lambda *a, **k: (False, None))
    monkeypatch.setattr(v2, "_event_reentry_blocked", AsyncMock(return_value=False))
    live_state = type(
        "DS",
        (),
        {"stage": "live", "fractional_kelly_today": 0.1, "detector": "D3", "live_stake_cap_usd": None},
    )()
    monkeypatch.setattr(v2, "get_detector_state", AsyncMock(return_value=live_state))
    monkeypatch.setattr(
        v2, "get_settings", lambda: type("S", (), {"execution_enabled": True, "live_min_stake": 1.0})()
    )
    monkeypatch.setattr(v2, "fetch_portfolio_for_detector", AsyncMock(return_value=None))
    monkeypatch.setattr(
        v2, "get_live_bankroll", AsyncMock(return_value=type("B", (), {"bankroll_usd": 1000.0, "is_sufficient": True})())
    )
    monkeypatch.setattr(
        v2,
        "size_from_calibrated_p",
        lambda **kw: SizingDecision(stake_usd=10.0, reason="ok", ev_pct=5.0, p=0.55, entry_price=0.40),
    )
    monkeypatch.setattr(v2, "_virtual_exposure_remaining", AsyncMock(return_value=1000.0))
    monkeypatch.setattr(v2, "_portfolio_exposure_used", AsyncMock(return_value=0.0))
    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(return_value=type("V", (), {"allowed": True, "reason": None, "current_price": 0.40, "book_depth_usd": None})()),
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
    notify_mock = AsyncMock()
    monkeypatch.setattr(v2, "_notify_live_bet", notify_mock)

    sig = {
        "detector": "D3",
        "detector_signal_id": "00000000-0000-0000-0000-000000000010",
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
    assert len(place_calls) == 2
    assert place_calls[0]["stake"] == 10.0
    assert place_calls[1]["stake"] < 10.0
    placed_updates = [c for c in executed if "status='placed'" in c[0]]
    assert len(placed_updates) == 1
    assert placed_updates[0][1][0] == "order-retry-ok"
    assert placed_updates[0][1][2] == "intent-db-id"
    assert placed_updates[0][1][1] < 10.0  # retry_stake сохранён

    # regression: успешный reanimate обязан слать TG-уведомление о live-ставке
    # (audit топ-1: live был BUY-only без exit-уведомления) с УРЕЗАННЫМ стейком.
    notify_mock.assert_awaited_once()
    notify_call_decision = notify_mock.await_args.args[1]
    assert notify_call_decision.stake_usd < 10.0
    assert notify_mock.await_args.args[3] == "order-retry-ok"


@pytest.mark.asyncio
async def test_process_signal_fok_reject_no_usable_depth_rejects_without_retry(monkeypatch):
    """Book depth пуст (usable_usdc ~ 0) → нет смысла ретраить, сразу rejected,
    _place_order вызывается только один раз."""
    import app.jobs.v2_execution as v2

    executed = []

    async def _fake_execute(sql, *args):
        executed.append((sql, args))

    async def _fake_insert_order(payload):
        return "intent-db-id"

    place_calls = []

    async def _fake_place_order(signal, mode, limit_price, price_cap=None):
        place_calls.append(dict(signal))
        raise RuntimeError(
            "PolyApiException[status_code=400, error_message={'error': "
            "\"order couldn't be fully filled. FOK orders are fully filled or killed.\", "
            "'orderID': '0xabc'}]"
        )

    notify_mock = AsyncMock()

    monkeypatch.setattr(v2, "execute", _fake_execute)
    monkeypatch.setattr(v2, "_insert_order", _fake_insert_order)
    monkeypatch.setattr(v2, "_place_order", _fake_place_order)
    monkeypatch.setattr(
        "app.services.virtual_execution.fetch_full_book", AsyncMock(return_value={"asks": [], "bids": []})
    )
    monkeypatch.setattr(v2, "_is_category_blacklisted", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_entry_price_out_of_range", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_cal_p_zone_blacklisted", lambda *a, **k: (False, None))
    monkeypatch.setattr(v2, "_event_reentry_blocked", AsyncMock(return_value=False))
    live_state = type(
        "DS",
        (),
        {"stage": "live", "fractional_kelly_today": 0.1, "detector": "D3", "live_stake_cap_usd": None},
    )()
    monkeypatch.setattr(v2, "get_detector_state", AsyncMock(return_value=live_state))
    monkeypatch.setattr(
        v2, "get_settings", lambda: type("S", (), {"execution_enabled": True, "live_min_stake": 1.0})()
    )
    monkeypatch.setattr(v2, "fetch_portfolio_for_detector", AsyncMock(return_value=None))
    monkeypatch.setattr(
        v2, "get_live_bankroll", AsyncMock(return_value=type("B", (), {"bankroll_usd": 1000.0, "is_sufficient": True})())
    )
    monkeypatch.setattr(
        v2,
        "size_from_calibrated_p",
        lambda **kw: SizingDecision(stake_usd=10.0, reason="ok", ev_pct=5.0, p=0.55, entry_price=0.40),
    )
    monkeypatch.setattr(v2, "_virtual_exposure_remaining", AsyncMock(return_value=1000.0))
    monkeypatch.setattr(v2, "_portfolio_exposure_used", AsyncMock(return_value=0.0))
    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(return_value=type("V", (), {"allowed": True, "reason": None, "current_price": 0.40, "book_depth_usd": None})()),
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
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    sig = {
        "detector": "D3",
        "detector_signal_id": "00000000-0000-0000-0000-000000000011",
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
    assert len(place_calls) == 1
    rejected_updates = [c for c in executed if "status='rejected'" in c[0]]
    assert len(rejected_updates) == 1


@pytest.mark.asyncio
async def test_process_signal_fok_reject_full_depth_skips_retry(monkeypatch):
    """Book depth >= original_stake (отказ был не про depth, или книга не изменилась) —
    ретрай тем же размером повторил бы тот же отказ, поэтому пропускаем его и идём
    сразу в rejected. _place_order вызывается только один раз."""
    import app.jobs.v2_execution as v2

    executed = []

    async def _fake_execute(sql, *args):
        executed.append((sql, args))

    async def _fake_insert_order(payload):
        return "intent-db-id"

    place_calls = []

    async def _fake_place_order(signal, mode, limit_price, price_cap=None):
        place_calls.append(dict(signal))
        raise RuntimeError(
            "PolyApiException[status_code=400, error_message={'error': "
            "\"order couldn't be fully filled. FOK orders are fully filled or killed.\", "
            "'orderID': '0xabc'}]"
        )

    notify_mock = AsyncMock()

    monkeypatch.setattr(v2, "execute", _fake_execute)
    monkeypatch.setattr(v2, "_insert_order", _fake_insert_order)
    monkeypatch.setattr(v2, "_place_order", _fake_place_order)
    monkeypatch.setattr(
        "app.services.virtual_execution.fetch_full_book",
        AsyncMock(
            return_value={
                "asks": [{"price": "0.41", "size": "1000"}],
                "bids": [{"price": "0.40", "size": "1000"}],
            }
        ),
    )
    monkeypatch.setattr(v2, "_is_category_blacklisted", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_entry_price_out_of_range", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_cal_p_zone_blacklisted", lambda *a, **k: (False, None))
    monkeypatch.setattr(v2, "_event_reentry_blocked", AsyncMock(return_value=False))
    live_state = type(
        "DS",
        (),
        {"stage": "live", "fractional_kelly_today": 0.1, "detector": "D3", "live_stake_cap_usd": None},
    )()
    monkeypatch.setattr(v2, "get_detector_state", AsyncMock(return_value=live_state))
    monkeypatch.setattr(
        v2, "get_settings", lambda: type("S", (), {"execution_enabled": True, "live_min_stake": 1.0})()
    )
    monkeypatch.setattr(v2, "fetch_portfolio_for_detector", AsyncMock(return_value=None))
    monkeypatch.setattr(
        v2, "get_live_bankroll", AsyncMock(return_value=type("B", (), {"bankroll_usd": 1000.0, "is_sufficient": True})())
    )
    monkeypatch.setattr(
        v2,
        "size_from_calibrated_p",
        lambda **kw: SizingDecision(stake_usd=10.0, reason="ok", ev_pct=5.0, p=0.55, entry_price=0.40),
    )
    monkeypatch.setattr(v2, "_virtual_exposure_remaining", AsyncMock(return_value=1000.0))
    monkeypatch.setattr(v2, "_portfolio_exposure_used", AsyncMock(return_value=0.0))
    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(return_value=type("V", (), {"allowed": True, "reason": None, "current_price": 0.40, "book_depth_usd": None})()),
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
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", notify_mock)

    sig = {
        "detector": "D3",
        "detector_signal_id": "00000000-0000-0000-0000-000000000012",
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
    assert len(place_calls) == 1
    rejected_updates = [c for c in executed if "status='rejected'" in c[0]]
    assert len(rejected_updates) == 1
