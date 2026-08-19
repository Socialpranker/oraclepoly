"""V2 Execution _process_signal — bankroll/kelly из portfolio (Phase 3).

Проверяем что:
- В virtual mode bankroll берётся из Portfolio.bankroll_usd, не SHADOW_BANKROLL_USD
- В virtual mode kelly fraction берётся из Portfolio.kelly_fraction, не из detector_state
- Если portfolio missing (transient state) → fallback к старому поведению
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.calibration.ev_sizer import SizingDecision
from app.jobs import v2_execution
from app.services.portfolios import Portfolio


def _make_signal():
    return {
        "detector": "STANDALONE_V2_LGB",
        "detector_signal_id": "00000000-0000-0000-0000-000000000001",
        "market_id": "0xMARKET",
        "event_id": "0xEVENT",
        "side": "NO",
        "entry_price": 0.30,
        # cal_p=0.40 — profitable zone для V2_LGB (avoid 0.50-0.70 blacklist).
        # Эти тесты проверяют portfolio kelly/cap, не cal_p zone gate.
        "calibrated_p": 0.40,
        "meta_p": 0.40,
        "token_id": "0xTOKEN",
        "generated_at": None,
        "market_end_date": None,
    }


def _make_state(stage="virtual", kelly=0.10):
    state = MagicMock()
    state.stage = stage
    state.fractional_kelly_today = kelly
    state.detector = "STANDALONE_V2_LGB"
    state.portfolio_id = "ml"
    return state


def _make_portfolio(pid="ml", bankroll=1000.0, cap=1000.0, kelly=0.10):
    return Portfolio(
        portfolio_id=pid,
        display_name=pid.title(),
        bankroll_usd=bankroll,
        exposure_cap_usd=cap,
        kelly_fraction=kelly,
    )


@pytest.mark.asyncio
async def test_uses_portfolio_kelly_fraction():
    """ML portfolio kelly = 0.10 → size_from_calibrated_p вызывается с 0.10."""
    sig = _make_signal()
    state = _make_state(stage="virtual", kelly=0.99)  # state has different kelly
    portfolio = _make_portfolio(kelly=0.10)

    captured: dict = {}

    def fake_size(*, calibrated_p, entry_price, bankroll_usd, fractional_kelly, **_kwargs):
        captured["fractional_kelly"] = fractional_kelly
        captured["bankroll_usd"] = bankroll_usd
        return SizingDecision(stake_usd=20.0, reason="ok", ev_pct=15.0, p=calibrated_p, entry_price=entry_price)

    with (
        patch.object(v2_execution, "get_detector_state", new=AsyncMock(return_value=state)),
        patch.object(v2_execution, "fetch_portfolio_for_detector", new=AsyncMock(return_value=portfolio)),
        patch.object(v2_execution, "size_from_calibrated_p", side_effect=fake_size),
        patch.object(v2_execution, "_virtual_exposure_remaining", new=AsyncMock(return_value=1000.0)),
        patch.object(v2_execution, "_portfolio_exposure_used", new=AsyncMock(return_value=0.0)),
        patch.object(
            v2_execution, "safety_evaluate", new=AsyncMock(return_value=MagicMock(allowed=False, reason="test_skip"))
        ),
        patch.object(v2_execution, "_insert_order", new=AsyncMock(return_value=None)),
        patch.object(v2_execution, "_event_reentry_blocked", new=AsyncMock(return_value=False)),
        patch.object(v2_execution, "get_settings") as mock_settings,
    ):
        mock_settings.return_value.execution_enabled = False
        await v2_execution._process_signal(sig)

    assert captured["fractional_kelly"] == 0.10  # из portfolio, не state.kelly=0.99
    assert captured["bankroll_usd"] == 1000.0  # portfolio bankroll


@pytest.mark.asyncio
async def test_uses_portfolio_bankroll_3000_for_rules():
    """Rules portfolio bankroll = $3000 — NEW behaviour (vs old SHADOW=$1000)."""
    sig = _make_signal()
    sig["detector"] = "D7b_no_favorite_fade"
    state = _make_state(stage="virtual", kelly=0.20)
    portfolio = _make_portfolio(pid="rules", bankroll=3000.0, cap=3000.0, kelly=0.20)

    captured: dict = {}

    def fake_size(*, calibrated_p, entry_price, bankroll_usd, fractional_kelly, **_kwargs):
        captured["bankroll_usd"] = bankroll_usd
        return SizingDecision(stake_usd=60.0, reason="ok", ev_pct=20.0, p=calibrated_p, entry_price=entry_price)

    with (
        patch.object(v2_execution, "get_detector_state", new=AsyncMock(return_value=state)),
        patch.object(v2_execution, "fetch_portfolio_for_detector", new=AsyncMock(return_value=portfolio)),
        patch.object(v2_execution, "size_from_calibrated_p", side_effect=fake_size),
        patch.object(v2_execution, "_virtual_exposure_remaining", new=AsyncMock(return_value=3000.0)),
        patch.object(v2_execution, "_portfolio_exposure_used", new=AsyncMock(return_value=0.0)),
        patch.object(
            v2_execution, "safety_evaluate", new=AsyncMock(return_value=MagicMock(allowed=False, reason="test_skip"))
        ),
        patch.object(v2_execution, "_insert_order", new=AsyncMock(return_value=None)),
        patch.object(v2_execution, "_event_reentry_blocked", new=AsyncMock(return_value=False)),
        patch.object(v2_execution, "get_settings") as mock_settings,
    ):
        mock_settings.return_value.execution_enabled = False
        await v2_execution._process_signal(sig)

    assert captured["bankroll_usd"] == 3000.0


@pytest.mark.asyncio
async def test_fallback_to_shadow_bankroll_when_portfolio_missing():
    """Если portfolio не найден (transient migration state) → fallback SHADOW_BANKROLL_USD."""
    sig = _make_signal()
    state = _make_state(stage="virtual", kelly=0.15)

    captured: dict = {}

    def fake_size(*, calibrated_p, entry_price, bankroll_usd, fractional_kelly, **_kwargs):
        captured["bankroll_usd"] = bankroll_usd
        captured["fractional_kelly"] = fractional_kelly
        return SizingDecision(stake_usd=20.0, reason="ok", ev_pct=10.0, p=calibrated_p, entry_price=entry_price)

    with (
        patch.object(v2_execution, "get_detector_state", new=AsyncMock(return_value=state)),
        patch.object(v2_execution, "fetch_portfolio_for_detector", new=AsyncMock(return_value=None)),
        patch.object(v2_execution, "size_from_calibrated_p", side_effect=fake_size),
        patch.object(v2_execution, "_virtual_exposure_remaining", new=AsyncMock(return_value=1000.0)),
        patch.object(
            v2_execution, "safety_evaluate", new=AsyncMock(return_value=MagicMock(allowed=False, reason="test_skip"))
        ),
        patch.object(v2_execution, "_insert_order", new=AsyncMock(return_value=None)),
        patch.object(v2_execution, "_event_reentry_blocked", new=AsyncMock(return_value=False)),
        patch.object(v2_execution, "get_settings") as mock_settings,
    ):
        mock_settings.return_value.execution_enabled = False
        await v2_execution._process_signal(sig)

    assert captured["bankroll_usd"] == v2_execution.SHADOW_BANKROLL_USD  # = 1000
    # Kelly fallback to detector_state.fractional_kelly_today = 0.15
    assert captured["fractional_kelly"] == 0.15


@pytest.mark.asyncio
async def test_portfolio_cap_rejects_when_exceeded():
    """Portfolio cap=$1000, used=$990, stake=$20 → reject."""
    sig = _make_signal()
    state = _make_state(stage="virtual")
    portfolio = _make_portfolio(pid="ml", bankroll=1000.0, cap=1000.0, kelly=0.10)

    inserted: dict = {}

    async def capture_insert(payload):
        inserted.update(payload)
        return "test-id"

    def fake_size(*, calibrated_p, entry_price, bankroll_usd, fractional_kelly, **_kwargs):
        return SizingDecision(stake_usd=20.0, reason="ok", ev_pct=10.0, p=calibrated_p, entry_price=entry_price)

    with (
        patch.object(v2_execution, "get_detector_state", new=AsyncMock(return_value=state)),
        patch.object(v2_execution, "fetch_portfolio_for_detector", new=AsyncMock(return_value=portfolio)),
        patch.object(v2_execution, "size_from_calibrated_p", side_effect=fake_size),
        # per-detector cap has room ($1000 - $0 = $1000 remaining > $20)
        patch.object(v2_execution, "_virtual_exposure_remaining", new=AsyncMock(return_value=1000.0)),
        # portfolio cap нарушен: used=$990, cap=$1000, remaining=$10 < $20 stake
        patch.object(v2_execution, "_portfolio_exposure_used", new=AsyncMock(return_value=990.0)),
        patch.object(v2_execution, "_insert_order", side_effect=capture_insert),
        patch.object(v2_execution, "_event_reentry_blocked", new=AsyncMock(return_value=False)),
        patch.object(v2_execution, "get_settings") as mock_settings,
    ):
        mock_settings.return_value.execution_enabled = False
        result = await v2_execution._process_signal(sig)

    assert result == "rejected"
    assert inserted["status"] == "rejected"
    assert inserted["execution_mode"] == "rejected"
    assert "virtual_portfolio_cap_exceeded" in inserted["rejection_reason"]
    assert "portfolio=ml" in inserted["rejection_reason"]
    assert "used=$990" in inserted["rejection_reason"]


@pytest.mark.asyncio
async def test_per_detector_cap_still_enforced_when_portfolio_has_room():
    """Per-detector cap=$1000 проверяется ПЕРЕД portfolio cap (early reject)."""
    sig = _make_signal()
    state = _make_state(stage="virtual")
    portfolio = _make_portfolio(pid="rules", bankroll=3000.0, cap=3000.0, kelly=0.20)

    inserted: dict = {}

    async def capture_insert(payload):
        inserted.update(payload)
        return "test-id"

    def fake_size(*, calibrated_p, entry_price, bankroll_usd, fractional_kelly, **_kwargs):
        return SizingDecision(stake_usd=20.0, reason="ok", ev_pct=10.0, p=calibrated_p, entry_price=entry_price)

    portfolio_used_mock = AsyncMock(return_value=0.0)

    with (
        patch.object(v2_execution, "get_detector_state", new=AsyncMock(return_value=state)),
        patch.object(v2_execution, "fetch_portfolio_for_detector", new=AsyncMock(return_value=portfolio)),
        patch.object(v2_execution, "size_from_calibrated_p", side_effect=fake_size),
        # per-detector cap нарушен: $1000 - $999 = $1 < $20
        patch.object(v2_execution, "_virtual_exposure_remaining", new=AsyncMock(return_value=1.0)),
        # portfolio has room ($3000 cap, $0 used)
        patch.object(v2_execution, "_portfolio_exposure_used", new=portfolio_used_mock),
        patch.object(v2_execution, "_insert_order", side_effect=capture_insert),
        patch.object(v2_execution, "_event_reentry_blocked", new=AsyncMock(return_value=False)),
        patch.object(v2_execution, "get_settings") as mock_settings,
    ):
        mock_settings.return_value.execution_enabled = False
        result = await v2_execution._process_signal(sig)

    assert result == "rejected"
    # Должен сработать per-detector reject (он первый), portfolio_exposure_used не вызывался
    assert "virtual_exposure_cap_exceeded" in inserted["rejection_reason"]
    assert "virtual_portfolio_cap_exceeded" not in inserted["rejection_reason"]
    portfolio_used_mock.assert_not_awaited()
