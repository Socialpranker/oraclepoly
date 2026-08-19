"""Тесты MakerDirectionRepository — CRUD для maker_direction_cache."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.repositories.maker_direction_repository import MakerDirectionRepository

MODULE = "app.repositories.maker_direction_repository"


@pytest.mark.asyncio
async def test_upsert_stores_data():
    market_id = uuid4()
    with patch(f"{MODULE}.execute", new_callable=AsyncMock, return_value="INSERT 0 1") as mock_exec:
        repo = MakerDirectionRepository()
        await repo.upsert(
            market_id=market_id,
            condition_id="0xabc",
            maker_direction_ratio=0.75,
            depth_imbalance=0.3,
            bid_depth_usd=50000.0,
            ask_depth_usd=20000.0,
            maker_count=42,
        )

    mock_exec.assert_called_once()
    call_args = mock_exec.call_args[0]
    assert "INSERT INTO maker_direction_cache" in call_args[0]
    assert "ON CONFLICT (market_id)" in call_args[0]
    # параметры — $1..$7 → market_id идёт первым
    assert market_id in call_args


@pytest.mark.asyncio
async def test_get_by_market_id_returns_row():
    market_id = uuid4()
    fake_row = {
        "market_id": market_id,
        "maker_direction_ratio": 0.65,
        "depth_imbalance": 0.2,
        "fetched_at": None,
    }
    with patch(f"{MODULE}.fetchrow", new_callable=AsyncMock, return_value=fake_row) as mock_fetchrow:
        repo = MakerDirectionRepository()
        result = await repo.get_by_market_id(market_id)

    assert result is not None
    assert result["maker_direction_ratio"] == 0.65
    mock_fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_get_by_market_id_returns_none_when_missing():
    with patch(f"{MODULE}.fetchrow", new_callable=AsyncMock, return_value=None):
        repo = MakerDirectionRepository()
        result = await repo.get_by_market_id(uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_get_stale_returns_markets_older_than_ttl():
    with patch(
        f"{MODULE}.fetch",
        new_callable=AsyncMock,
        return_value=[{"market_id": uuid4(), "maker_direction_ratio": 0.5}],
    ) as mock_fetch:
        repo = MakerDirectionRepository()
        results = await repo.get_stale(ttl_hours=2)

    assert len(results) == 1
    mock_fetch.assert_called_once()


@pytest.mark.asyncio
async def test_get_all_active_returns_fresh_records():
    with patch(
        f"{MODULE}.fetch",
        new_callable=AsyncMock,
        return_value=[
            {"market_id": uuid4(), "condition_id": "0xabc", "maker_direction_ratio": 0.4},
            {"market_id": uuid4(), "condition_id": "0xdef", "maker_direction_ratio": -0.2},
        ],
    ) as mock_fetch:
        repo = MakerDirectionRepository()
        results = await repo.get_all_active()

    assert len(results) == 2
    mock_fetch.assert_called_once()
    query = mock_fetch.call_args[0][0]
    assert "maker_direction_cache" in query
    assert "2 hours" in query
