"""Гейт цены входа + принудительный taker для детекторов из entry_price_gate_detectors.

Спека: docs/superpowers/specs/2026-08-05-entry-price-gate-taker-mode-design.md

Замер 2026-08-05: edge D3 живёт только на входах <0.5 (там +60.6% taker ROI net
за 60д, устойчиво 13/13 недель), выше — минус. Maker при этом наливают охотнее
именно в убыточной полосе (fill-rate 17.9% против 13.2%) — adverse selection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.execution_safety import (
    SafetyContext,
    check_entry_price,
    evaluate,
)

D3 = "D3_cross_mkt_arb"
OTHER = "D7b_no_favorite_fade"


# ─── check_entry_price: чистая логика ───


def test_entry_price_below_threshold_passes():
    ok, reason = check_entry_price(D3, 0.45)
    assert ok is True
    assert reason == ""


def test_entry_price_at_threshold_rejected():
    """Порог невключающий: 0.50 >= 0.50 → reject. Бакет 0.5-0.6 даёт +6.0% —
    почти ноль до учёта того, что симуляция оптимистична (риск 8.1 спеки)."""
    ok, reason = check_entry_price(D3, 0.50)
    assert ok is False
    assert "entry_price_too_high" in reason


def test_entry_price_above_threshold_rejected():
    ok, reason = check_entry_price(D3, 0.75)
    assert ok is False
    assert "entry_price_too_high" in reason


def test_entry_price_reason_carries_numbers():
    """Причина отказа должна называть и цену, и порог — иначе воронку не разобрать."""
    ok, reason = check_entry_price(D3, 0.7312)
    assert ok is False
    assert "0.7312" in reason
    assert "0.50" in reason


def test_entry_price_gate_skips_other_detectors():
    """Механизм per-detector: у D36 эффекта нет (−14.2 против −14.1), у STANDALONE
    полоса <0.5 даёт всего +1.9% — общий порог навредил бы без выигрыша."""
    ok, reason = check_entry_price(OTHER, 0.95)
    assert ok is True
    assert reason == ""


def test_entry_price_gate_disabled_by_empty_config(monkeypatch):
    """Пустой список детекторов = гейт выключен целиком."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ENTRY_PRICE_GATE_DETECTORS", "")
    try:
        ok, reason = check_entry_price(D3, 0.95)
        assert ok is True
        assert reason == ""
    finally:
        get_settings.cache_clear()


