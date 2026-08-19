"""Тесты bounded-executor L2 (Phase 3): whitelist + три адаптера."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.analytics.action_gates import KIND_PAUSE_DETECTOR, KIND_REDUCE_CAP
from app.services.action_executor import execute_action


@pytest.mark.asyncio
async def test_unknown_kind_never_reaches_adapter():
    """Whitelist §2 п.3: неизвестный kind отклоняется, побочных эффектов нет."""
    with patch("app.services.action_executor.execute", new=AsyncMock()) as ex:
        res = await execute_action("drop_database", {"detector": "D3"})
    assert res.ok is False
    assert res.error == "unknown_kind"
    ex.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_detector_calls_primitive():
    with patch("app.services.action_executor.execute", new=AsyncMock(return_value="UPDATE 1")) as ex:
        res = await execute_action(KIND_PAUSE_DETECTOR, {"detector": "D3_cross_mkt_arb", "reason": "advisory L2"})
    assert res.ok is True
    assert res.effect["detector"] == "D3_cross_mkt_arb"
    assert res.effect["new_stage"] == "paused"
    assert ex.await_count == 1


@pytest.mark.asyncio
async def test_pause_detector_fails_when_detector_missing():
    """UPDATE 0 = детектора нет. Молчаливый ложный успех недопустим:
    владелец увидел бы «Сделано», хотя ничего не изменилось."""
    with patch("app.services.action_executor.execute", new=AsyncMock(return_value="UPDATE 0")):
        res = await execute_action(KIND_PAUSE_DETECTOR, {"detector": "D_ghost", "reason": "x"})
    assert res.ok is False
    assert res.error == "detector_not_found"


@pytest.mark.asyncio
async def test_reduce_cap_updates_and_reports_effect():
    with patch("app.services.action_executor.execute", new=AsyncMock(return_value="UPDATE 1")) as ex:
        res = await execute_action(KIND_REDUCE_CAP, {"detector": "D3_cross_mkt_arb", "new_cap_usd": 5.0})
    assert res.ok is True
    assert res.effect["new_cap_usd"] == 5.0
    assert ex.await_count == 1


@pytest.mark.asyncio
async def test_reduce_cap_fails_when_no_row_updated():
    """UPDATE 0 = детектора нет. Молчаливого 'успеха' быть не должно."""
    with patch("app.services.action_executor.execute", new=AsyncMock(return_value="UPDATE 0")):
        res = await execute_action(KIND_REDUCE_CAP, {"detector": "D_ghost", "new_cap_usd": 5.0})
    assert res.ok is False
    assert res.error == "detector_not_found"


@pytest.mark.asyncio
async def test_reduce_cap_rejects_non_positive():
    """Отрицательный/нулевой cap — не «урезание», а поломка сайзинга."""
    with patch("app.services.action_executor.execute", new=AsyncMock()) as ex:
        res = await execute_action(KIND_REDUCE_CAP, {"detector": "D3", "new_cap_usd": 0})
    assert res.ok is False
    assert res.error == "invalid_cap"
    ex.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_position_is_not_executable_here():
    """Необратимое закрытие позиции НЕ входит в whitelist Phase 3 (решение владельца).

    У policy engine нет order_db_id, значит потребителя у адаптера не было бы.
    Закрытие живёт в контуре close_pos: (аудит — задача 5). Этот тест стережёт,
    чтобы адаптер не «вернулся» незаметно.
    """
    res = await execute_action("close_position", {"order_db_id": "ord-1"})
    assert res.ok is False
    assert res.error == "unknown_kind"


@pytest.mark.asyncio
async def test_adapter_exception_becomes_failed_not_crash():
    """Падение адаптера → ok=False с текстом, а не проброс исключения в вебхук."""
    with patch("app.services.action_executor.execute", new=AsyncMock(side_effect=RuntimeError("db down"))):
        res = await execute_action(KIND_PAUSE_DETECTOR, {"detector": "D3", "reason": "x"})
    assert res.ok is False
    assert "db down" in (res.error or "")


@pytest.mark.asyncio
async def test_missing_payload_key_is_reported_not_raised():
    res = await execute_action(KIND_PAUSE_DETECTOR, {"reason": "нет detector"})
    assert res.ok is False
    assert res.error == "bad_payload"


@pytest.mark.asyncio
async def test_reduce_cap_rejects_nan_and_inf():
    """float('nan') <= 0 и float('inf') <= 0 оба False — проверки `<= 0` мало.

    Без isfinite такие значения молча уехали бы в live_stake_cap_usd и испортили
    сайзинг (находка ревью Task 2).
    """
    for bad in ("nan", "inf", "-inf"):
        with patch("app.services.action_executor.execute", new=AsyncMock()) as ex:
            res = await execute_action(KIND_REDUCE_CAP, {"detector": "D3", "new_cap_usd": bad})
        assert res.ok is False, f"{bad} прошёл как валидный cap"
        assert res.error == "invalid_cap"
        ex.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_string_detector_is_bad_payload():
    """detector нестрокового типа отклоняется с внятным кодом, а не падает на границе asyncpg."""
    for bad in ({"nested": "dict"}, ["list"], None, ""):
        with patch("app.services.action_executor.execute", new=AsyncMock()) as ex:
            res = await execute_action(KIND_PAUSE_DETECTOR, {"detector": bad, "reason": "x"})
        assert res.ok is False
        assert res.error == "bad_payload"
        ex.assert_not_awaited()
