"""Fix-3 шаг 1 (latency-аудит №3): shadow-телеметрия «stake до/после».

Kelly-stake считается по stale sig["entry_price"] (снапшот детекции), а свежая
цена verdict.current_price из book-fetch'а safety gate появляется ПОЗЖЕ и на
stake не влияет (правит только limit_price). Прежде чем пересчитывать stake
(шаг 2, отдельное решение по данным), собираем телеметрию: каким был бы stake,
если бы sizer видел live-цену.

Контракт _log_stake_staleness_shadow(...) -> dict | None:
  - live_price None (virtual-путь: safety gate книгу не фетчит) → None, без лога.
  - live_price задан → пересчёт size_from_calibrated_p с теми же параметрами,
    но entry_price=live_price; лог v2_exec_stake_staleness_shadow; payload
    с entry_price/live_price/stake_stale/stake_live возвращается.
  - shadow-sizer вернул skip (stake None) → stake_live=0.0 + shadow_skip_reason.
  - Любое исключение внутри → None, наружу не летит (money-path safe).
  - Поведение _process_signal НЕ меняется: decision не мутируется.
"""

from __future__ import annotations

import pytest

from app.calibration.ev_sizer import size_from_calibrated_p
from app.jobs.v2_execution import _log_stake_staleness_shadow

# ---------- Хелпер: no-op без live-цены ----------


def test_returns_none_when_no_live_price():
    """virtual-путь: verdict.current_price=None → телеметрии нет."""
    payload = _log_stake_staleness_shadow(
        detector="D3_cross_mkt_arb",
        sig={"market_id": "0xM", "entry_price": 0.40, "meta_p": 0.55, "category": "crypto"},
        live_price=None,
        stale_stake_usd=47.0,
        bankroll=1000.0,
        fractional_kelly=0.1,
    )
    assert payload is None


# ---------- Хелпер: пересчёт по live-цене ----------


def test_shadow_recomputes_stake_with_live_price():
    """Цена ушла 0.40 → 0.50: payload содержит оба stake, shadow == sizer(live)."""
    # p=0.70 (было 0.55): при верной формуле EV = p*(1/entry - 1) - (1-p) на
    # входе 0.50 даёт +40% — выше порога 20%, сценарий не вырождается в skip.
    # Со старой (завышенной) формулой хватало и 0.55. Тест про равенство
    # shadow == sizer(live), поэтому конкретное p роли не играет — важно лишь,
    # чтобы обе ветки реально считали стейк.
    sig = {
        "market_id": "0xM",
        "entry_price": 0.40,
        "meta_p": 0.70,
        "category": None,
        "fee_schedule": None,
    }
    expected_live = size_from_calibrated_p(
        calibrated_p=0.70,
        entry_price=0.50,
        bankroll_usd=1000.0,
        category=None,
        fee_schedule=None,
        fractional_kelly=0.1,
    )
    assert expected_live.stake_usd is not None  # sanity: сценарий не skip

    payload = _log_stake_staleness_shadow(
        detector="D3_cross_mkt_arb",
        sig=sig,
        live_price=0.50,
        stale_stake_usd=47.0,
        bankroll=1000.0,
        fractional_kelly=0.1,
    )

    assert payload is not None
    assert payload["market_id"] == "0xM"
    assert payload["detector"] == "D3_cross_mkt_arb"
    assert payload["entry_price"] == 0.40
    assert payload["live_price"] == 0.50
    assert payload["stake_stale"] == 47.0
    assert payload["stake_live"] == pytest.approx(float(expected_live.stake_usd))
    assert payload["stake_delta_usd"] == pytest.approx(float(expected_live.stake_usd) - 47.0)


def test_shadow_skip_maps_to_zero_stake_with_reason():
    """live-цена ушла за ceiling → sizer skip → stake_live=0.0 + причина."""
    payload = _log_stake_staleness_shadow(
        detector="D3_cross_mkt_arb",
        sig={"market_id": "0xM", "entry_price": 0.90, "meta_p": 0.92, "category": None},
        live_price=0.999,  # ≥ ENTRY_PRICE_CEILING → invalid_entry_price/ceiling skip
        stale_stake_usd=12.0,
        bankroll=1000.0,
        fractional_kelly=0.1,
    )
    assert payload is not None
    assert payload["stake_live"] == 0.0
    assert payload["shadow_skip_reason"]


def test_sizer_exception_swallowed(monkeypatch):
    """Исключение sizer'а не должно ронять money-path: → None."""
    import app.jobs.v2_execution as v2

    def _boom(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(v2, "size_from_calibrated_p", _boom)

    payload = _log_stake_staleness_shadow(
        detector="D3_cross_mkt_arb",
        sig={"market_id": "0xM", "entry_price": 0.40, "meta_p": 0.55},
        live_price=0.50,
        stale_stake_usd=47.0,
        bankroll=1000.0,
        fractional_kelly=0.1,
    )
    assert payload is None


# ---------- Wiring: _process_signal вызывает shadow-телеметрию ----------


@pytest.mark.asyncio
async def test_process_signal_calls_shadow_after_safety_gate(monkeypatch):
    """Live-путь: хелпер вызван с live-ценой из verdict и raw Kelly-stake
    (до risk_engine reduce и ramp-cap клампа)."""
    import app.jobs.v2_execution as v2
    from tests.test_ramp_cap import _make_signal, _wire_common_live_mocks

    # cap=10 при stake=47: клампы сработают ПОСЛЕ телеметрии —
    # shadow должен увидеть raw 47.0, а не 10.0.
    _wire_common_live_mocks(monkeypatch, v2, cap=10.0, stake_usd=47.0)

    async def _fake_notify(sig, decision, mode, order_id, order_id_db=None):
        return None

    monkeypatch.setattr(v2, "_notify_live_bet", _fake_notify)

    calls = []

    def _spy(**kw):
        calls.append(kw)
        return None

    monkeypatch.setattr(v2, "_log_stake_staleness_shadow", _spy)

    result = await v2._process_signal(_make_signal())

    assert result == "opened"
    assert len(calls) == 1
    kw = calls[0]
    assert kw["live_price"] == 0.40  # verdict.current_price из мока
    assert kw["stale_stake_usd"] == 47.0  # raw Kelly до ramp-cap
    assert kw["detector"] == "D3_cross_mkt_arb"
    assert kw["bankroll"] == 1000.0
    assert kw["fractional_kelly"] == 0.1
