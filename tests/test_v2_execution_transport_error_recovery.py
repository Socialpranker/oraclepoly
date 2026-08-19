"""Transport-error recovery (инцидент 2026-07-18): py_clob_client_v2 ловит любой
httpx.RequestError и переупаковывает в голый PolyApiException(status_code=None,
error_message="Request exception!") — оригинальный httpx-тип теряется, поэтому
старая isinstance-проверка по httpx-типам никогда не срабатывала для этого пути,
и honest-сверка с CLOB (find_our_open_order) не запускалась. 2 сигнала протухли
в проде (wide_spread/stale_signal на rescan) вместо мгновенного recovery.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from py_clob_client_v2.exceptions import PolyApiException

from app.calibration.ev_sizer import SizingDecision
from app.jobs.v2_execution import _is_transport_error


def test_is_transport_error_true_for_wrapped_request_exception():
    e = PolyApiException(error_msg="Request exception!")
    assert _is_transport_error(e) is True


def test_is_transport_error_false_for_real_clob_rejection():
    e = PolyApiException(error_msg={"error": "insufficient balance"})
    e.status_code = 400
    assert _is_transport_error(e) is False


def test_is_transport_error_false_for_other_exceptions():
    assert _is_transport_error(RuntimeError("order_placement_failed: unknown")) is False
    assert _is_transport_error(ValueError("maker mode requires limit_price")) is False


class _StubService:
    def __init__(self, clob_client):
        self.clob_client = clob_client


@pytest.mark.asyncio
async def test_process_signal_transport_error_recovers_via_clob_lookup(monkeypatch):
    """_place_order кидает PolyApiException(status_code=None) (сетевой сбой,
    переупакованный библиотекой) → find_our_open_order находит ордер на CLOB →
    'placed' вместо мгновенного rejected на протухший сигнал."""
    import app.jobs.v2_execution as v2

    executed = []

    async def _fake_execute(sql, *args):
        executed.append((sql, args))

    async def _fake_insert_order(payload):
        return "intent-db-id"

    monkeypatch.setattr(v2, "execute", _fake_execute)
    monkeypatch.setattr(v2, "_insert_order", _fake_insert_order)
    monkeypatch.setattr(
        v2, "_place_order", AsyncMock(side_effect=PolyApiException(error_msg="Request exception!"))
    )
    monkeypatch.setattr(
        "app.services.clob_order_lookup.find_our_open_order",
        AsyncMock(return_value="recovered-order-id"),
    )
    monkeypatch.setattr(
        "app.services.polymarket_service.PolymarketService",
        lambda: _StubService(clob_client=object()),
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
        lambda **kw: SizingDecision(stake_usd=5.0, reason="ok", ev_pct=5.0, p=0.55, entry_price=0.40),
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
        lambda *a, **k: type("R", (), {"is_blocked": False, "approved_stake": 5.0, "reductions": []})(),
    )
    monkeypatch.setattr(v2, "_apply_risk_engine", AsyncMock(return_value=(5.0, "", [])))
    monkeypatch.setattr(v2, "resolve_execution_mode", lambda *a, **kw: "taker")

    async def _fake_get_sweep_state(market_id):
        return None

    monkeypatch.setattr("app.services.whale_sweep_service.get_sweep_state", _fake_get_sweep_state)
    monkeypatch.setattr(
        "app.services.whale_sweep_service.apply_sweep_adjustment",
        lambda **kw: (kw["base_confidence"], "no_sweep"),
    )

    sig = {
        "detector": "D3",
        "detector_signal_id": "00000000-0000-0000-0000-000000000020",
        "market_id": "0xMARKET",
        "event_id": "0xEVT",
        "side": "NO",
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


@pytest.mark.asyncio
async def test_process_signal_transport_error_not_found_falls_through_to_rejected(monkeypatch):
    """Транспортная ошибка, но find_our_open_order ничего не находит на CLOB
    (ордер реально не дошёл) → обычный rejected-путь, is_transient_reject=true
    (place_error уже в TRANSIENT_PREFIXES — rescan подхватит позже)."""
    import app.jobs.v2_execution as v2

    executed = []

    async def _fake_execute(sql, *args):
        executed.append((sql, args))

    async def _fake_insert_order(payload):
        return "intent-db-id"

    notify_mock = AsyncMock()

    monkeypatch.setattr(v2, "execute", _fake_execute)
    monkeypatch.setattr(v2, "_insert_order", _fake_insert_order)
    monkeypatch.setattr(
        v2, "_place_order", AsyncMock(side_effect=PolyApiException(error_msg="Request exception!"))
    )
    monkeypatch.setattr(
        "app.services.clob_order_lookup.find_our_open_order",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.polymarket_service.PolymarketService",
        lambda: _StubService(clob_client=object()),
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
        lambda **kw: SizingDecision(stake_usd=5.0, reason="ok", ev_pct=5.0, p=0.55, entry_price=0.40),
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
        lambda *a, **k: type("R", (), {"is_blocked": False, "approved_stake": 5.0, "reductions": []})(),
    )
    monkeypatch.setattr(v2, "_apply_risk_engine", AsyncMock(return_value=(5.0, "", [])))
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
        "detector_signal_id": "00000000-0000-0000-0000-000000000021",
        "market_id": "0xMARKET",
        "event_id": "0xEVT",
        "side": "NO",
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
    assert "is_transient_reject=true" in rejected_updates[0][0]
