"""V2 Execution — temperature-блэклист (question-pattern гейт).

Эмпирика 2026-07-07 (memory temperature-markets-adverse-selection-2026-07-07):
virtual-итог всех детекторов на temperature-рынках — 206 ставок, $79.8k stake,
net −$12,328 (ROI −15.4%), в минусе ВСЕ кроме шумового D3 (+2% на 43).
Структурная причина: резолв по METAR конкретной метеостанции — контрагент
с публичным метео-фидом знает исход раньше любого price/flow-сигнала.

Почему question-pattern, а не category: все 28.7K temperature-рынков лежат
ВНУТРИ категории World & Economy — категорийный блэклист их не видит.

Контракт _is_temperature_market(question):
  - вопрос с 'temperature' / '°F' / '°C' → True
  - обычный вопрос → False
  - None / '' → False (fail-open при missing data)

Контракт гейта в _process_signal:
  - direction-детектор × temperature-рынок → rejected + shadow-телеметрия
  - D3_cross_mkt_arb исключён (структурный арбитраж, temp-результат не в минусе)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs import v2_execution
from app.jobs.v2_execution import (
    _TEMPERATURE_GATE_EXEMPT,
    _is_temperature_market,
)

# --- _is_temperature_market -----------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Will the highest temperature in Amsterdam be 22°C on July 9?",
        "Will the lowest temperature in Hong Kong be 28°C on June 23?",
        "Will the highest temperature in Miami be between 88-89°F on July 3?",
        "Will the highest temperature in NYC be 65°F or below on July 7?",
        "Will global temperature increase by between 1.5ºC and 2.0ºC in June 2026?",
    ],
)
def test_temperature_questions_matched(question: str):
    assert _is_temperature_market(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "Will Bitcoin reach $150,000 by July 31?",
        "Will the Lakers win the 2027 NBA Finals?",
        "Fed decision in September?",
    ],
)
def test_regular_questions_not_matched(question: str):
    assert _is_temperature_market(question) is False


def test_none_question_safe():
    """question=None (LEFT JOIN не нашёл рынок / basket) — не падаем, не блокируем."""
    assert _is_temperature_market(None) is False


def test_empty_question_safe():
    assert _is_temperature_market("") is False


def test_d3_exempt():
    """D3 — структурный арбитраж, та же логика что вынос из Sports-блэклиста 2026-07-05."""
    assert "D3_cross_mkt_arb" in _TEMPERATURE_GATE_EXEMPT


# --- гейт в _process_signal ------------------------------------------------

_TEMP_QUESTION = "Will the highest temperature in Amsterdam be 22°C on July 9?"


def _make_sig(detector: str, question: str | None) -> dict:
    return {
        "detector": detector,
        "detector_signal_id": "00000000-0000-0000-0000-00000000temp",
        "market_id": "0xMARKET",
        "event_id": "0xEVENT",
        "side": "YES",
        "category": "World & Economy",
        "question": question,
        "entry_price": 0.42,
        "calibrated_p": 0.55,
        "meta_p": 0.55,
        "token_id": "0xTOKEN",
    }


@pytest.mark.asyncio
async def test_direction_detector_temp_market_rejected():
    """LGB × temperature-рынок → rejected c reason temperature_blacklist + shadow."""
    inserted: list[dict] = []

    async def capture(order: dict) -> str:
        inserted.append(order)
        return "order-1"

    with (
        patch.object(v2_execution, "_insert_order", new=AsyncMock(side_effect=capture)),
        patch.object(v2_execution, "_try_claim_signal", new=AsyncMock(return_value=True)),
        patch(
            "app.services.gate_shadow_telemetry.log_gate_shadow", new=AsyncMock()
        ) as mock_shadow,
    ):
        result = await v2_execution._process_signal(_make_sig("STANDALONE_V2_LGB", _TEMP_QUESTION))

    assert result == "rejected"
    assert len(inserted) == 1
    assert inserted[0]["rejection_reason"] == "temperature_blacklist"
    assert inserted[0]["execution_mode"] == "rejected"
    mock_shadow.assert_awaited_once()


@pytest.mark.asyncio
async def test_d38_temp_market_hits_category_blacklist_first():
    """D38 × temperature (W&E) режется КАТЕГОРИЙНЫМ блэклистом раньше — порядок гейтов."""
    inserted: list[dict] = []

    async def capture(order: dict) -> str:
        inserted.append(order)
        return "order-1"

    with (
        patch.object(v2_execution, "_insert_order", new=AsyncMock(side_effect=capture)),
        patch.object(v2_execution, "_try_claim_signal", new=AsyncMock(return_value=True)),
        patch("app.services.gate_shadow_telemetry.log_gate_shadow", new=AsyncMock()),
    ):
        result = await v2_execution._process_signal(_make_sig("D38_resolution_lag", _TEMP_QUESTION))

    assert result == "rejected"
    assert inserted[0]["rejection_reason"] == "category_blacklist:World & Economy"
