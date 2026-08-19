"""_flush_prices_once: при ошибке записи батч возвращается в _dirty_prices,
свежие тики (пришедшие во время неудачной записи) имеют приоритет.

До фикса батч обнулялся ДО подтверждения записи — при падении executemany
30-секундное окно цен терялось безвозвратно (аудит пайплайна 2026-07-05).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.polymarket_ws_service import PolymarketWebSocketService


def _svc() -> PolymarketWebSocketService:
    # Без __init__: не поднимаем сеть/Redis, нужен только _dirty_prices.
    svc = PolymarketWebSocketService.__new__(PolymarketWebSocketService)
    svc._dirty_prices = {}
    return svc


@pytest.mark.asyncio
async def test_flush_failure_requeues_batch_new_ticks_win():
    svc = _svc()
    svc._dirty_prices = {"mkt-a": 0.40, "mkt-b": 0.55}

    async def failing_executemany(sql, rows):
        # имитируем тик, пришедший ПОКА запись падала
        svc._dirty_prices["mkt-a"] = 0.42
        raise RuntimeError("db down")

    with patch("app.services.database.executemany", new=failing_executemany):
        await svc._flush_prices_once()

    # mkt-a: свежий тик 0.42 победил, mkt-b: восстановлен из упавшего батча
    assert svc._dirty_prices == {"mkt-a": 0.42, "mkt-b": 0.55}


@pytest.mark.asyncio
async def test_flush_success_clears_batch():
    svc = _svc()
    svc._dirty_prices = {"mkt-a": 0.40}

    ok = AsyncMock()
    with patch("app.services.database.executemany", new=ok):
        await svc._flush_prices_once()

    assert svc._dirty_prices == {}
    ok.assert_awaited_once()


@pytest.mark.asyncio
async def test_flush_noop_on_empty_batch():
    svc = _svc()

    ok = AsyncMock()
    with patch("app.services.database.executemany", new=ok):
        await svc._flush_prices_once()

    ok.assert_not_awaited()
