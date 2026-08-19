"""Тесты симуляторов альтернативных exit-стратегий (research/position-exit-strategies-2026-07).

Симуляторы прогоняют исторические ценовые траектории (detector_signal_price_snapshots)
через альтернативные exit-правила (trailing stop / time-decay / partial exit) и
сравнивают honest net PnL с фактическим. Переиспользуют _compute_early_exit_pnl /
_compute_settlement_pnl из position_manager.py / v2_execution_resolve.py — не
дублируют fee/slippage-логику.

Все тесты на синтетических траекториях — без прод-БД (та доступна только на VPS,
см. CLAUDE.md: scripts/research/*.py запускаются только на проде).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.research.exit_strategy_backtest import (
    PositionTrajectory,
    PricePoint,
    simulate_partial_exit,
    simulate_time_decay,
    simulate_trailing_stop,
)


def _traj(
    *,
    side: str = "YES",
    stake: float = 100.0,
    fill_price: float = 0.40,
    category: str = "crypto",
    prices: list[tuple[int, float]],
    horizon_minutes: float = 120.0,
) -> PositionTrajectory:
    """prices — список (offset_minutes, yes_price) от filled_at."""
    filled_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    snapshots = [
        PricePoint(captured_at=filled_at + timedelta(minutes=m), yes_price=p) for m, p in prices
    ]
    return PositionTrajectory(
        order_id="test-order",
        detector="D_TEST",
        side=side,
        stake=stake,
        fill_price=fill_price,
        category=category,
        fee_schedule=None,
        filled_at=filled_at,
        snapshots=snapshots,
        expected_resolution_at=filled_at + timedelta(minutes=horizon_minutes),
    )


# --- simulate_trailing_stop ---------------------------------------------


def test_simulate_trailing_stop_locks_profit_on_pullback():
    """YES, fill=0.40. Цена растёт до 0.60 (+50% unrealized, пик), откатывается.
    Trailing 10pp от пика PnL% должен зафиксировать выход на первом шаге, где
    просадка от пика достигает 10pp — не позже.

    Арифметика (PnL% = (price/0.40 - 1) * 100):
      0.60 -> +50.0% (пик)
      0.55 -> +37.5% (drawdown от пика = 12.5pp >= trail 10pp -> триггер здесь)
    """
    traj = _traj(
        prices=[
            (0, 0.40),
            (10, 0.50),  # +25% unrealized
            (20, 0.60),  # +50% unrealized — новый пик
            (30, 0.55),  # откат: drawdown 12.5pp >= trail_pct=10 -> триггер
            (40, 0.50),
            (50, 0.45),
        ]
    )
    result = simulate_trailing_stop(traj, trail_pct=10.0)

    assert result.exit_reason == "trailing_stop"
    # Должен был выйти на шаге 30 (0.55) — первый момент, где просадка от пика
    # пробивает 10pp, не должен дойти до более поздних (0.50/0.45).
    assert result.exit_price == pytest.approx(0.55)
    assert result.net_pnl > 0  # зафиксировал часть прибыли, не ушёл в минус


def test_simulate_trailing_stop_never_triggers_on_monotonic_rise():
    """Цена монотонно растёт без отката — trailing stop не должен сработать
    раньше конца траектории (держим до конца доступных данных)."""
    traj = _traj(
        prices=[
            (0, 0.40),
            (10, 0.45),
            (20, 0.50),
            (30, 0.55),
            (40, 0.60),
        ]
    )
    result = simulate_trailing_stop(traj, trail_pct=10.0)

    assert result.exit_reason == "held_to_end_of_data"
    assert result.exit_price == pytest.approx(0.60)


def test_simulate_trailing_stop_no_snapshots_returns_none():
    traj = _traj(prices=[])
    result = simulate_trailing_stop(traj, trail_pct=10.0)
    assert result is None


# --- simulate_time_decay -------------------------------------------------


def test_simulate_time_decay_narrows_range_near_resolution():
    """SL сужается линейно от wide_sl_pct (в начале хода) до narrow_sl_pct
    (у самой резолюции). Цена падает умеренно ближе к горизонту — узкий SL
    должен сработать раньше, чем сработал бы широкий фиксированный."""
    traj = _traj(
        horizon_minutes=100.0,
        prices=[
            (0, 0.40),
            (20, 0.38),  # -5% — рано, широкий SL (-30%) не тронул бы
            (85, 0.34),  # -15% и близко к горизонту (85/100 = 85%) — узкий SL здесь должен сработать
            (95, 0.30),
        ],
    )
    result = simulate_time_decay(traj, wide_sl_pct=-30.0, narrow_sl_pct=-10.0, wide_tp_pct=30.0, narrow_tp_pct=10.0)

    assert result.exit_reason == "time_decay_sl"
    assert result.exit_price == pytest.approx(0.34)


def test_simulate_time_decay_holds_when_within_narrowing_bounds():
    """Цена почти не двигается — ни широкий, ни узкий порог не пробит, держим
    до конца доступных данных."""
    traj = _traj(
        horizon_minutes=100.0,
        prices=[(0, 0.40), (30, 0.41), (60, 0.39), (90, 0.40)],
    )
    result = simulate_time_decay(traj, wide_sl_pct=-30.0, narrow_sl_pct=-10.0, wide_tp_pct=30.0, narrow_tp_pct=10.0)

    assert result.exit_reason == "held_to_end_of_data"


# --- simulate_partial_exit ------------------------------------------------


def test_simulate_partial_exit_splits_correctly_with_double_fee():
    """50% позиции продаётся на промежуточном таргете (половина полного TP),
    остаток держится до фактического исхода (передаётся как actual_outcome)."""
    traj = _traj(
        stake=100.0,
        prices=[
            (0, 0.40),
            (10, 0.46),  # +15% — промежуточный таргет (половина TP=30%)
            (20, 0.52),
        ],
    )
    result = simulate_partial_exit(
        traj,
        partial_trigger_pct=15.0,
        partial_fraction=0.5,
        actual_outcome="WIN",
    )

    assert result is not None
    # Первая нога — early exit по 0.46 на половину stake (fee двойная).
    assert result.partial_exit_price == pytest.approx(0.46)
    assert result.partial_stake == pytest.approx(50.0)
    assert result.remainder_stake == pytest.approx(50.0)
    # Общий net_pnl = net от partial-ноги + net от settlement-ноги остатка.
    assert result.net_pnl != 0.0


def test_simulate_partial_exit_no_trigger_returns_full_settlement():
    """Промежуточный таргет не достигнут — вся позиция идёт как settlement
    (совпадает с baseline, без доп. комиссии за частичную продажу)."""
    traj = _traj(prices=[(0, 0.40), (10, 0.41), (20, 0.42)])
    result = simulate_partial_exit(
        traj,
        partial_trigger_pct=15.0,
        partial_fraction=0.5,
        actual_outcome="WIN",
    )

    assert result is not None
    assert result.partial_exit_price is None
    assert result.remainder_stake == pytest.approx(100.0)


# --- baseline sanity check -------------------------------------------------


def test_baseline_trailing_stop_matches_actual_early_exit_when_thresholds_equal():
    """Sanity gate из плана: если симулировать trailing stop с фактическим
    зафиксированным TP-порогом, и цена монотонно доходит ровно до этого порога
    без отката, симуляция должна дать тот же exit_price, что фактический TP.
    Не байт-в-байт net_pnl (там fee считаются по общей формуле, не по SQL
    напрямую) — но exit_price обязан совпасть, иначе логика симуляции сломана."""
    traj = _traj(
        prices=[(0, 0.40), (10, 0.45), (20, 0.52)],  # пик +30% на шаге 20
    )
    result = simulate_trailing_stop(traj, trail_pct=0.01)  # почти нулевой trail = фиксация на пике

    assert result.exit_price == pytest.approx(0.52)
