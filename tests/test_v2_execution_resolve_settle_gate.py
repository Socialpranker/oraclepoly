"""Тесты settle-гейта: live-ордера settle только по резолву РЫНКА.

P0 из research/fable-gates-audit-2026-07-03 §3.1: EXPIRED сигнала (таймаут
horizon_sec×N) закрывал живые live-позиции в $0 при нерезолвнутом рынке.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.jobs import v2_execution_resolve as ver


def _row(**over):
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "execution_mode": "taker",
        "fill_price": 0.40,
        "stake": 5.0,
        "side": "NO",
        "market_id": "mkt-1",
        "outcome": "EXPIRED",  # ds.outcome — сигнал истёк по таймауту
        "winner": None,  # m.winner — рынок ещё не резолвнут
        "category": "crypto",
        "fee_schedule": None,
        "question": "Q?",
        "as_penalty_usd": None,
    }
    base.update(over)
    return base


def test_settleable_sql_requires_market_resolve_for_live():
    """SQL-гейт: live-ордера попадают в выборку только при резолве рынка."""
    sql = ver._FETCH_SETTLEABLE_SQL
    assert "m.resolved = true" in sql
    assert "m.winner IN ('yes', 'no')" in sql
    assert "eo.execution_mode = 'virtual'" in sql
    assert "m.winner" in sql.split("FROM")[0]  # winner есть в SELECT


def test_settleable_sql_ds_resolved_gates_only_virtual():
    """ds.resolved-гейт только для virtual-ветки: live settle сразу по markets.winner.

    Инцидент 724f3e71 (2026-07-07): live-ордер ждал часовой тик :40
    detector-signals-resolve (+56 мин лага), хотя live-outcome целиком
    выводится из m.winner и ds.resolved для него избыточен.
    """
    sql = ver._FETCH_SETTLEABLE_SQL
    assert "eo.execution_mode = 'virtual' AND ds.resolved = true" in sql
    assert "eo.execution_mode != 'virtual'" in sql
    # Нет top-level гейта ds.resolved, общего для обеих веток
    where = sql.split("WHERE", 1)[1]
    top_level_lines = [line.strip() for line in where.splitlines() if line.strip().startswith("AND ds.resolved")]
    assert top_level_lines == []


@pytest.mark.asyncio
async def test_live_outcome_derived_from_market_winner(monkeypatch):
    """Live-ордер NO при winner='no' → WIN (а не EXPIRED из ds.outcome)."""
    fetch_mock = AsyncMock(return_value=[_row(winner="no", outcome="EXPIRED")])
    execute_mock = AsyncMock()
    monkeypatch.setattr(ver, "fetch", fetch_mock)
    monkeypatch.setattr(ver, "execute", execute_mock)

    n = await ver._settle_filled()

    assert n == 1
    args = execute_mock.call_args.args
    # _SETTLE_UPDATE_SQL: $2 = statuses[], $3 = outcomes[]
    assert args[2] == ["settled"]
    assert args[3] == ["WIN"]


@pytest.mark.asyncio
async def test_live_no_winner_is_skipped(monkeypatch):
    """Live-ордер без валидного winner (страховка от дырки в SQL) — пропуск, не $0."""
    fetch_mock = AsyncMock(return_value=[_row(winner=None)])
    execute_mock = AsyncMock()
    monkeypatch.setattr(ver, "fetch", fetch_mock)
    monkeypatch.setattr(ver, "execute", execute_mock)

    n = await ver._settle_filled()

    assert n == 0
    execute_mock.assert_not_called()


@pytest.mark.asyncio
async def test_virtual_expired_settles_as_before(monkeypatch):
    """Virtual-ордер: ds.outcome='EXPIRED' по-прежнему закрывается в 0 (paper-учёт)."""
    fetch_mock = AsyncMock(
        return_value=[_row(execution_mode="virtual", outcome="EXPIRED", winner=None)]
    )
    execute_mock = AsyncMock()
    monkeypatch.setattr(ver, "fetch", fetch_mock)
    monkeypatch.setattr(ver, "execute", execute_mock)

    n = await ver._settle_filled()

    assert n == 1
    args = execute_mock.call_args.args
    assert args[2] == ["virtual_settled"]
    assert args[3] == ["EXPIRED"]
    assert args[4] == [0.0]  # pnl нулевой


@pytest.mark.asyncio
async def test_live_loss_when_winner_opposite(monkeypatch):
    """Live YES при winner='no' → LOSS с настоящим минусом, не фантомный 0."""
    fetch_mock = AsyncMock(return_value=[_row(side="YES", winner="no", outcome="WIN")])
    execute_mock = AsyncMock()
    monkeypatch.setattr(ver, "fetch", fetch_mock)
    monkeypatch.setattr(ver, "execute", execute_mock)

    n = await ver._settle_filled()

    assert n == 1
    args = execute_mock.call_args.args
    assert args[3] == ["LOSS"]
    assert args[4] == [pytest.approx(-5.0, abs=0.01)]  # gross = -stake
