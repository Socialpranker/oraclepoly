"""Anti-selection fix (spec 2026-07-22, §3): Kelly-стейк live-режима на live-цене.

Task A подняла age-гейт сигналов с 300s до LIVE_SIGNAL_HARD_MAX_AGE_SEC=1800 —
раз впускаем сигналы возрастом до 30 мин, Kelly-стейк обязан пересчитываться
на live-цене (verdict.current_price — live ask НАШЕГО токена, book fetched by
sig["token_id"], side-aware), а не на устаревшей sig["entry_price"] (снапшот
детекции). Shadow-телеметрия (_log_stake_staleness_shadow, PR #674/#694) была
мерой качества; здесь cutover — реальный пересчёт для live-пути.

Контракт:
  - is_live_mode and verdict.current_price is not None → repriced sizer call
    с entry_price=verdict.current_price; decision.stake_usd/ev_pct
    перезаписываются в decision И base (payload для _insert_order).
  - repriced.stake_usd None/<=0 → reject "ev_negative_at_live_price:<reason>;
    signal=...,live=...", ордер НЕ размещается, статус 'rejected'.
  - verdict.current_price is None (safety gate книгу не фетчил) → fallback
    на сигнальную цену, пересчёт НЕ вызывается (старое поведение).
  - virtual-путь (is_live_mode=False) → пересчёт не вызывается вообще,
    даже если бы current_price был задан (shadow-логирование выше — не эта ветка).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.calibration.ev_sizer import SizingDecision
from tests.test_ramp_cap import _make_signal, _wire_common_live_mocks


@pytest.mark.asyncio
async def test_live_stake_recomputed_on_live_price(monkeypatch):
    """live-режим, verdict.current_price=0.55 при signal entry_price=0.40 →
    stake в _insert_order/placement считан от 0.55, не от 0.40."""
    import app.jobs.v2_execution as v2

    executed = _wire_common_live_mocks(monkeypatch, v2, cap=None, stake_usd=47.0)

    captured_notify = {}

    async def _fake_notify(sig, decision, mode, order_id, order_id_db=None):
        captured_notify["stake_usd"] = decision.stake_usd
        captured_notify["ev_pct"] = decision.ev_pct

    monkeypatch.setattr(v2, "_notify_live_bet", _fake_notify)

    # safety_evaluate возвращает verdict.current_price=0.55 (live ask ушёл
    # выше сигнальной цены 0.40) — это НЕ то же, что raw sizing по 0.40=47.0.
    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(
            return_value=type(
                "V", (), {"allowed": True, "reason": None, "current_price": 0.55, "book_depth_usd": None}
            )()
        ),
    )

    repriced_calls = []

    def _fake_size_from_calibrated_p(**kw):
        repriced_calls.append(kw)
        if kw["entry_price"] == 0.55:
            return SizingDecision(stake_usd=30.0, reason="ok", ev_pct=8.0, p=kw["calibrated_p"], entry_price=0.55)
        # raw sizing call (entry_price=0.40 сигнальная) — как в _wire_common_live_mocks.
        return SizingDecision(stake_usd=47.0, reason="ok", ev_pct=5.0, p=kw["calibrated_p"], entry_price=kw["entry_price"])

    monkeypatch.setattr(v2, "size_from_calibrated_p", _fake_size_from_calibrated_p)
    # shadow-телеметрия не должна мешать — оставляем реальной, но она читает
    # тот же size_from_calibrated_p мок (live_price=0.55 совпадает с repriced).

    result = await v2._process_signal(_make_signal())

    assert result == "opened"
    placed_updates = [c for c in executed if "status='placed'" in c[0]]
    assert len(placed_updates) == 1

    # Нагруженная проверка: реально размещённый (в notify → decision) stake/ev_pct
    # взяты из repriced sizer-вызова (live-цена 0.55), НЕ из raw sizing (47.0/5.0).
    assert captured_notify["stake_usd"] == 30.0
    assert captured_notify["ev_pct"] == 8.0

    # repriced sizer вызван с live-ценой 0.55.
    live_price_calls = [c for c in repriced_calls if c["entry_price"] == 0.55]
    assert len(live_price_calls) >= 1


@pytest.mark.asyncio
async def test_live_stake_fallback_when_no_book_price(monkeypatch):
    """verdict.current_price=None → stake от сигнальной цены (как раньше), repricing не вызывается."""
    import app.jobs.v2_execution as v2

    executed = _wire_common_live_mocks(monkeypatch, v2, cap=None, stake_usd=47.0)

    captured_notify = {}

    async def _fake_notify(sig, decision, mode, order_id, order_id_db=None):
        captured_notify["stake_usd"] = decision.stake_usd

    monkeypatch.setattr(v2, "_notify_live_bet", _fake_notify)

    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(
            return_value=type("V", (), {"allowed": True, "reason": None, "current_price": None, "book_depth_usd": None})()
        ),
    )

    calls_seen = []
    orig_sizer = v2.size_from_calibrated_p

    def _spy(**kw):
        calls_seen.append(kw["entry_price"])
        return orig_sizer(**kw)

    monkeypatch.setattr(v2, "size_from_calibrated_p", _spy)

    result = await v2._process_signal(_make_signal())

    assert result == "opened"
    placed_updates = [c for c in executed if "status='placed'" in c[0]]
    assert len(placed_updates) == 1
    # base stake остаётся 47.0 (raw sizing по сигнальной цене) — repricing не тронул.
    assert captured_notify["stake_usd"] == 47.0
    # Ни один вызов sizer'а не получил entry_price != 0.40 (сигнальная) —
    # репрайсинг-ветка не вызывалась вовсе, когда current_price is None.
    assert all(p == 0.40 for p in calls_seen)


@pytest.mark.asyncio
async def test_live_reject_when_ev_negative_at_live_price(monkeypatch):
    """live-цена съела edge (sizer вернул stake=None) → status='rejected',
    rejection_reason startswith 'ev_negative_at_live_price:', ордер не размещался."""
    import app.jobs.v2_execution as v2

    executed = _wire_common_live_mocks(monkeypatch, v2, cap=None, stake_usd=47.0)

    place_order_mock = AsyncMock(return_value="order-xyz")
    monkeypatch.setattr(v2, "_place_order", place_order_mock)

    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(
            return_value=type(
                "V", (), {"allowed": True, "reason": None, "current_price": 0.93, "book_depth_usd": None}
            )()
        ),
    )

    def _fake_size_from_calibrated_p(**kw):
        if kw["entry_price"] == 0.93:
            return SizingDecision(stake_usd=None, reason="ev_below_threshold_net (-1.2%<22.0%)", ev_pct=-1.2, p=kw["calibrated_p"], entry_price=0.93)
        return SizingDecision(stake_usd=47.0, reason="ok", ev_pct=5.0, p=kw["calibrated_p"], entry_price=kw["entry_price"])

    monkeypatch.setattr(v2, "size_from_calibrated_p", _fake_size_from_calibrated_p)

    insert_calls = []

    async def _fake_insert_order(payload):
        insert_calls.append(payload)
        return "intent-db-id"

    monkeypatch.setattr(v2, "_insert_order", _fake_insert_order)

    result = await v2._process_signal(_make_signal())

    assert result == "rejected"
    place_order_mock.assert_not_called()
    assert len(insert_calls) == 1
    rejected_payload = insert_calls[-1]
    assert rejected_payload["status"] == "rejected"
    assert rejected_payload["rejection_reason"].startswith("ev_negative_at_live_price:")
    # Review fix (2026-07-22): reject-payload должен нести repriced-значения,
    # не stale raw-Kelly от сигнальной цены (0.40=47.0/5.0) — иначе audit-trail
    # в БД вводит в заблуждение (выглядит как будто стейк ещё жив).
    assert rejected_payload["stake"] == 0.0  # sizer вернул None — стейка нет
    assert rejected_payload["ev_pct"] == pytest.approx(-1.2)  # repriced ev_pct, не stale 5.0
    placed_updates = [c for c in executed if "status='placed'" in c[0]]
    assert len(placed_updates) == 0


@pytest.mark.asyncio
async def test_virtual_path_unchanged(monkeypatch):
    """virtual-режим: даже когда verdict.current_price задан (0.55, отличается
    от сигнальной 0.40), stake от сигнальной цены — пересчёт не вызывался
    (repricing branch gated на is_live_mode, не на наличие current_price).

    Review fix (2026-07-22): предыдущая версия теста ставила verdict.allowed=
    False, что обрывало поток ДО репрайсинг-ветки (шаг 5.24) — потеря `is_live_
    mode and` в условии не была бы поймана. Теперь verdict.allowed=True и
    current_price=0.55 — поток реально доходит до репрайсинг-ветки и идёт
    дальше в virtual placement (simulate_place замокан на virtual_placed,
    вне скоупа Task B — деталь placement не важна для этого теста).
    """
    import app.jobs.v2_execution as v2
    from app.core.detector_state import DetectorState
    from app.services.virtual_execution import VirtualPlaceResult

    monkeypatch.setattr(v2, "_is_category_blacklisted", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_entry_price_out_of_range", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_cal_p_zone_blacklisted", lambda *a, **k: (False, None))
    monkeypatch.setattr(v2, "_event_reentry_blocked", AsyncMock(return_value=False))

    virtual_state = DetectorState(
        detector="D3_cross_mkt_arb",
        stage="virtual",
        paused_reason=None,
        promoted_at=None,
        fractional_kelly_today=0.1,
        portfolio_id=None,
        live_stake_cap_usd=None,
    )
    monkeypatch.setattr(v2, "get_detector_state", AsyncMock(return_value=virtual_state))
    monkeypatch.setattr(v2, "get_settings", lambda: type("S", (), {"execution_enabled": True})())
    monkeypatch.setattr(v2, "fetch_portfolio_for_detector", AsyncMock(return_value=None))

    repriced_calls = []

    def _fake_size_from_calibrated_p(**kw):
        repriced_calls.append(kw)
        return SizingDecision(stake_usd=47.0, reason="ok", ev_pct=5.0, p=kw["calibrated_p"], entry_price=kw["entry_price"])

    monkeypatch.setattr(v2, "size_from_calibrated_p", _fake_size_from_calibrated_p)
    monkeypatch.setattr(v2, "_virtual_exposure_remaining", AsyncMock(return_value=1000.0))
    monkeypatch.setattr(v2, "_portfolio_exposure_used", AsyncMock(return_value=0.0))
    monkeypatch.setattr(v2, "resolve_execution_mode", lambda *a, **kw: "taker")

    async def _fake_get_sweep_state(market_id):
        return None

    monkeypatch.setattr("app.services.whale_sweep_service.get_sweep_state", _fake_get_sweep_state)
    monkeypatch.setattr(
        "app.services.whale_sweep_service.apply_sweep_adjustment",
        lambda **kw: (kw["base_confidence"], "no_sweep"),
    )
    monkeypatch.setattr(
        "app.services.virtual_execution.simulate_place",
        AsyncMock(return_value=VirtualPlaceResult(status="virtual_placed", reason="ok")),
    )

    executed = []

    async def _fake_execute(sql, *args):
        executed.append((sql, args))

    monkeypatch.setattr(v2, "execute", _fake_execute)

    insert_calls = []

    async def _fake_insert_order(payload):
        insert_calls.append(payload)
        return "intent-db-id"

    monkeypatch.setattr(v2, "_insert_order", _fake_insert_order)

    # verdict.allowed=True + current_price=0.55 (≠ сигнальная 0.40) — поток
    # реально доходит до репрайсинг-ветки Task B; is_live_mode=False должен
    # её заглушить несмотря на заданный current_price.
    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(
            return_value=type(
                "V", (), {"allowed": True, "reason": None, "current_price": 0.55, "book_depth_usd": None}
            )()
        ),
    )

    result = await v2._process_signal(_make_signal())

    assert result == "opened"
    assert len(insert_calls) == 1
    assert insert_calls[-1]["stake"] == 47.0  # сигнальная цена, репрайсинг не вызывался
    assert insert_calls[-1]["ev_pct"] == 5.0
    # _log_stake_staleness_shadow (существующая телеметрия, ортогональна Task B)
    # ЗАКОННО зовёт sizer с live_price=0.55 ровно 1 раз — это не репрайсинг-ветка
    # Task B. Если бы репрайсинг-ветка Task B сработала (баг: gate потерян),
    # был бы ВТОРОЙ вызов с entry_price=0.55 — считаем звонки, не сам факт цены.
    live_price_calls = [c for c in repriced_calls if c["entry_price"] == 0.55]
    assert len(live_price_calls) == 1  # только shadow-телеметрия, репрайсинг Task B не сработал
