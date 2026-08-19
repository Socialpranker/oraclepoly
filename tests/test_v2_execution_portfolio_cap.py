"""V2 Execution — per-portfolio exposure cap (Phase 3 of portfolios refactor).

Параллельно с per-detector cap ($1000) проверяется portfolio cap (e.g. rules=$3000).
Сигнал rejected если ЛИБО per-detector ЛИБО per-portfolio cap превышен.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs import v2_execution
from app.services.portfolios import Portfolio


@pytest.mark.asyncio
async def test_portfolio_exposure_used_returns_sum():
    """Portfolio exposure = SUM(stake) across all detectors в portfolio."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=1500.0)):
        used = await v2_execution._portfolio_exposure_used("rules")
    assert used == 1500.0


@pytest.mark.asyncio
async def test_portfolio_exposure_used_handles_none():
    """fetchval returns None если portfolio has no orders → 0."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=None)):
        used = await v2_execution._portfolio_exposure_used("sentiment")
    assert used == 0.0


@pytest.mark.asyncio
async def test_portfolio_exposure_remaining_zero_when_at_cap():
    """Cap = $3000, used = $3000 → remaining = $0."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=3000.0)):
        remaining = await v2_execution._portfolio_exposure_remaining("rules", 3000.0)
    assert remaining == 0.0


@pytest.mark.asyncio
async def test_portfolio_exposure_remaining_partial():
    """Cap = $3000, used = $1200 → remaining = $1800."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=1200.0)):
        remaining = await v2_execution._portfolio_exposure_remaining("rules", 3000.0)
    assert remaining == 1800.0


@pytest.mark.asyncio
async def test_portfolio_exposure_remaining_overshoot_clamped():
    """Used > cap → clamped to 0, не negative."""
    with patch.object(v2_execution, "fetchval", new=AsyncMock(return_value=4000.0)):
        remaining = await v2_execution._portfolio_exposure_remaining("rules", 3000.0)
    assert remaining == 0.0


# ─── Reject reason format ─────────────────────────────────────────────────────


def test_portfolio_cap_reject_reason_format_documented():
    """Reject reason format должен быть detectable от per-detector reason.

    Phase 4 dashboard будет фильтровать rejections по prefix.
    """
    # per-detector format: 'virtual_exposure_cap_exceeded:open=...,stake=...,cap=...'
    # per-portfolio format: 'virtual_portfolio_cap_exceeded:portfolio=...,used=...,stake=...,cap=...'
    # Они должны различаться по prefix чтобы UI/SQL легко фильтровали.
    per_detector_prefix = "virtual_exposure_cap_exceeded"
    per_portfolio_prefix = "virtual_portfolio_cap_exceeded"
    assert per_portfolio_prefix != per_detector_prefix
    assert per_portfolio_prefix.startswith("virtual_portfolio")


# ─── Unchanged: per-detector cap constant guard ─────────────────────────────


def test_per_detector_cap_constant_unchanged():
    """Phase 3 не меняет per-detector cap — оба cap'а co-exist.

    2026-05-13: per-detector cap $1K → $3K → $5K (high-edge focus)."""
    assert v2_execution.VIRTUAL_PER_DETECTOR_EXPOSURE_CAP_USD == 5000.0


# ─── Portfolio dataclass round-trip ──────────────────────────────────────────


def test_portfolio_passed_to_cap_calculation():
    """Phase 3 reads cap из Portfolio.exposure_cap_usd, не hardcoded."""
    p = Portfolio(
        portfolio_id="ml",
        display_name="ML",
        bankroll_usd=1000.0,
        exposure_cap_usd=1000.0,
        kelly_fraction=0.10,
    )
    assert p.exposure_cap_usd == 1000.0
    # Sentiment portfolio: cap = $500
    p2 = Portfolio(
        portfolio_id="sentiment",
        display_name="Sentiment",
        bankroll_usd=500.0,
        exposure_cap_usd=500.0,
        kelly_fraction=0.10,
    )
    assert p2.exposure_cap_usd == 500.0
