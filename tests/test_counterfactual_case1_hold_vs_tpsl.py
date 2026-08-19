"""Тесты нового CLI-скрипта scripts/research/counterfactual_case1_hold_vs_tpsl.py —
первый вызывающий код app/analytics/counterfactual.py, заменяет audit_case3.

Все тесты на моках БД (без прод-подключения) — паттерн как остальные Phase 0
audit_case тесты (fetch/execute патчатся, не реальный asyncpg)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from scripts.research.counterfactual_case1_hold_vs_tpsl import (
    _build_actual_lookups,
    _config_hash,
    run_audit,
)
from scripts.research.exit_strategy_backtest import PositionTrajectory


def _traj(order_id: str, detector: str = "D38_resolution_lag") -> PositionTrajectory:
    return PositionTrajectory(
        order_id=order_id,
        detector=detector,
        side="YES",
        stake=100.0,
        fill_price=0.40,
        category="crypto",
        fee_schedule=None,
        filled_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        snapshots=[],
        expected_resolution_at=None,
    )


@pytest.mark.asyncio
async def test_build_actual_lookups_maps_order_id_to_pnl_and_outcome():
    """asyncpg возвращает execution_orders.id как UUID-объект (не str) —
    row["id"] здесь намеренно НЕ строка, чтобы поймать регрессию 2026-07-21:
    прод-прогон дал tpsl_pnl=0.0 для всех trajectories, потому что lookup был
    ключован UUID-объектами, а .get(traj.order_id, ...) искал по str(order_id) —
    юнит-тест со строковыми id в моке эту регрессию не ловил."""
    order_id_1 = uuid.uuid4()
    order_id_2 = uuid.uuid4()
    trajectories = [_traj(str(order_id_1)), _traj(str(order_id_2))]
    fake_rows = [
        {"id": order_id_1, "net_pnl": 25.0, "outcome": "WIN"},
        {"id": order_id_2, "net_pnl": -15.0, "outcome": "LOSS"},
    ]

    with patch(
        "scripts.research.counterfactual_case1_hold_vs_tpsl.fetch", new=AsyncMock(return_value=fake_rows)
    ):
        pnl_lookup, outcome_lookup = await _build_actual_lookups(trajectories)

    assert pnl_lookup == {str(order_id_1): 25.0, str(order_id_2): -15.0}
    assert outcome_lookup == {str(order_id_1): "WIN", str(order_id_2): "LOSS"}


@pytest.mark.asyncio
async def test_run_audit_returns_metrics_and_verdict_with_ci():
    order_ids = [uuid.uuid4() for _ in range(5)]
    trajectories = [_traj(str(oid)) for oid in order_ids]
    # id — UUID-объект (как реально возвращает asyncpg), не str — см. регрессионный
    # docstring test_build_actual_lookups_maps_order_id_to_pnl_and_outcome выше.
    fake_rows = [{"id": oid, "net_pnl": 10.0, "outcome": "WIN"} for oid in order_ids]

    with (
        patch(
            "scripts.research.counterfactual_case1_hold_vs_tpsl.fetch_resolved_with_snapshots",
            new=AsyncMock(return_value=trajectories),
        ),
        patch(
            "scripts.research.counterfactual_case1_hold_vs_tpsl.fetch", new=AsyncMock(return_value=fake_rows)
        ),
    ):
        result = await run_audit(datetime(2026, 5, 28, tzinfo=UTC))

    assert "metrics" in result
    assert "verdict" in result
    assert "ci_low" in result["metrics"]
    assert "ci_high" in result["metrics"]
    assert result["metrics"]["n_trades"] == 10  # 5 trajectories x 2 detectors (same fixture reused per detector)
    # tpsl_pnl (baseline, AsIsPolicy) должен реально подхватить net_pnl=10.0 x 10
    # rows, а не молча остаться 0.0 из-за несовпадения типа ключа lookup (UUID vs str).
    assert result["metrics"]["tpsl_pnl"] == pytest.approx(100.0)


def test_config_hash_deterministic_for_same_input():
    start_date = datetime(2026, 5, 28, tzinfo=UTC)

    first = _config_hash(start_date)
    second = _config_hash(start_date)

    assert first == second
    assert isinstance(first, str)
