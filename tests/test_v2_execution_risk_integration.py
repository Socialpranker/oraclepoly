"""Risk engine интеграция в v2_execution._process_signal — Phase 1 Stage 2.

Покрываем _apply_risk_engine — изолированный helper из v2_execution, который
вызывает risk_engine.check_risk и решает блокировать/уменьшить stake.

Полный pipeline _process_signal тестируется уже в test_v2_execution_*.py;
здесь — только новая логика risk_engine integration.

Задача D (2026-07-03): _apply_risk_engine больше не читает peak напрямую через
get_peak_bank — источник peak это update_peak_if_higher(bankroll, key=LIVE_PEAK_BANK_KEY),
который сам делает GREATEST(stored, bankroll) и сидит bankroll при пустом ключе.
Мокаем update_peak_if_higher напрямую (не get_peak_bank) во всех тестах ниже.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.core.peak_bank import LIVE_PEAK_BANK_KEY
from app.jobs.v2_execution import _apply_risk_engine
from app.services.risk_engine import OpenPosition


def _open_pos(*, market_id: str, event_id: str | None, category: str, stake: float) -> OpenPosition:
    return OpenPosition(
        market_id=market_id,
        event_id=event_id,
        category=category,
        stake=stake,
        created_at=datetime.now(UTC),
        resolved_at=None,
    )


@pytest.mark.asyncio
async def test_apply_risk_engine_passes_when_no_exposure(monkeypatch):
    """No open positions → full stake approved."""
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetch_open_positions_for_risk",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.jobs.v2_execution.update_peak_if_higher", AsyncMock(return_value=1000.0)
    )
    approved, reason, reductions = await _apply_risk_engine(
        proposed_stake=50.0,
        market_id="mX",
        event_id="eX",
        category="Sports",
        bankroll=1000.0,
    )
    assert approved == 50.0
    assert reason == ""
    assert reductions == []


@pytest.mark.asyncio
async def test_apply_risk_engine_blocks_category_cap(monkeypatch):
    """Sports cat=30% = $300. Already $300 used → 0 available → block."""
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetch_open_positions_for_risk",
        AsyncMock(
            return_value=[_open_pos(market_id="m1", event_id="e1", category="Sports", stake=300.0)]
        ),
    )
    monkeypatch.setattr(
        "app.jobs.v2_execution.update_peak_if_higher", AsyncMock(return_value=1000.0)
    )
    approved, reason, reductions = await _apply_risk_engine(
        proposed_stake=200.0,
        market_id="m2",
        event_id="e2",
        category="Sports",
        bankroll=1000.0,
    )
    assert approved == 0.0
    assert "risk_block" in reason
    assert "category_cap" in reductions


@pytest.mark.asyncio
async def test_apply_risk_engine_reduces_stake_partial(monkeypatch):
    """Sports cap (30% × $10k = $3000), $2900 used → $100 available.
    proposed $200 → reduce to $100 (cat cap), but per_bet_cap (5% × $10k = $500)
    не активен здесь. Итого approved=$100, reason='' (reduce), passes min_stake $25.
    """
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetch_open_positions_for_risk",
        AsyncMock(
            return_value=[_open_pos(market_id="m1", event_id="e1", category="Sports", stake=2900.0)]
        ),
    )
    monkeypatch.setattr(
        "app.jobs.v2_execution.update_peak_if_higher", AsyncMock(return_value=10000.0)
    )
    approved, reason, reductions = await _apply_risk_engine(
        proposed_stake=200.0,
        market_id="m2",
        event_id="e2",
        category="Sports",
        bankroll=10000.0,
    )
    assert approved == pytest.approx(100.0, abs=0.01)
    assert reason == ""  # reduced, not blocked
    assert "category_cap" in reductions


@pytest.mark.asyncio
async def test_apply_risk_engine_event_cap(monkeypatch):
    """Event cap 10% of $1000 = $100. Same event has $100 → 0 left."""
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetch_open_positions_for_risk",
        AsyncMock(
            return_value=[_open_pos(market_id="m1", event_id="evtX", category="Sports", stake=100.0)]
        ),
    )
    monkeypatch.setattr(
        "app.jobs.v2_execution.update_peak_if_higher", AsyncMock(return_value=1000.0)
    )
    approved, reason, reductions = await _apply_risk_engine(
        proposed_stake=50.0,
        market_id="m2",
        event_id="evtX",
        category="Sports",
        bankroll=1000.0,
    )
    assert approved == 0.0
    assert "risk_block" in reason
    assert "event_cap" in reductions


@pytest.mark.asyncio
async def test_apply_risk_engine_drawdown_reduces(monkeypatch):
    """Peak=$10k, current=$7k → drawdown 30% > 20% threshold → stake × 0.5.
    proposed $100 × 0.5 = $50, passes min_stake $25."""
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetch_open_positions_for_risk",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.jobs.v2_execution.update_peak_if_higher", AsyncMock(return_value=10000.0)
    )
    approved, reason, reductions = await _apply_risk_engine(
        proposed_stake=100.0,
        market_id="m2",
        event_id="e2",
        category="Other",
        bankroll=7000.0,  # current bank
    )
    # per_bet_cap (5% × 7000 = $350) ample. drawdown_reduce: 100 × 0.5 = 50.
    assert approved == pytest.approx(50.0, abs=0.01)
    assert reason == ""
    assert "drawdown_reduce" in reductions


@pytest.mark.asyncio
async def test_apply_risk_engine_unknown_category_treated_as_separate(monkeypatch):
    """category=None / unknown → не объединяется с другими (риск отдельный).
    bankroll=$10k чтобы per_bet_cap не trim'нул $100."""
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetch_open_positions_for_risk",
        AsyncMock(
            return_value=[_open_pos(market_id="m1", event_id="e1", category="Sports", stake=3000.0)]
        ),
    )
    monkeypatch.setattr(
        "app.jobs.v2_execution.update_peak_if_higher", AsyncMock(return_value=10000.0)
    )
    approved, reason, reductions = await _apply_risk_engine(
        proposed_stake=100.0,
        market_id="m2",
        event_id="e2",
        category="unknown",  # not Sports
        bankroll=10000.0,
    )
    assert approved == 100.0
    assert "category_cap" not in reductions


