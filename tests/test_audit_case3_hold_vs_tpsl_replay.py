"""Тесты новых функций Phase 0 audit-скрипта replay-кейса 3
(scripts/research/audit_case3_hold_vs_tpsl_replay.py).

Покрывает `simulate_hold_to_resolution` (новая симуляция, отсутствовавшая в
эталонном `exit_strategy_backtest.py`), `_delta_pct` (формула сравнения
hold-to-resolution vs as-is TP/SL) и `_config_hash` (детерминированность
конфига для agent_runs.config_hash).

Все тесты на синтетических данных — без прод-БД (та доступна только на VPS,
см. CLAUDE.md: scripts/research/*.py запускаются только на проде). В отличие
от `tests/test_exit_strategy_backtest.py`, hold-to-resolution не использует
ценовую траекторию (снапшоты) вообще — `simulate_hold_to_resolution` идёт
прямиком в `_compute_settlement_pnl` по stake/fill_price/outcome, поэтому
фикстура ниже проще, чем `_traj()` из эталонного теста (snapshots не нужны).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from scripts.research.audit_case3_hold_vs_tpsl_replay import (
    _config_hash,
    _delta_pct,
    simulate_hold_to_resolution,
)
from scripts.research.exit_strategy_backtest import PositionTrajectory


def _hold_traj(
    *,
    stake: float = 100.0,
    fill_price: float = 0.40,
    category: str = "crypto",
) -> PositionTrajectory:
    """Синтетическая траектория для hold-to-resolution: снапшоты не нужны —
    `simulate_hold_to_resolution` использует только stake/fill_price/category/
    fee_schedule, минуя ценовую траекторию целиком (нет exit-ноги)."""
    filled_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    return PositionTrajectory(
        order_id="test-order-hold",
        detector="D_TEST",
        side="YES",
        stake=stake,
        fill_price=fill_price,
        category=category,
        fee_schedule=None,
        filled_at=filled_at,
        snapshots=[],
        expected_resolution_at=None,
    )


# --- simulate_hold_to_resolution -------------------------------------------


def test_simulate_hold_to_resolution_win_is_positive():
    """WIN: gross = stake/fill - stake, net = gross - fee(entry only, resolution
    fee = 0 на exit-ноге). Для stake=100, fill=0.40, category=crypto (rate=0.072):
    gross = 100/0.40 - 100 = 150; fee = 100*0.072*(0.4*0.6) = 1.728;
    net = 150 - 1.728 = 148.272."""
    traj = _hold_traj(stake=100.0, fill_price=0.40, category="crypto")

    result = simulate_hold_to_resolution(traj, "WIN")

    assert result.net_pnl == pytest.approx(148.272)
    assert result.net_pnl > 0


def test_simulate_hold_to_resolution_loss_is_negative_with_fee():
    """LOSS: gross = -stake, fee всё равно применяется на entry-ногу (не 0).
    Для stake=100, fill=0.40, category=crypto: fee = 100*0.072*(0.4*0.6) = 1.728;
    net = -100 - 1.728 = -101.728 (не голые -100 — независимая проверка формулы
    _compute_settlement_pnl в app/jobs/v2_execution_resolve.py, не копия
    заявленного числа из отчёта)."""
    traj = _hold_traj(stake=100.0, fill_price=0.40, category="crypto")

    result = simulate_hold_to_resolution(traj, "LOSS")

    assert result.net_pnl == pytest.approx(-101.728)
    assert result.net_pnl < -100.0  # строго хуже голых -100 — fee применена


def test_simulate_hold_to_resolution_exit_reason_and_no_single_exit_price():
    """exit_reason фиксирован как 'hold_to_resolution'; exit_price = NaN —
    задокументированное намеренное решение (резолюция — не торговля, нет
    единой exit-цены, в отличие от trailing/decay/partial)."""
    traj = _hold_traj()

    result = simulate_hold_to_resolution(traj, "WIN")

    assert result.exit_reason == "hold_to_resolution"
    assert math.isnan(result.exit_price)
    assert result.order_id == "test-order-hold"


# --- _delta_pct --------------------------------------------------------------


def test_delta_pct_normal_case_hold_better():
    """hold=150, tpsl=100 -> hold лучше на 50% относительно |tpsl|."""
    assert _delta_pct(150.0, 100.0) == pytest.approx(50.0)


def test_delta_pct_sign_flip_hold_worse():
    """hold=50, tpsl=100 -> hold хуже, delta_pct отрицателен."""
    assert _delta_pct(50.0, 100.0) == pytest.approx(-50.0)


def test_delta_pct_division_by_zero_guard():
    """tpsl_pnl == 0 -> None (нет базы для относительного сравнения),
    не деление на ноль/inf."""
    assert _delta_pct(100.0, 0.0) is None
    assert _delta_pct(-100.0, 0.0) is None
    assert _delta_pct(0.0, 0.0) is None


# --- _config_hash --------------------------------------------------------------


def test_config_hash_deterministic_for_same_input():
    """Один и тот же start_date -> один и тот же хеш при повторном вызове —
    нужно для agent_runs.config_hash (идемпотентность/сравнимость прогонов)."""
    start_date = datetime(2026, 5, 28, tzinfo=UTC)

    first = _config_hash(start_date)
    second = _config_hash(start_date)

    assert first == second
    assert isinstance(first, str)
    assert len(first) == 16  # sha256 hexdigest усечён до 16 символов


def test_config_hash_differs_for_different_start_date():
    """Разный start_date должен менять хеш (конфиг реально зависит от даты,
    не заглушка с фиксированным выводом)."""
    hash_a = _config_hash(datetime(2026, 5, 28, tzinfo=UTC))
    hash_b = _config_hash(datetime(2026, 6, 1, tzinfo=UTC))

    assert hash_a != hash_b
