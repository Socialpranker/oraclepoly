"""Net-EV гейт: category/fee_schedule доходят до size_from_calibrated_p (audit 2026-07-02, топ-2).

Контекст: v2_execution.py вызывал size_from_calibrated_p БЕЗ category → в ev_sizer.py
ветка `if category is not None:` (net-EV: ev_pct - fee_drag - spread_drag >= min_edge +
EDGE_BUFFER_PCT) была мертва, гейт работал по gross EV. fee_schedule есть в markets,
но не выбирался в _FETCH_FRESH_SIGNALS_SQL.

Эталонный паттерн БД-мока для _process_signal — tests/test_v2_execution_portfolio_kelly.py
(patch.object(v2_execution, <имя>, ...) на именах, импортированных в модуль v2_execution;
fetchval/fetchrow напрямую НЕ перехватывают вложенные вызовы get_detector_state/
fetch_portfolio_for_detector — те резолвятся через свои собственные module-level fetchrow).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.calibration.ev_sizer import SizingDecision, size_from_calibrated_p
from app.jobs import v2_execution


def test_net_ev_path_rejects_when_costs_eat_edge():
    # EV per $1 stake = p * payout - (1 - p), где payout = ЧИСТАЯ прибыль = 1/entry - 1.
    # p=0.62, entry_price=0.50 → gross EV = 0.62*1.0 - 0.38 = 24.0% (проходит min_edge=20%),
    # но sports fee_drag=3% + spread_drag=2% + EDGE_BUFFER_PCT=2% → net=19.0% < 22% порог.
    #
    # Было p=0.415: под старой формулой (payout = валовой 1/entry) это давало gross
    # «24.5%», а на самом деле −17%. Фикстура держалась на баге, исправленном
    # 2026-08-10 — см. research/favorite-buying-audit-2026-08-10.md §2.4.
    decision = size_from_calibrated_p(
        calibrated_p=0.62,
        entry_price=0.50,
        bankroll_usd=1000.0,
        category="sports",
    )
    assert decision.stake_usd is None
    assert "net" in decision.reason


def test_gross_path_unchanged_without_category():
    # Тот же (p, entry_price) БЕЗ category — net-EV ветка мертва, гейт по gross EV
    # (24.0% >= 20% min_edge) — проходит, в отличие от net-EV ветки выше.
    decision_no_cat = size_from_calibrated_p(
        calibrated_p=0.62,
        entry_price=0.50,
        bankroll_usd=1000.0,
    )
    assert decision_no_cat.stake_usd is not None  # gross EV 24.0% >= 20%


def _make_signal():
    return {
        "detector": "D3_cross_mkt_arb",
        "detector_signal_id": "00000000-0000-0000-0000-000000000001",
        "market_id": "0xMARKET",
        "event_id": "0xEVENT",
        "side": "NO",
        "entry_price": 0.30,
        "calibrated_p": 0.40,
        "meta_p": 0.40,
        "category": "crypto",
        "fee_schedule": None,
        "token_id": "0xTOKEN",
        "generated_at": None,
        "market_end_date": None,
    }


def _make_state(stage="virtual", kelly=0.10):
    state = MagicMock()
    state.stage = stage
    state.fractional_kelly_today = kelly
    state.detector = "D3_cross_mkt_arb"
    state.portfolio_id = "ml"
    return state


@pytest.mark.asyncio
async def test_process_signal_passes_category_to_sizer():
    """_process_signal должен прокидывать sig['category'] в size_from_calibrated_p."""
    sig = _make_signal()
    state = _make_state()

    captured: dict = {}

    def fake_sizer(**kwargs):
        captured.update(kwargs)
        return SizingDecision(None, "test_stop", 0.0, kwargs["calibrated_p"], kwargs["entry_price"])

    with (
        patch.object(v2_execution, "get_detector_state", new=AsyncMock(return_value=state)),
        patch.object(v2_execution, "fetch_portfolio_for_detector", new=AsyncMock(return_value=None)),
        patch.object(v2_execution, "size_from_calibrated_p", side_effect=fake_sizer),
        patch.object(v2_execution, "_event_reentry_blocked", new=AsyncMock(return_value=False)),
        patch.object(v2_execution, "get_settings") as mock_settings,
    ):
        mock_settings.return_value.execution_enabled = False
        await v2_execution._process_signal(sig)

    assert captured.get("category") == "crypto"


@pytest.mark.asyncio
async def test_process_signal_passes_fee_schedule_to_sizer():
    """_process_signal должен прокидывать sig['fee_schedule'] (уже dict, не str) в sizer."""
    sig = _make_signal()
    sig["fee_schedule"] = {"maker_bps": 0, "taker_bps": 200}
    state = _make_state()

    captured: dict = {}

    def fake_sizer(**kwargs):
        captured.update(kwargs)
        return SizingDecision(None, "test_stop", 0.0, kwargs["calibrated_p"], kwargs["entry_price"])

    with (
        patch.object(v2_execution, "get_detector_state", new=AsyncMock(return_value=state)),
        patch.object(v2_execution, "fetch_portfolio_for_detector", new=AsyncMock(return_value=None)),
        patch.object(v2_execution, "size_from_calibrated_p", side_effect=fake_sizer),
        patch.object(v2_execution, "_event_reentry_blocked", new=AsyncMock(return_value=False)),
        patch.object(v2_execution, "get_settings") as mock_settings,
    ):
        mock_settings.return_value.execution_enabled = False
        await v2_execution._process_signal(sig)

    assert captured.get("fee_schedule") == {"maker_bps": 0, "taker_bps": 200}
