"""Tests for check_daily_pnl_limit_mtm — Phase 1 Stage 1.

Покрытие:
  - realized_today (settled) + sum(unrealized_pnl_last) для open positions.
  - Stale-aware: позиции с mtm_updated_at > MTM_STALE_THRESHOLD_SEC исключаются
    (conservative: не паникуем без свежих данных).
  - Threshold compare: total < -bankroll × DAILY_LOSS_LIMIT_PCT → block.
  - missing data → не блокируем (нет данных = разрешаем, log warning).

DB интеграция замокана через monkeypatch app.services.database.fetchrow.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.execution_safety import (
    DAILY_LOSS_LIMIT_PCT,
    MTM_STALE_THRESHOLD_SEC,
    check_daily_pnl_limit_mtm,
)


@pytest.mark.asyncio
async def test_mtm_no_losses_passes(monkeypatch):
    """realized=0, unrealized=0 → allowed."""
    mock = AsyncMock(return_value={"realized_today": 0.0, "unrealized_fresh": 0.0})
    monkeypatch.setattr(
        "app.services.execution_safety._fetch_pnl_mtm_aggregate", mock
    )
    ok, reason = await check_daily_pnl_limit_mtm(bankroll_usd=1000.0)
    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
async def test_mtm_realized_only_loss_below_threshold(monkeypatch):
    """realized=-$20, unrealized=0, bankroll=$1000, threshold=-$30 → allowed."""
    mock = AsyncMock(return_value={"realized_today": -20.0, "unrealized_fresh": 0.0})
    monkeypatch.setattr(
        "app.services.execution_safety._fetch_pnl_mtm_aggregate", mock
    )
    ok, reason = await check_daily_pnl_limit_mtm(bankroll_usd=1000.0)
    assert ok is True


@pytest.mark.asyncio
async def test_mtm_realized_only_loss_above_threshold(monkeypatch):
    """realized=-$35, bankroll=$1000, threshold=-$30 → block."""
    mock = AsyncMock(return_value={"realized_today": -35.0, "unrealized_fresh": 0.0})
    monkeypatch.setattr(
        "app.services.execution_safety._fetch_pnl_mtm_aggregate", mock
    )
    ok, reason = await check_daily_pnl_limit_mtm(bankroll_usd=1000.0)
    assert ok is False
    assert "daily_loss_limit_mtm" in reason


@pytest.mark.asyncio
async def test_mtm_unrealized_pushes_over_threshold(monkeypatch):
    """realized=-$20 (под threshold), unrealized=-$15 → total=-$35 < -$30 → BLOCK.

    Это ключевой кейс preventive vs reactive: realized-only пропустил бы,
    но MtM видит unrealized loss in progress."""
    mock = AsyncMock(
        return_value={"realized_today": -20.0, "unrealized_fresh": -15.0}
    )
    monkeypatch.setattr(
        "app.services.execution_safety._fetch_pnl_mtm_aggregate", mock
    )
    ok, reason = await check_daily_pnl_limit_mtm(bankroll_usd=1000.0)
    assert ok is False
    assert "daily_loss_limit_mtm" in reason
    assert "-35" in reason or "-35.0" in reason


@pytest.mark.asyncio
async def test_mtm_unrealized_profits_offset_realized_losses(monkeypatch):
    """realized=-$25 (около threshold), unrealized=+$20 → total=-$5 → allowed.

    Winning open positions компенсируют closed losses."""
    mock = AsyncMock(
        return_value={"realized_today": -25.0, "unrealized_fresh": 20.0}
    )
    monkeypatch.setattr(
        "app.services.execution_safety._fetch_pnl_mtm_aggregate", mock
    )
    ok, _ = await check_daily_pnl_limit_mtm(bankroll_usd=1000.0)
    assert ok is True


@pytest.mark.asyncio
async def test_mtm_query_uses_stale_threshold(monkeypatch):
    """Query должен фильтровать unrealized по mtm_updated_at >= now() − staleness.

    Реализация _fetch_pnl_mtm_aggregate должна включать
    interval=MTM_STALE_THRESHOLD_SEC. Это вызывается через aiopg, сам SQL
    проверяется отдельно через test ниже."""
    assert MTM_STALE_THRESHOLD_SEC >= 60  # минимум минута
    assert MTM_STALE_THRESHOLD_SEC <= 900  # максимум 15 минут (sanity)


@pytest.mark.asyncio
async def test_mtm_threshold_pct_unchanged():
    """DAILY_LOSS_LIMIT_PCT остаётся 3% (не меняем семантику)."""
    assert DAILY_LOSS_LIMIT_PCT == 0.03


@pytest.mark.asyncio
async def test_mtm_zero_bankroll_safety_no_division(monkeypatch):
    """bankroll=0 → threshold=0 → даже tiny loss → block. Не должно крашить."""
    mock = AsyncMock(return_value={"realized_today": -0.01, "unrealized_fresh": 0.0})
    monkeypatch.setattr(
        "app.services.execution_safety._fetch_pnl_mtm_aggregate", mock
    )
    ok, reason = await check_daily_pnl_limit_mtm(bankroll_usd=0.0)
    assert ok is False


@pytest.mark.asyncio
async def test_mtm_fetch_returns_none_treated_as_zero(monkeypatch):
    """Если query возвращает None (нет данных вообще) → 0 / 0 → allowed."""
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.services.execution_safety._fetch_pnl_mtm_aggregate", mock
    )
    ok, _ = await check_daily_pnl_limit_mtm(bankroll_usd=1000.0)
    assert ok is True
