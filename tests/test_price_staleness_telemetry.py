"""Тесты shadow-телеметрии price staleness."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.price_staleness_telemetry import log_price_staleness


@pytest.mark.asyncio
async def test_logs_delta_when_live_price_available():
    with (
        patch(
            "app.services.price_staleness_telemetry.get_live_yes_price",
            new_callable=AsyncMock,
            return_value=(0.55, True),
        ),
        patch(
            "app.services.price_staleness_telemetry.execute", new_callable=AsyncMock
        ) as mock_execute,
    ):
        await log_price_staleness("sizing", "mkt1", "tok_yes_1", 0.50)

    args = mock_execute.call_args.args
    # $1..$7: point, market_id, yes_token_id, db_price, live_price, is_live, delta_abs
    assert args[1] == "sizing"
    assert args[2] == "mkt1"
    assert args[4] == 0.50
    assert args[5] == 0.55
    assert args[6] is True
    assert args[7] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_logs_null_live_when_ws_cache_empty():
    with (
        patch(
            "app.services.price_staleness_telemetry.get_live_yes_price",
            new_callable=AsyncMock,
            return_value=(0.50, False),  # fallback вернул db_price, is_live=False
        ),
        patch(
            "app.services.price_staleness_telemetry.execute", new_callable=AsyncMock
        ) as mock_execute,
    ):
        await log_price_staleness("bucket_gate", "mkt2", None, 0.50)

    args = mock_execute.call_args.args
    assert args[5] is None      # live_price = NULL
    assert args[6] is False
    assert args[7] is None      # delta_abs = NULL


@pytest.mark.asyncio
async def test_never_raises_on_db_failure():
    with (
        patch(
            "app.services.price_staleness_telemetry.get_live_yes_price",
            new_callable=AsyncMock,
            return_value=(0.55, True),
        ),
        patch(
            "app.services.price_staleness_telemetry.execute",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ),
    ):
        # Телеметрия НЕ должна ронять money-path
        await log_price_staleness("sizing", "mkt3", "tok3", 0.42)
