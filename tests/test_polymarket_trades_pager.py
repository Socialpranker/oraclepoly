"""Tests для fetch_trades_since.

Корень бага инцидента 2026-05-24: Polymarket `/trades?before=ts` отдаёт trades
с `timestamp <= ts` (inclusive). Старый paging выставлял `before = min_ts`
очередной страницы → cursor застревал на границе с одинаковыми timestamps →
infinite loop до runaway-cap, ~20 redundant requests на market.

Эти тесты фиксируют поведение pager'а: cursor двигается на min_ts - 1,
safety break срабатывает если по какой-то причине не сдвинулся.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import polymarket_trades_pager as pager


@pytest.mark.asyncio
async def test_empty_response_breaks_immediately() -> None:
    fake_page = AsyncMock(return_value=[])
    with patch.object(pager, "_fetch_one_page", fake_page):
        result = await pager.fetch_trades_since(
            client=AsyncMock(),
            condition_id="0xabc",
            since_ts=0,
        )

    assert result == []
    assert fake_page.await_count == 1


@pytest.mark.asyncio
async def test_single_page_breaks_when_reached_since_ts() -> None:
    page = [
        {"timestamp": 1000, "side": "BUY", "size": 1, "price": 0.5},
        {"timestamp": 900, "side": "BUY", "size": 1, "price": 0.5},
        {"timestamp": 500, "side": "BUY", "size": 1, "price": 0.5},  # старее since_ts
    ]
    fake_page = AsyncMock(return_value=page)
    with patch.object(pager, "_fetch_one_page", fake_page):
        result = await pager.fetch_trades_since(
            client=AsyncMock(),
            condition_id="0xabc",
            since_ts=800,
        )

    # Только timestamp > 800 попадают в результат.
    assert [t["timestamp"] for t in result] == [1000, 900]
    # Cursor не двигается дальше — 1 запрос.
    assert fake_page.await_count == 1


@pytest.mark.asyncio
async def test_cursor_decrements_by_one_after_each_page() -> None:
    """Главный регресс-тест: `before = min_ts - 1`, не `min_ts`."""
    page_one = [{"timestamp": ts, "side": "BUY", "size": 1, "price": 0.5} for ts in (1000, 950, 900)]
    page_two: list[dict] = []  # empty → break
    fake_page = AsyncMock(side_effect=[page_one, page_two])

    with patch.object(pager, "_fetch_one_page", fake_page):
        await pager.fetch_trades_since(
            client=AsyncMock(),
            condition_id="0xabc",
            since_ts=0,
        )

    assert fake_page.await_count == 2
    second_call_params = fake_page.await_args_list[1].args[1]
    # min_ts на первой странице = 900 → before = 899 (exclusive).
    assert second_call_params["before"] == 899


@pytest.mark.asyncio
async def test_safety_break_when_cursor_does_not_advance() -> None:
    """Если по какой-то причине new_before >= old_before — выходим, не loop'имся.

    Это второй слой защиты на случай если API вернёт страницу с min_ts >= before
    (например, дубликат от cache). До инцидента такой защиты не было и runaway
    спасал только page-cap = 20.
    """
    # Каждая страница возвращает trades с одинаковым min_ts=500. Без safety
    # будет 20 запросов; с safety — 2 (1-я + safety-bail).
    repeating_page = [{"timestamp": 500, "side": "BUY", "size": 1, "price": 0.5}] * 3
    fake_page = AsyncMock(return_value=repeating_page)
    with patch.object(pager, "_fetch_one_page", fake_page):
        await pager.fetch_trades_since(
            client=AsyncMock(),
            condition_id="0xabc",
            since_ts=0,
            max_pages=20,
        )

    # 1 первая страница + 1 со сдвинутым before — потом safety break.
    # (Iteration 2: before=499, тот же respond, new_before=499 >= 499 → break.)
    assert fake_page.await_count == 2


@pytest.mark.asyncio
async def test_max_pages_cap_respected() -> None:
    """Runaway cap всё ещё работает даже если safety break почему-то промахнётся."""
    # Page N возвращает trades с min_ts = (initial - N), cursor движется на 1.
    counter = {"i": 0}

    async def descending_page(_client, params, _timeout):
        counter["i"] += 1
        before = params.get("before", 1000)
        # 3 trades, min_ts = before, после фильтрации before/cursor сдвинется на 1.
        return [
            {"timestamp": before, "side": "BUY", "size": 1, "price": 0.5},
            {"timestamp": before, "side": "BUY", "size": 1, "price": 0.5},
        ]

    with patch.object(pager, "_fetch_one_page", side_effect=descending_page):
        await pager.fetch_trades_since(
            client=AsyncMock(),
            condition_id="0xabc",
            since_ts=0,
            max_pages=5,
        )

    # 1 первая + safety break срабатывает на 2-й (new_before=before-1 < before, OK,
    # но на 3-й min_ts=before-1, new=before-2 < before-1 — progress). На этом
    # шаблоне safety не выстреливает; должны упереться в cap=5.
    assert counter["i"] == 5
