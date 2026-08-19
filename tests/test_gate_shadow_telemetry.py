"""Тесты shadow-телеметрии гейтов на пересмотре."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.gate_shadow_telemetry import log_gate_shadow


@pytest.mark.asyncio
async def test_logs_book_when_available():
    with (
        patch(
            "app.services.gate_shadow_telemetry.fetch_live_book",
            new_callable=AsyncMock,
            return_value={"bid": 0.40, "ask": 0.42, "bid_size": 100.0, "ask_size": 50.0},
        ),
        patch("app.services.gate_shadow_telemetry.execute", new_callable=AsyncMock) as mock_execute,
    ):
        await log_gate_shadow(
            detector="D3_cross_mkt_arb",
            gate="category_blacklist:Sports",
            detector_signal_id="sig-1",
            stake_usd=24.96,
            token_id="tok1",
        )

    args = mock_execute.call_args.args
    # $1..$7: detector, gate, detector_signal_id, stake_usd, best_bid, best_ask, depth_usd
    assert args[1] == "D3_cross_mkt_arb"
    assert args[2] == "category_blacklist:Sports"
    assert args[3] == "sig-1"
    assert args[4] == 24.96
    assert args[5] == 0.40
    assert args[6] == 0.42
    assert args[7] == pytest.approx(100.0 * 0.40 + 50.0 * 0.42)


@pytest.mark.asyncio
async def test_logs_null_book_when_no_token_id():
    with patch("app.services.gate_shadow_telemetry.execute", new_callable=AsyncMock) as mock_execute:
        await log_gate_shadow(
            detector="D3_cross_mkt_arb",
            gate="category_blacklist:Sports",
            detector_signal_id="sig-2",
            stake_usd=None,
            token_id=None,
        )

    args = mock_execute.call_args.args
    assert args[4] is None  # stake_usd
    assert args[5] is None  # best_bid
    assert args[6] is None  # best_ask
    assert args[7] is None  # depth_usd


@pytest.mark.asyncio
async def test_logs_null_book_when_fetch_fails():
    with (
        patch(
            "app.services.gate_shadow_telemetry.fetch_live_book",
            new_callable=AsyncMock,
            side_effect=RuntimeError("book fetch error"),
        ),
        patch("app.services.gate_shadow_telemetry.execute", new_callable=AsyncMock) as mock_execute,
    ):
        await log_gate_shadow(
            detector="D3_cross_mkt_arb",
            gate="category_blacklist:Sports",
            detector_signal_id="sig-3",
            token_id="tok3",
        )

    args = mock_execute.call_args.args
    assert args[5] is None
    assert args[6] is None
    assert args[7] is None


@pytest.mark.asyncio
async def test_insert_sql_has_dedup_on_conflict():
    """Регрессия: v2-execution гоняется раз в минуту и переотдаёт тот же
    rejected-сигнал (resolved=false), пока рынок не резолвится — без ON
    CONFLICT один сигнал пишет ×N дублей в gate_shadow_log (прод: ~59/сигнал).
    """
    with patch("app.services.gate_shadow_telemetry.execute", new_callable=AsyncMock) as mock_execute:
        await log_gate_shadow(
            detector="D42",
            gate="category_health:Sports",
            detector_signal_id="sig-dup",
        )

    sql = mock_execute.call_args.args[0]
    assert "ON CONFLICT (detector, gate, detector_signal_id) DO NOTHING" in sql


@pytest.mark.asyncio
async def test_never_raises_on_db_failure():
    with (
        patch(
            "app.services.gate_shadow_telemetry.fetch_live_book",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.services.gate_shadow_telemetry.execute",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ),
    ):
        # Телеметрия НЕ должна ронять money-path
        await log_gate_shadow(
            detector="D3_cross_mkt_arb",
            gate="category_blacklist:Sports",
            detector_signal_id="sig-4",
        )
