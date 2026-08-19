"""Тесты taker flow sync job (сохраняется имя clob_book_sync для совместимости)."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.jobs.clob_book_sync import (
    ClobBookSync,
    compute_taker_bias,
)

# ---------- Pure math: compute_taker_bias ----------


def test_compute_taker_bias_token0_heavy():
    """BUY преимущественно на token0 (outcomeIndex=0) → bias > 0."""
    trades = [
        {"side": "BUY", "outcomeIndex": 0, "size": 100, "price": 0.70},
        {"side": "BUY", "outcomeIndex": 0, "size": 50, "price": 0.75},
        {"side": "BUY", "outcomeIndex": 1, "size": 10, "price": 0.30},
    ]
    bias, total, n = compute_taker_bias(trades)
    assert bias > 0.5  # сильно смещён в сторону token0
    assert total > 0
    assert n == 3


def test_compute_taker_bias_token1_heavy():
    """BUY на token1 (NO) → bias < 0."""
    trades = [
        {"side": "BUY", "outcomeIndex": 1, "size": 500, "price": 0.80},
        {"side": "BUY", "outcomeIndex": 0, "size": 10, "price": 0.20},
    ]
    bias, total, n = compute_taker_bias(trades)
    assert bias < -0.5


def test_compute_taker_bias_balanced():
    """Равные BUY по обеим сторонам → bias ≈ 0."""
    trades = [
        {"side": "BUY", "outcomeIndex": 0, "size": 100, "price": 0.50},
        {"side": "BUY", "outcomeIndex": 1, "size": 100, "price": 0.50},
    ]
    bias, total, n = compute_taker_bias(trades)
    assert abs(bias) < 0.01
    assert total == 100.0  # 50+50


def test_compute_taker_bias_sells_ignored():
    """SELL трейды не учитываются в bias."""
    trades = [
        {"side": "SELL", "outcomeIndex": 0, "size": 10000, "price": 0.80},
        {"side": "BUY", "outcomeIndex": 0, "size": 10, "price": 0.50},
    ]
    bias, total, n = compute_taker_bias(trades)
    assert bias == 1.0  # только BUY на token0
    assert n == 1  # только 1 BUY


def test_compute_taker_bias_empty():
    bias, total, n = compute_taker_bias([])
    assert bias == 0.0
    assert total == 0.0
    assert n == 0


def test_compute_taker_bias_malformed_skipped():
    """Трейды с некорректными полями пропускаются."""
    trades = [
        {"side": "BUY", "outcomeIndex": 0, "size": "bad", "price": 0.50},
        {"side": "BUY", "outcomeIndex": 0, "size": 100, "price": 0.50},
    ]
    bias, total, n = compute_taker_bias(trades)
    assert bias == 1.0  # один валидный BUY на token0
    assert total == 50.0


# ---------- ClobBookSync orchestrator ----------


@pytest.mark.asyncio
async def test_sync_skips_markets_without_condition_id():
    """Если у рынка нет condition_id — repo.upsert не вызывается."""
    repo = MagicMock()
    repo.upsert = AsyncMock()

    sync = ClobBookSync(repo=repo)
    sync._fetch_top_markets = AsyncMock(return_value=[{"id": uuid4(), "condition_id": None, "clob_token_ids": ["tok"]}])
    sync._fetch_trades = AsyncMock(return_value=[])
    sync._fetch_previous_ratios = AsyncMock(return_value={})

    await sync.run()
    repo.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_sync_skips_low_volume_markets():
    """Рынки с объёмом BUY < $25 фильтруются как noise."""
    repo = MagicMock()
    repo.upsert = AsyncMock()

    sync = ClobBookSync(repo=repo)
    sync._fetch_top_markets = AsyncMock(
        return_value=[{"id": uuid4(), "condition_id": "0xabc", "clob_token_ids": ["tok"]}]
    )
    # всего $10 BUY объёма — ниже порога $25
    sync._fetch_trades = AsyncMock(
        return_value=[
            {"side": "BUY", "outcomeIndex": 0, "size": 10, "price": 0.50},
            {"side": "BUY", "outcomeIndex": 1, "size": 10, "price": 0.50},
        ]
    )
    sync._fetch_previous_ratios = AsyncMock(return_value={})

    await sync.run()
    repo.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_sync_processes_market_and_upserts():
    """Активный рынок с достаточным BUY объёмом → upsert вызван."""
    market_id = uuid4()
    repo = MagicMock()
    repo.upsert = AsyncMock()

    sync = ClobBookSync(repo=repo)
    sync._fetch_top_markets = AsyncMock(
        return_value=[
            {
                "id": market_id,
                "condition_id": "0xdef",
                "clob_token_ids": ["tok-yes", "tok-no"],
            }
        ]
    )
    sync._fetch_previous_ratios = AsyncMock(return_value={})
    # Token0 доминирует: 300 USD BUY vs 50 USD BUY
    sync._fetch_trades = AsyncMock(
        return_value=[
            {"side": "BUY", "outcomeIndex": 0, "size": 500, "price": 0.60},  # 300 USD
            {"side": "BUY", "outcomeIndex": 1, "size": 100, "price": 0.50},  # 50 USD
            {"side": "SELL", "outcomeIndex": 1, "size": 1000, "price": 0.50},  # ignored
        ]
    )

    await sync.run()

    repo.upsert.assert_called_once()
    kwargs = repo.upsert.call_args.kwargs
    assert kwargs["market_id"] == market_id
    assert kwargs["condition_id"] == "0xdef"
    assert kwargs["maker_direction_ratio"] > 0.5  # token0-heavy taker flow
    assert kwargs["bid_depth_usd"] == 350.0  # total BUY volume
    assert kwargs["maker_count"] == 2  # 2 BUY trades processed
    assert kwargs["depth_imbalance"] is None
