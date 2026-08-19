"""Claim-гейт против дубля daemon-vs-cron (fix 2026-07-03).

Измерено 2026-07-03 12:18: сигнал f661c2f2 получил ДВА rejected-ордера с
разницей 0.5с — execution_daemon и минутный cron обработали его параллельно.
SQL-дедуп (partial UNIQUE uq_execution_orders_signal_active + NOT EXISTS в
выборке) не видит незакоммиченный INSERT соседа и не покрывает rejected-строки.
Атомарный Redis SET NX закрывает окно; SQL остаётся второй линией.

Дубли rejected у live-детектора кормят anomaly-счётчик risk_daemon → ложный
pause/kill (класс инцидента D38 2026-06-29).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_try_claim_first_wins_second_loses():
    from app.jobs.v2_execution import _try_claim_signal

    redis = AsyncMock()
    redis.set = AsyncMock(side_effect=[True, None])  # SET NX: второй вызов → None
    with patch("app.core.cache.get_redis", AsyncMock(return_value=redis)):
        assert await _try_claim_signal("sig-1") is True
        assert await _try_claim_signal("sig-1") is False

    _, kwargs = redis.set.call_args
    assert kwargs.get("nx") is True
    # TTL короче retry-окна транзиентных reject'ов (180с), иначе блокирует ретраи
    assert 0 < kwargs.get("ex") < 180


@pytest.mark.asyncio
async def test_try_claim_fail_open_when_redis_unavailable():
    """Redis down → дубль дешевле остановки всего execution."""
    from app.jobs.v2_execution import _try_claim_signal

    with patch("app.core.cache.get_redis", AsyncMock(return_value=None)):
        assert await _try_claim_signal("sig-1") is True

    with patch("app.core.cache.get_redis", AsyncMock(side_effect=RuntimeError("boom"))):
        assert await _try_claim_signal("sig-1") is True


@pytest.mark.asyncio
async def test_process_signal_returns_duplicate_when_claim_lost():
    """Проигравший гонку путь (daemon или cron) выходит ДО любых гейтов и insert'ов."""
    from app.jobs import v2_execution

    sig = {"detector": "D3_cross_mkt_arb", "detector_signal_id": "sig-race-1"}
    with patch.object(v2_execution, "_try_claim_signal", AsyncMock(return_value=False)):
        result = await v2_execution._process_signal(sig)

    assert result == "duplicate"
