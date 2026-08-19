"""Ramp-лесенка кодификация (Phase 3 Task 2, audit research-gap).

Канареечный размер live-ставки раньше держался только на бумаге
(docs/superpowers/plans/2026-06-25-d3-live-proxy-ramp.md §3.1) и молча
переставал защищать банк при пополнении кошелька. Три пласта:

  1. DetectorState.live_stake_cap_usd + get_state() читает колонку.
  2. _process_signal применяет cap в live-пути (после sizing/risk_engine,
     до whale-sweep) через dataclasses.replace (SizingDecision — frozen).
  3. ramp_check джоба: находит live-детекторы на известной ступени лесенки,
     резолвнутые (не early-exit) ставки >= min_resolved и pnl >= min_pnl →
     TG-кнопка ramp_up:{detector}:{next_cap} с debounce раз в сутки.
  4. telegram.py ramp_up: callback — UPDATE detector_state.live_stake_cap_usd.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.calibration.ev_sizer import SizingDecision
from app.core.detector_state import DetectorState
from app.jobs import ramp_check

# ── 1. DetectorState.get_state читает live_stake_cap_usd ────────────────────


@pytest.mark.asyncio
async def test_get_state_reads_live_stake_cap_usd(monkeypatch):
    import app.core.detector_state as ds_mod

    async def _fake_fetchrow(sql, *args):
        assert "live_stake_cap_usd" in sql
        return {
            "detector": "D3_cross_mkt_arb",
            "stage": "live",
            "paused_reason": None,
            "promoted_at": None,
            "fractional_kelly_today": 0.1,
            "portfolio_id": None,
            "live_stake_cap_usd": 10.00,
        }

    monkeypatch.setattr(ds_mod, "fetchrow", _fake_fetchrow)

    state = await ds_mod.get_state("D3_cross_mkt_arb")

    assert state.live_stake_cap_usd == 10.00


@pytest.mark.asyncio
async def test_get_state_live_stake_cap_usd_null_is_none(monkeypatch):
    import app.core.detector_state as ds_mod

    async def _fake_fetchrow(sql, *args):
        return {
            "detector": "D5",
            "stage": "virtual",
            "paused_reason": None,
            "promoted_at": None,
            "fractional_kelly_today": 0.1,
            "portfolio_id": None,
            "live_stake_cap_usd": None,
        }

    monkeypatch.setattr(ds_mod, "fetchrow", _fake_fetchrow)

    state = await ds_mod.get_state("D5")

    assert state.live_stake_cap_usd is None


def test_detector_state_dataclass_default_none():
    state = DetectorState(
        detector="D3_cross_mkt_arb",
        stage="live",
        paused_reason=None,
        promoted_at=None,
        fractional_kelly_today=0.1,
    )
    assert state.live_stake_cap_usd is None


# ── 2. _process_signal applies cap в live-пути ───────────────────────────────


def _make_signal():
    return {
        "detector": "D3_cross_mkt_arb",
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


class _StubService:
    def __init__(self, clob_client):
        self.clob_client = clob_client


def _wire_common_live_mocks(monkeypatch, v2, *, cap: float | None, stake_usd: float = 47.0):
    """Общая обвязка live-пути _process_signal вплоть до _place_order (мокнутого)."""
    executed = []

    async def _fake_execute(sql, *args):
        executed.append((sql, args))

    async def _fake_insert_order(payload):
        return "intent-db-id"

    monkeypatch.setattr(v2, "execute", _fake_execute)
    monkeypatch.setattr(v2, "_insert_order", _fake_insert_order)
    monkeypatch.setattr(v2, "_place_order", AsyncMock(return_value="order-xyz"))
    monkeypatch.setattr(v2, "_is_category_blacklisted", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_entry_price_out_of_range", lambda *a, **k: False)
    monkeypatch.setattr(v2, "_is_cal_p_zone_blacklisted", lambda *a, **k: (False, None))
    monkeypatch.setattr(v2, "_event_reentry_blocked", AsyncMock(return_value=False))

    live_state = DetectorState(
        detector="D3_cross_mkt_arb",
        stage="live",
        paused_reason=None,
        promoted_at=None,
        fractional_kelly_today=0.1,
        portfolio_id=None,
        live_stake_cap_usd=cap,
    )
    monkeypatch.setattr(v2, "get_detector_state", AsyncMock(return_value=live_state))
    monkeypatch.setattr(v2, "get_settings", lambda: type("S", (), {"execution_enabled": True})())
    monkeypatch.setattr(v2, "fetch_portfolio_for_detector", AsyncMock(return_value=None))
    monkeypatch.setattr(
        v2, "get_live_bankroll", AsyncMock(return_value=type("B", (), {"bankroll_usd": 1000.0, "is_sufficient": True})())
    )
    monkeypatch.setattr(
        v2,
        "size_from_calibrated_p",
        lambda **kw: SizingDecision(
            stake_usd=stake_usd, reason="ok", ev_pct=5.0, p=kw["calibrated_p"], entry_price=kw["entry_price"]
        ),
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
        lambda *a, **k: type("R", (), {"is_blocked": False, "approved_stake": stake_usd, "reductions": []})(),
    )
    # approved_stake == stake_usd → risk_engine reduce branch не срабатывает
    # (не вызывает dataclasses.replace на decision), cap-логика тестируется отдельно.
    monkeypatch.setattr(v2, "_apply_risk_engine", AsyncMock(return_value=(stake_usd, "", [])))
    monkeypatch.setattr(v2, "resolve_execution_mode", lambda *a, **kw: "taker")

    async def _fake_get_sweep_state(market_id):
        return None

    monkeypatch.setattr("app.services.whale_sweep_service.get_sweep_state", _fake_get_sweep_state)
    monkeypatch.setattr(
        "app.services.whale_sweep_service.apply_sweep_adjustment",
        lambda **kw: (kw["base_confidence"], "no_sweep"),
    )
    monkeypatch.setattr(
        "app.services.polymarket_service.PolymarketService",
        lambda: _StubService(clob_client=object()),
    )
    return executed


@pytest.mark.asyncio
async def test_live_stake_cap_reduces_stake_when_exceeded(monkeypatch):
    """cap=10, sizing вернул 47 → stake=10 в notify + placed order."""
    import app.jobs.v2_execution as v2

    executed = _wire_common_live_mocks(monkeypatch, v2, cap=10.0, stake_usd=47.0)

    captured_notify = {}

    async def _fake_notify(sig, decision, mode, order_id, order_id_db=None):
        captured_notify["stake_usd"] = decision.stake_usd

    monkeypatch.setattr(v2, "_notify_live_bet", _fake_notify)

    result = await v2._process_signal(_make_signal())

    assert result == "opened"
    assert captured_notify["stake_usd"] == 10.0
    placed_updates = [c for c in executed if "status='placed'" in c[0]]
    assert len(placed_updates) == 1


@pytest.mark.asyncio
async def test_live_stake_cap_null_leaves_stake_unchanged(monkeypatch):
    """cap=None → decision.stake_usd не трогается (regression guard, поведение до Task 2)."""
    import app.jobs.v2_execution as v2

    executed = _wire_common_live_mocks(monkeypatch, v2, cap=None, stake_usd=47.0)

    captured_notify = {}

    async def _fake_notify(sig, decision, mode, order_id, order_id_db=None):
        captured_notify["stake_usd"] = decision.stake_usd

    monkeypatch.setattr(v2, "_notify_live_bet", _fake_notify)

    result = await v2._process_signal(_make_signal())

    assert result == "opened"
    assert captured_notify["stake_usd"] == 47.0
    assert executed  # sanity: placement path executed


@pytest.mark.asyncio
async def test_live_stake_cap_noop_when_stake_below_cap(monkeypatch):
    """cap=50, sizing=10 → cap не активируется (stake уже под потолком)."""
    import app.jobs.v2_execution as v2

    _wire_common_live_mocks(monkeypatch, v2, cap=50.0, stake_usd=10.0)

    captured_notify = {}

    async def _fake_notify(sig, decision, mode, order_id, order_id_db=None):
        captured_notify["stake_usd"] = decision.stake_usd

    monkeypatch.setattr(v2, "_notify_live_bet", _fake_notify)

    result = await v2._process_signal(_make_signal())

    assert result == "opened"
    assert captured_notify["stake_usd"] == 10.0


# ── 2.5 thin_book need clamped by ramp-cap (audit 2026-07-03) ───────────────
# check_market_book считает need=max(2×stake_usd, $20) от СЫРОГО Kelly-стейка —
# safety_evaluate (шаг 4) идёт РАНЬШЕ ramp-cap клампа decision.stake_usd (шаг
# 5.3). При cap=$10 честный need=$20 (не $94=2×47 от нередуцированного стейка).
# research/fable-gates-audit-2026-07-03/evidence/01-inventory-repo.md §a.


@pytest.mark.asyncio
async def test_safety_gate_receives_ramp_capped_stake(monkeypatch):
    """cap=10, sizing вернул 47 → SafetyContext.stake_usd=10, не 47."""
    import app.jobs.v2_execution as v2

    _wire_common_live_mocks(monkeypatch, v2, cap=10.0, stake_usd=47.0)

    await v2._process_signal(_make_signal())

    ctx = v2.safety_evaluate.call_args[0][0]
    assert ctx.stake_usd == 10.0


@pytest.mark.asyncio
async def test_safety_gate_receives_raw_stake_when_below_cap(monkeypatch):
    """cap=50, sizing=10 (уже под потолком) → SafetyContext.stake_usd=10 (не искажён клампом)."""
    import app.jobs.v2_execution as v2

    _wire_common_live_mocks(monkeypatch, v2, cap=50.0, stake_usd=10.0)

    await v2._process_signal(_make_signal())

    ctx = v2.safety_evaluate.call_args[0][0]
    assert ctx.stake_usd == 10.0


@pytest.mark.asyncio
async def test_safety_gate_receives_raw_stake_when_cap_null(monkeypatch):
    """cap=None → SafetyContext.stake_usd не клампится (regression guard)."""
    import app.jobs.v2_execution as v2

    _wire_common_live_mocks(monkeypatch, v2, cap=None, stake_usd=47.0)

    await v2._process_signal(_make_signal())

    ctx = v2.safety_evaluate.call_args[0][0]
    assert ctx.stake_usd == 47.0


# ── 3. ramp_check джоба ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ramp_check_offers_promotion_when_rung_conditions_met(monkeypatch):
    from app.jobs import ramp_check

    async def _fake_fetch(sql, *args):
        if "detector_state" in sql:
            return [{"detector": "D3_cross_mkt_arb", "live_stake_cap_usd": 10.0}]
        # _RESOLVED_LIVE_SQL
        return [{"n": 12, "pnl": 5.5}]

    debounce_mock = AsyncMock(return_value=True)
    offer_mock = AsyncMock()

    monkeypatch.setattr(ramp_check, "fetch", _fake_fetch)
    monkeypatch.setattr(ramp_check, "debounce_acquire", debounce_mock)
    monkeypatch.setattr(ramp_check, "_offer_ramp_up", offer_mock)

    result = await ramp_check.run_ramp_check()

    assert result == {"detectors_checked": 1, "promotions_offered": 1}
    offer_mock.assert_awaited_once_with("D3_cross_mkt_arb", 10.0, 20.0, 12, 5.5)
    debounce_mock.assert_awaited_once_with("ramp_check:D3_cross_mkt_arb", 86400)


@pytest.mark.asyncio
async def test_ramp_check_skips_when_below_min_resolved(monkeypatch):
    from app.jobs import ramp_check

    async def _fake_fetch(sql, *args):
        if "detector_state" in sql:
            return [{"detector": "D3_cross_mkt_arb", "live_stake_cap_usd": 10.0}]
        return [{"n": 3, "pnl": 5.5}]  # n < min_resolved=10

    offer_mock = AsyncMock()
    monkeypatch.setattr(ramp_check, "fetch", _fake_fetch)
    monkeypatch.setattr(ramp_check, "_offer_ramp_up", offer_mock)

    result = await ramp_check.run_ramp_check()

    assert result == {"detectors_checked": 1, "promotions_offered": 0}
    offer_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_ramp_check_skips_when_pnl_negative(monkeypatch):
    from app.jobs import ramp_check

    async def _fake_fetch(sql, *args):
        if "detector_state" in sql:
            return [{"detector": "D3_cross_mkt_arb", "live_stake_cap_usd": 10.0}]
        return [{"n": 15, "pnl": -2.0}]  # pnl < min_pnl=0

    offer_mock = AsyncMock()
    monkeypatch.setattr(ramp_check, "fetch", _fake_fetch)
    monkeypatch.setattr(ramp_check, "_offer_ramp_up", offer_mock)

    result = await ramp_check.run_ramp_check()

    assert result == {"detectors_checked": 1, "promotions_offered": 0}
    offer_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_ramp_check_debounced_skips_offer(monkeypatch):
    from app.jobs import ramp_check

    async def _fake_fetch(sql, *args):
        if "detector_state" in sql:
            return [{"detector": "D3_cross_mkt_arb", "live_stake_cap_usd": 10.0}]
        return [{"n": 12, "pnl": 5.5}]

    debounce_mock = AsyncMock(return_value=False)  # уже слали за последние сутки
    offer_mock = AsyncMock()
    monkeypatch.setattr(ramp_check, "fetch", _fake_fetch)
    monkeypatch.setattr(ramp_check, "debounce_acquire", debounce_mock)
    monkeypatch.setattr(ramp_check, "_offer_ramp_up", offer_mock)

    result = await ramp_check.run_ramp_check()

    assert result == {"detectors_checked": 1, "promotions_offered": 0}
    offer_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_ramp_check_top_rung_no_next_cap_skips(monkeypatch):
    """cap на верхней ступени (40.0, next_cap=None) → ничего не предлагаем."""
    from app.jobs import ramp_check

    async def _fake_fetch(sql, *args):
        if "detector_state" in sql:
            return [{"detector": "D3_cross_mkt_arb", "live_stake_cap_usd": 40.0}]
        return [{"n": 100, "pnl": 500.0}]

    offer_mock = AsyncMock()
    monkeypatch.setattr(ramp_check, "fetch", _fake_fetch)
    monkeypatch.setattr(ramp_check, "_offer_ramp_up", offer_mock)

    result = await ramp_check.run_ramp_check()

    assert result == {"detectors_checked": 1, "promotions_offered": 0}
    offer_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_ramp_check_unknown_cap_value_skips():
    """cap не матчит ни одну ступень (например кастомное ручное значение) — no-op, no crash."""
    from app.jobs import ramp_check

    assert ramp_check._find_rung(17.5) is None


def test_ramp_ladder_matches_ramp_plan_section_3_1():
    """RAMP_LADDER — реальные числа из ramp-плана §3.1, не выдуманные placeholder'ы.

    S0 канарейка $5-10 → берём верхнюю границу $10 (согласуется с миграцией:
    UPDATE ... SET live_stake_cap_usd = 10.00 WHERE stage='live').
    S1 $15-20 → next_cap=$20. S2 $30-40 → next_cap=$40.
    S3 «полный» — план не даёт фиксированного числа (по EV-sizer cap) →
    next_cap=None (джоба не предлагает подъём выше S2).
    """
    caps = [rung["cap"] for rung in ramp_check.RAMP_LADDER]
    next_caps = [rung["next_cap"] for rung in ramp_check.RAMP_LADDER]
    assert caps == [10.0, 20.0, 40.0]
    assert next_caps == [20.0, 40.0, None]


# ── 4. telegram.py ramp_up: callback ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_ramp_up_callback_updates_cap(monkeypatch):
    from app.api import telegram as tg

    answered = []
    executed = []

    async def mock_answer_callback_query(cq_id, text=None):
        answered.append((cq_id, text))

    async def mock_execute(sql, *args):
        executed.append((sql, args))
        return "UPDATE 1"

    class _FakeFrom:
        id = 1  # admin

    class _FakeCQ:
        id = "cq-ramp-1"
        data = "ramp_up:D3_cross_mkt_arb:20.0"
        from_ = _FakeFrom()
        message = None

    def _fake_settings():
        class _S:
            admin_telegram_ids = [1]

        return _S()

    monkeypatch.setattr(tg, "get_settings", _fake_settings)
    monkeypatch.setattr(tg, "answer_callback_query", mock_answer_callback_query)
    monkeypatch.setattr(tg, "execute", mock_execute)

    await tg._handle_callback_query(_FakeCQ())

    assert executed[0][1] == ("D3_cross_mkt_arb", 20.0)
    assert "live_stake_cap_usd" in executed[0][0]
    assert answered == [("cq-ramp-1", "✅ D3_cross_mkt_arb: cap поднят до $20")]


@pytest.mark.asyncio
async def test_ramp_up_callback_rejects_non_admin(monkeypatch):
    from app.api import telegram as tg

    answered = []
    executed = []

    async def mock_answer_callback_query(cq_id, text=None):
        answered.append((cq_id, text))

    async def mock_execute(sql, *args):
        executed.append((sql, args))

    class _FakeFrom:
        id = 999  # не admin

    class _FakeCQ:
        id = "cq-ramp-2"
        data = "ramp_up:D3_cross_mkt_arb:20.0"
        from_ = _FakeFrom()
        message = None

    def _fake_settings():
        class _S:
            admin_telegram_ids = [1]

        return _S()

    monkeypatch.setattr(tg, "get_settings", _fake_settings)
    monkeypatch.setattr(tg, "answer_callback_query", mock_answer_callback_query)
    monkeypatch.setattr(tg, "execute", mock_execute)

    await tg._handle_callback_query(_FakeCQ())

    assert executed == [], "non-admin не должен вызвать UPDATE"
    assert answered == [("cq-ramp-2", "⛔ Только для админов")]


# ── 2.6 decide_mode получает реальную глубину книги (gates-audit F1) ─────────
# Хардкод book_depth_usd=200.0 делал depth-условие decide_mode (<100 → taker)
# мёртвым. Реальная глубина приходит из book-fetch'а safety_evaluate; virtual-путь
# книгу не фетчит → None → консервативный fallback 200.0 (поведение не меняется).


@pytest.mark.asyncio
async def test_decide_mode_receives_real_book_depth(monkeypatch):
    import app.jobs.v2_execution as v2
    from app.services.execution_safety import SafetyVerdict

    _wire_common_live_mocks(monkeypatch, v2, cap=10.0, stake_usd=10.0)
    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(return_value=SafetyVerdict(True, "", current_price=0.40, book_depth_usd=57.0)),
    )
    captured = {}
    monkeypatch.setattr(v2, "resolve_execution_mode", lambda *a, **kw: captured.update(kw) or "taker")
    monkeypatch.setattr(v2, "_notify_live_bet", AsyncMock())

    await v2._process_signal(_make_signal())

    assert captured["book_depth_usd"] == 57.0


@pytest.mark.asyncio
async def test_decide_mode_depth_fallback_when_verdict_has_none(monkeypatch):
    """Virtual/None-depth → fallback 200.0 (regression guard старого поведения)."""
    import app.jobs.v2_execution as v2
    from app.services.execution_safety import SafetyVerdict

    _wire_common_live_mocks(monkeypatch, v2, cap=10.0, stake_usd=10.0)
    monkeypatch.setattr(
        v2,
        "safety_evaluate",
        AsyncMock(return_value=SafetyVerdict(True, "", current_price=0.40)),
    )
    captured = {}
    monkeypatch.setattr(v2, "resolve_execution_mode", lambda *a, **kw: captured.update(kw) or "taker")
    monkeypatch.setattr(v2, "_notify_live_bet", AsyncMock())

    await v2._process_signal(_make_signal())

    assert captured["book_depth_usd"] == 200.0
