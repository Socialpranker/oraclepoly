"""Tests для app.analytics.adverse_selection."""

from __future__ import annotations

from app.analytics.adverse_selection import (
    AdverseSelectionConfig,
    classify_cell,
    is_adverse,
    is_temperature_market,
)

_CFG = AdverseSelectionConfig(min_events=20, wilson_threshold=0.5)


def test_is_temperature_market_matches_markers():
    assert is_temperature_market("Will the temperature exceed 90°F?") is True
    assert is_temperature_market("Highest temperature in NYC") is True
    assert is_temperature_market("Will BTC hit 100k?") is False
    assert is_temperature_market(None) is False
    assert is_temperature_market("") is False


def test_is_adverse_true_when_all_conditions_met():
    # проигрываем направление (wilson_lower < 0.5), теряем деньги, N достаточно
    assert is_adverse(n_events=30, wilson_lower=0.37, net_pnl=-120.0, config=_CFG) is True


def test_is_adverse_false_when_below_min_events():
    # плохие метрики, но данных мало — не adverse (шум)
    assert is_adverse(n_events=10, wilson_lower=0.20, net_pnl=-50.0, config=_CFG) is False


def test_is_adverse_false_when_net_pnl_nonnegative():
    assert is_adverse(n_events=30, wilson_lower=0.37, net_pnl=0.0, config=_CFG) is False
    assert is_adverse(n_events=30, wilson_lower=0.37, net_pnl=5.0, config=_CFG) is False


def test_is_adverse_false_when_wilson_at_or_above_threshold():
    # winrate статистически не ниже 50% — просто убыток, не проигрыш направления
    assert is_adverse(n_events=30, wilson_lower=0.5, net_pnl=-120.0, config=_CFG) is False
    assert is_adverse(n_events=30, wilson_lower=0.62, net_pnl=-120.0, config=_CFG) is False


def test_classify_cell_reopens_temperature_anchor():
    # синтетический temperature/W&E: winrate ~37%, net_pnl<0, N>=20 → adverse + temperature
    outcomes = ["WIN"] * 11 + ["LOSS"] * 19  # 30 events, 37% wins
    pnls = [1.0] * 11 + [-1.0] * 19
    cell = classify_cell(
        detector="D38", category="World & Economy", outcomes=outcomes, pnls=pnls,
        net_pnl=-88.0, is_temperature=True, config=_CFG,
    )
    assert cell.is_adverse is True
    assert cell.is_temperature is True
    assert cell.n_events == 30
    assert cell.wins == 11


def test_classify_cell_healthy_not_adverse():
    outcomes = ["WIN"] * 22 + ["LOSS"] * 8  # 30 events, 73% wins
    pnls = [1.0] * 22 + [-1.0] * 8
    cell = classify_cell(
        detector="D3", category="Sports", outcomes=outcomes, pnls=pnls,
        net_pnl=140.0, is_temperature=False, config=_CFG,
    )
    assert cell.is_adverse is False
