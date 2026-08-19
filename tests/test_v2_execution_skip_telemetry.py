"""Тест: при skip по sizing логируется decision.reason (телеметрия для fee×2-аудита).

Контекст: decision.reason (готовая разбивка ev_below_threshold_net (net=... gross=...
fee=... spread=...)) терялась при skip — эффект завышенного fee×2 (ev_sizer.py:101)
на отклонение прибыльных сигналов был неизмерим. Покрываем оба call-site
size_from_calibrated_p в v2_execution.py: _process_signal (single-leg) и
_process_basket_signal (D29/D40 basket-путь).

Паттерн БД-мока — tests/test_v2_execution_net_ev.py (patch.object на именах,
импортированных в модуль v2_execution).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.calibration.ev_sizer import SizingDecision
from app.jobs import v2_execution

SKIP_REASON = "ev_below_threshold_net (net=1.00% gross=25.00% fee=20.00% spread=4.00% min=22.00%)"


def _make_signal(**overrides):
    sig = {
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
    sig.update(overrides)
    return sig


def _make_state(stage="virtual", kelly=0.10):
    state = MagicMock()
    state.stage = stage
    state.fractional_kelly_today = kelly
    state.detector = "D3_cross_mkt_arb"
    state.portfolio_id = "ml"
    return state


@pytest.mark.asyncio
async def test_process_signal_logs_reason_on_sizing_skip():
    """_process_signal: sizer вернул stake=None с reason → logger.info с detector+reason."""
    sig = _make_signal()
    state = _make_state()
    skip_decision = SizingDecision(None, SKIP_REASON, 1.0, 0.5, 0.4)

    with (
        patch.object(v2_execution, "get_detector_state", new=AsyncMock(return_value=state)),
        patch.object(v2_execution, "fetch_portfolio_for_detector", new=AsyncMock(return_value=None)),
        patch.object(v2_execution, "size_from_calibrated_p", return_value=skip_decision),
        patch.object(v2_execution, "_event_reentry_blocked", new=AsyncMock(return_value=False)),
        patch.object(v2_execution, "get_settings") as mock_settings,
        patch.object(v2_execution, "logger") as mock_logger,
    ):
        mock_settings.return_value.execution_enabled = False
        result = await v2_execution._process_signal(sig)

    assert result == "skipped"
    mock_logger.info.assert_any_call(
        "v2_exec_sizing_skipped",
        detector="D3_cross_mkt_arb",
        reason=SKIP_REASON,
    )


@pytest.mark.asyncio
async def test_process_basket_signal_logs_reason_on_sizing_skip():
    """_process_basket_signal: sizer вернул stake=None с reason → logger.info с detector+reason."""
    sig = _make_signal(
        detector="D29_negrisk_arb",
        evidence={
            "basket_markets": [
                {"clob_token_ids": "0xLEG1", "yes_price": 0.4},
                {"clob_token_ids": "0xLEG2", "yes_price": 0.3},
            ]
        },
    )
    state = _make_state(stage="virtual")
    state.detector = "D29_negrisk_arb"
    skip_decision = SizingDecision(None, SKIP_REASON, 1.0, 0.5, 0.4)

    with (
        patch.object(v2_execution, "get_detector_state", new=AsyncMock(return_value=state)),
        patch.object(v2_execution, "fetch_portfolio_for_detector", new=AsyncMock(return_value=None)),
        patch.object(v2_execution, "size_from_calibrated_p", return_value=skip_decision),
        patch.object(v2_execution, "get_settings") as mock_settings,
        patch.object(v2_execution, "logger") as mock_logger,
    ):
        mock_settings.return_value.execution_enabled = False
        mock_settings.return_value.d29_basket_enabled = True
        result = await v2_execution._process_basket_signal(sig)

    assert result == "skipped"
    mock_logger.info.assert_any_call(
        "v2_exec_sizing_skipped",
        detector="D29_negrisk_arb",
        reason=SKIP_REASON,
    )
