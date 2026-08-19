"""Shadow-прогон live-гейтов на virtual-потоке (spec 2026-07-22 §5).

shadow_evaluate_virtual — best-effort, никогда не бросает, ничего не блокирует.
Только пишет would-reject в gate_shadow_log с префиксом shadow_v2: для
последующей верификации anti-selection фикса (would-rejected больше не
концентрирует победителей).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services.execution_safety import SafetyContext, shadow_evaluate_virtual


def _ctx(**overrides):
    base = dict(
        detector="D7b_no_favorite_fade",
        detector_state=None,
        market_id="0xmarket1",
        token_id="tok1",
        side="NO",
        signal_price=0.10,
        stake_usd=10.0,
        bankroll_usd=1000.0,
        generated_at=datetime.now(UTC) - timedelta(seconds=10),  # свежий
        market_end_date=datetime.now(UTC) + timedelta(days=5),
        detector_signal_id=uuid4(),
    )
    base.update(overrides)
    return SafetyContext(**base)


@pytest.mark.asyncio
async def test_shadow_logs_would_reject_stale():
    """ctx.generated_at старше 1800s → log_gate_shadow вызван с
    gate='shadow_v2:stale_signal', token_id=None."""
    ctx = _ctx(generated_at=datetime.now(UTC) - timedelta(seconds=1801))
    with (
        patch(
            "app.services.execution_safety.check_max_open_positions",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_max_inflight_orders",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_daily_pnl_limit_mtm",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch("app.services.gate_shadow_telemetry.log_gate_shadow", new_callable=AsyncMock) as mock_log,
    ):
        await shadow_evaluate_virtual(ctx)

    mock_log.assert_awaited_once_with(
        detector=ctx.detector,
        gate="shadow_v2:stale_signal",
        detector_signal_id=ctx.detector_signal_id,
        stake_usd=ctx.stake_usd,
        token_id=None,
    )


@pytest.mark.asyncio
async def test_shadow_logs_nothing_when_all_pass():
    """Все чеки pass → log_gate_shadow не вызывался."""
    ctx = _ctx()
    with (
        patch(
            "app.services.execution_safety.check_max_open_positions",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_max_inflight_orders",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_daily_pnl_limit_mtm",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch("app.services.gate_shadow_telemetry.log_gate_shadow", new_callable=AsyncMock) as mock_log,
    ):
        await shadow_evaluate_virtual(ctx)

    mock_log.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_never_raises():
    """Чек бросил исключение → shadow_evaluate_virtual молча логирует warning,
    не пробрасывает (money-path не роняется)."""
    ctx = _ctx()
    with (
        patch(
            "app.services.execution_safety.check_max_open_positions",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ),
        patch(
            "app.services.execution_safety.check_max_inflight_orders",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_daily_pnl_limit_mtm",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch("app.services.gate_shadow_telemetry.log_gate_shadow", new_callable=AsyncMock),
    ):
        # Не должно бросить.
        await shadow_evaluate_virtual(ctx)


@pytest.mark.asyncio
async def test_virtual_verdict_not_affected(monkeypatch):
    """В _process_signal virtual-путь: verdict.allowed остаётся True
    независимо от would-reject'ов shadow."""
    import app.services.execution_safety as es

    ctx = _ctx(generated_at=datetime.now(UTC) - timedelta(seconds=10000))  # stale
    verdict = await es.evaluate(ctx, mode="virtual")
    assert verdict.allowed is True
    assert verdict.reason == ""

    # shadow-прогон на том же ctx — would-reject пишется в телеметрию,
    # но verdict от evaluate() (реальное решение virtual-пути) не меняется.
    with (
        patch(
            "app.services.execution_safety.check_max_open_positions",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_max_inflight_orders",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_daily_pnl_limit_mtm",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch("app.services.gate_shadow_telemetry.log_gate_shadow", new_callable=AsyncMock) as mock_log,
    ):
        await shadow_evaluate_virtual(ctx)

    mock_log.assert_awaited_once()
    assert verdict.allowed is True


@pytest.mark.asyncio
async def test_shadow_uses_same_daily_loss_gate_as_live():
    """Shadow судит по той же MtM-функции, что и live-ветка evaluate().

    Регрессия 2026-08-05: shadow звал DEPRECATED `check_daily_loss`, а live —
    `check_daily_pnl_limit_mtm` (unrealized-aware). Shadow измерял НЕ ТОТ гейт,
    который реально отклоняет сигналы, и вопрос «режут ли лимиты хорошее» по
    этим данным был неотвечаем.
    """
    ctx = _ctx()
    with (
        patch(
            "app.services.execution_safety.check_max_open_positions",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_max_inflight_orders",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_daily_pnl_limit_mtm",
            new_callable=AsyncMock,
            return_value=(False, "daily_loss_limit_mtm:total=$-99"),
        ) as mock_mtm,
        patch(
            "app.services.execution_safety.check_daily_loss",
            new_callable=AsyncMock,
            return_value=(False, "daily_loss:legacy"),
        ) as mock_legacy,
        patch("app.services.gate_shadow_telemetry.log_gate_shadow", new_callable=AsyncMock) as mock_log,
    ):
        await shadow_evaluate_virtual(ctx)

    mock_mtm.assert_awaited_once()
    mock_legacy.assert_not_awaited(), "deprecated check_daily_loss не должен зваться в shadow"
    gates = [c.kwargs["gate"] for c in mock_log.await_args_list]
    assert "shadow_v2:daily_loss_limit_mtm" in gates
    assert "shadow_v2:daily_loss" not in gates


@pytest.mark.asyncio
async def test_shadow_covers_horizon_and_inflight_gates():
    """horizon_cap и max_inflight_orders тоже участвуют в live-решении и не
    требуют book-I/O — значит должны попадать в shadow-замер."""
    ctx = _ctx(market_end_date=datetime.now(UTC) + timedelta(days=3650))  # за horizon-cap
    with (
        patch(
            "app.services.execution_safety.check_max_open_positions",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch(
            "app.services.execution_safety.check_max_inflight_orders",
            new_callable=AsyncMock,
            return_value=(False, "max_inflight_orders:5>=5"),
        ),
        patch(
            "app.services.execution_safety.check_daily_pnl_limit_mtm",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ),
        patch("app.services.gate_shadow_telemetry.log_gate_shadow", new_callable=AsyncMock) as mock_log,
    ):
        await shadow_evaluate_virtual(ctx)

    gates = [c.kwargs["gate"] for c in mock_log.await_args_list]
    assert "shadow_v2:horizon_cap" in gates
    assert "shadow_v2:max_inflight_orders" in gates