@pytest.mark.asyncio
async def test_apply_risk_engine_peak_bank_none_uses_current_as_seed(monkeypatch):
    """Если peak_bank ещё не установлен → используем current как peak (no drawdown).
    bankroll=$10k чтобы per_bet_cap не trim'нул $50."""
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetch_open_positions_for_risk",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.jobs.v2_execution.update_peak_if_higher", AsyncMock(return_value=10000.0)
    )
    approved, reason, reductions = await _apply_risk_engine(
        proposed_stake=50.0,
        market_id="m",
        event_id="e",
        category="Sports",
        bankroll=10000.0,
    )
    assert approved == 50.0  # no drawdown without prior peak
    assert "drawdown_reduce" not in reductions


# --- Задача D (2026-07-03): live_peak_bank_usd отделён от daemon-exposure ключа ---


@pytest.mark.asyncio
async def test_apply_risk_engine_uses_live_peak_key_not_daemon_key(monkeypatch):
    """_apply_risk_engine должен обновлять/читать через LIVE_PEAK_BANK_KEY, а не
    дефолтный PEAK_BANK_KEY (используемый risk_daemon.py для exposure-знаменателя —
    смешение доменов даёт вечный drawdown при stale-high daemon-peak)."""
    update_peak_mock = AsyncMock(return_value=800.0)
    monkeypatch.setattr("app.jobs.v2_execution.fetch_open_positions_for_risk", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.jobs.v2_execution.update_peak_if_higher", update_peak_mock)
    await _apply_risk_engine(
        proposed_stake=50.0,
        market_id="m",
        event_id="e",
        category="Sports",
        bankroll=800.0,
    )
    update_peak_mock.assert_called_once_with(800.0, key=LIVE_PEAK_BANK_KEY)


@pytest.mark.asyncio
async def test_apply_risk_engine_live_peak_drawdown_uses_live_history(monkeypatch):
    """Живая история peak=$10k (не daemon-эры $5000), current=$7k → 30%
    просадка > 20% threshold → drawdown_reduce срабатывает честно."""
    update_peak_mock = AsyncMock(return_value=10000.0)  # GREATEST(10000, 7000) = 10000
    monkeypatch.setattr("app.jobs.v2_execution.fetch_open_positions_for_risk", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.jobs.v2_execution.update_peak_if_higher", update_peak_mock)

    approved, reason, reductions = await _apply_risk_engine(
        proposed_stake=100.0,
        market_id="m",
        event_id="e",
        category="Sports",
        bankroll=7000.0,
    )
    assert "drawdown_reduce" in reductions
    assert approved == pytest.approx(50.0, abs=0.01)  # per_bet_cap ample (5%×7000=350), ×0.5 drawdown


# --- Phase 1 Stage 3: per-bet + min stake integration ---


@pytest.mark.asyncio
async def test_apply_risk_engine_per_bet_cap_5pct(monkeypatch):
    """Default Settings → per_bet_cap=5% bankroll. bank=$1000, proposed $200 → $50."""
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetch_open_positions_for_risk",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.jobs.v2_execution.update_peak_if_higher", AsyncMock(return_value=1000.0)
    )
    approved, reason, reductions = await _apply_risk_engine(
        proposed_stake=200.0,
        market_id="m",
        event_id=None,  # skip event_cap для изоляции теста
        category="crypto",
        bankroll=1000.0,
    )
    assert approved == pytest.approx(50.0, abs=0.01)
    assert reason == ""  # reduced, not blocked
    assert "per_bet_cap" in reductions


@pytest.mark.asyncio
async def test_apply_risk_engine_min_stake_blocks_tiny(monkeypatch):
    """Default min_stake $25. proposed $10 → block."""
    monkeypatch.setattr(
        "app.jobs.v2_execution.fetch_open_positions_for_risk",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.jobs.v2_execution.update_peak_if_higher", AsyncMock(return_value=1000.0)
    )
    approved, reason, reductions = await _apply_risk_engine(
        proposed_stake=10.0,
        market_id="m",
        event_id=None,
        category="crypto",
        bankroll=1000.0,
    )
    assert approved == 0.0
    assert "risk_block" in reason
    assert "below_min_stake" in reductions