def test_entry_price_threshold_configurable(monkeypatch):
    """Порог 0.5 — компромисс объёма и ROI, не оптимум (перегиба нет: 0.44→+84.7%,
    0.50→+56.6%, 0.60→+35.9%). Сдвиг не должен требовать правки кода."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ENTRY_PRICE_GATE_MAX", "0.60")
    try:
        ok, _ = check_entry_price(D3, 0.55)
        assert ok is True, "0.55 < порог 0.60 → должен проходить"
        ok, _ = check_entry_price(D3, 0.65)
        assert ok is False
    finally:
        get_settings.cache_clear()


def test_entry_price_detector_list_parses_csv(monkeypatch):
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ENTRY_PRICE_GATE_DETECTORS", f"{D3}, {OTHER}")
    try:
        ok, _ = check_entry_price(OTHER, 0.75)
        assert ok is False, "пробелы в CSV не должны ломать матч"
    finally:
        get_settings.cache_clear()


# ─── Интеграция в evaluate() ───


def _ctx(**overrides):
    base = dict(
        detector=D3,
        detector_state=None,
        market_id="0xmarket1",
        token_id="tok1",
        side="NO",
        signal_price=0.45,
        stake_usd=10.0,
        bankroll_usd=1000.0,
        generated_at=datetime.now(UTC) - timedelta(seconds=10),
        market_end_date=datetime.now(UTC) + timedelta(days=5),
    )
    base.update(overrides)
    return SafetyContext(**base)


@pytest.mark.asyncio
async def test_evaluate_live_rejects_high_entry_price():
    """Гейт стоит до async-чеков и book-I/O — арифметика дешевле того, что отсекает."""
    verdict = await evaluate(_ctx(signal_price=0.72), mode="live")
    assert verdict.allowed is False
    assert "entry_price_too_high" in verdict.reason


@pytest.mark.asyncio
async def test_evaluate_live_does_not_reject_other_detector_on_price():
    """Не-D3 с той же ценой не должен падать ИМЕННО на entry_price."""
    verdict = await evaluate(_ctx(detector=OTHER, signal_price=0.72), mode="live")
    assert "entry_price_too_high" not in verdict.reason


@pytest.mark.asyncio
async def test_evaluate_virtual_still_bypasses_entry_price():
    """evaluate() при virtual отдаёт allow сразу (риск 8.4 спеки): D3 сейчас в
    virtual, поэтому live-гейт на него не подействует до промоута. Единственный
    источник данных до live — shadow-замер."""
    verdict = await evaluate(_ctx(signal_price=0.99), mode="virtual")
    assert verdict.allowed is True


# ─── Shadow-замер (единственный источник данных, пока D3 в virtual) ───


@pytest.mark.asyncio
async def test_shadow_logs_would_reject_entry_price():
    """Гейт обязан дублироваться в shadow: иначе замер снова мерит не тот гейт,
    что решает — ровно ошибка, закрытая PR #758."""
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from app.services.execution_safety import shadow_evaluate_virtual

    ctx = _ctx(signal_price=0.72, detector_signal_id=uuid4())
    with (
        patch(
            "app.services.execution_safety.check_max_open_positions",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_max_inflight_orders",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_daily_pnl_limit_mtm",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch("app.services.gate_shadow_telemetry.log_gate_shadow", new_callable=AsyncMock) as mock_log,
    ):
        await shadow_evaluate_virtual(ctx)

    gates = [c.kwargs["gate"] for c in mock_log.await_args_list]
    assert "shadow_v2:entry_price" in gates


@pytest.mark.asyncio
async def test_shadow_no_entry_price_row_when_price_ok():
    from unittest.mock import AsyncMock, patch
    from uuid import uuid4

    from app.services.execution_safety import shadow_evaluate_virtual

    ctx = _ctx(signal_price=0.30, detector_signal_id=uuid4())
    with (
        patch(
            "app.services.execution_safety.check_max_open_positions",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_max_inflight_orders",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_daily_pnl_limit_mtm",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch("app.services.gate_shadow_telemetry.log_gate_shadow", new_callable=AsyncMock) as mock_log,
    ):
        await shadow_evaluate_virtual(ctx)

    gates = [c.kwargs["gate"] for c in mock_log.await_args_list]
    assert "shadow_v2:entry_price" not in gates


# ─── Принудительный taker (decide_mode остаётся нетронутым для прочих) ───


def test_resolve_mode_forces_taker_for_gated_detector():
    """Гейт и режим осмысленны только вместе: пропустить сигнал и снова не
    исполниться лимиткой = ничего не изменить (решение 3.2 спеки)."""
    from app.services.execution_safety import resolve_execution_mode

    # Условия, при которых старая эвристика вернула бы maker
    mode = resolve_execution_mode(D3, book_depth_usd=5000.0, signal_age_sec=1.0, ev_pct=1.0)
    assert mode == "taker"


def test_resolve_mode_keeps_heuristic_for_other_detectors():
    from app.services.execution_safety import decide_mode, resolve_execution_mode

    args = dict(book_depth_usd=5000.0, signal_age_sec=1.0, ev_pct=1.0)
    assert resolve_execution_mode(OTHER, **args) == decide_mode(**args) == "maker"


def test_resolve_mode_heuristic_taker_unchanged():
    """Тонкий book у не-gated детектора по-прежнему даёт taker."""
    from app.services.execution_safety import resolve_execution_mode

    assert resolve_execution_mode(OTHER, book_depth_usd=50.0, signal_age_sec=1.0, ev_pct=1.0) == "taker"


@pytest.mark.asyncio
async def test_process_signal_passes_real_detector_to_mode_resolver(monkeypatch):
    """Регрессия: имя детектора обязано доезжать до resolve_execution_mode.

    Без этой проверки подмена аргумента на константу проходит мимо всех тестов —
    override для D3 молча не сработал бы в проде, а гейт цены входа без taker'а
    бессмыслен (решение 3.2 спеки).
    """
    from unittest.mock import AsyncMock

    import app.jobs.v2_execution as v2
    from tests.test_ramp_cap import _make_signal, _wire_common_live_mocks

    _wire_common_live_mocks(monkeypatch, v2, cap=10.0, stake_usd=10.0)
    captured: dict = {}

    def _spy(detector, **kw):
        captured["detector"] = detector
        return "taker"

    monkeypatch.setattr(v2, "resolve_execution_mode", _spy)
    monkeypatch.setattr(v2, "_notify_live_bet", AsyncMock())

    await v2._process_signal(_make_signal())

    assert captured["detector"] == D3
