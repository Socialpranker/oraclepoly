"""Тесты адаптера canonical-replay -> exit-симуляторы (research/position-exit-strategies-2026-07).

Разведка (2026-07-03) показала: реальные прод-сигналы (execution_orders) не
пересекаются во времени с canonical (заморожен на 2026-05-04) — единственный
путь прогнать exit-симуляторы на широком историческом окне — пересчитать
детектор ЗАНОВО через app/backtest поверх price_snapshots.parquet.

Не патчит app/backtest/replay_engine.py напрямую (тот уже используется для
promote-гейтов, менять рискованно) — отдельная копия минимального цикла,
которая дополнительно прокидывает snapshot-траекторию ПОСЛЕ тика эмита сигнала
и market_row (для end_date/condition_id), которые run_backtest() не отдаёт наружу.

Тесты на синтетических DataFrame/dict — без реального canonical parquet (тот
доступен только на проде, см. CLAUDE.md).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from scripts.research.canonical_exit_backtest import (
    CanonicalSignalTrajectory,
    build_trajectories_for_detector,
)


class _FakeSignal:
    def __init__(self, side="YES", fair_price=0.6, confidence=0.8, expected_edge_pct=20.0):
        self.side = side
        self.fair_price = fair_price
        self.confidence = confidence
        self.expected_edge_pct = expected_edge_pct


def _fake_detect_always_signal(market_event, now):
    """Детектор-заглушка: эмитит сигнал на первом тике, где current_price < 0.5."""
    if market_event["current_price"] < 0.5:
        return _FakeSignal(side="YES")
    return None


def _fake_detect_never():
    def _inner(market_event, now):
        return None

    return _inner


MARKETS_DF = pd.DataFrame(
    [
        {
            "condition_id": "0xmarket1",
            "category": "Crypto",
            "end_date": pd.Timestamp("2024-06-10 00:00:00"),
            "question": "Will BTC hit 100k?",
            "yes_won": 1,
        }
    ]
)

# 4 снапшота, far -> near (hours_to_resolve DESC), как отдаёт iter_market_snapshots.
SNAPSHOTS_MARKET1 = [
    {"condition_id": "0xmarket1", "hours_to_resolve": 168, "yes_price": 0.30, "trade_ts": 1717200000},
    {"condition_id": "0xmarket1", "hours_to_resolve": 72, "yes_price": 0.45, "trade_ts": 1717545600},
    {"condition_id": "0xmarket1", "hours_to_resolve": 24, "yes_price": 0.65, "trade_ts": 1717804800},
    {"condition_id": "0xmarket1", "hours_to_resolve": 4, "yes_price": 0.80, "trade_ts": 1717977600},
]


class _FakeLoader:
    """Мок CanonicalDataLoader — отдаёт заранее заданные markets/snapshots без parquet."""

    def __init__(self, markets_df, snapshots_by_market):
        self._markets = markets_df
        self._snapshots_by_market = snapshots_by_market

    def filtered_markets(self, config):
        return self._markets

    def iter_market_snapshots(self, markets, tick_stride=1):
        for mr in markets.to_dict(orient="records"):
            cid = mr["condition_id"]
            if cid in self._snapshots_by_market:
                yield mr, self._snapshots_by_market[cid]


def test_build_trajectories_captures_snapshots_after_signal_tick():
    """Сигнал эмитится на тике с hours_to_resolve=72 (yes_price 0.30->0.45, первый
    момент current_price >= 0.5 не сработавший, но у нас сигнал эмитится когда
    < 0.5 -- то есть на первом тике 0.30). Траектория ПОСЛЕ этого тика должна
    включать оставшиеся 3 снапшота (72, 24, 4 hours_to_resolve)."""
    loader = _FakeLoader(MARKETS_DF, {"0xmarket1": SNAPSHOTS_MARKET1})

    trajectories = build_trajectories_for_detector(
        detect_func=_fake_detect_always_signal,
        loader=loader,
        config_start=datetime(2024, 1, 1, tzinfo=UTC),
        config_end=datetime(2024, 12, 31, tzinfo=UTC),
        category_filter=None,
    )

    assert len(trajectories) == 1
    traj = trajectories[0]
    assert traj.market_id == "0xmarket1"
    assert traj.side == "YES"
    assert traj.entry_price == pytest.approx(0.30)
    assert traj.yes_won == 1
    # Первый снапшот (0.30, hours=168) — это тик эмита, не входит в "будущую"
    # траекторию для симуляторов (симуляторы сами начинают с точки входа =
    # entry, а snapshots — это то, что происходит ПОСЛЕ входа).
    assert len(traj.snapshots_after_entry) == 3
    assert traj.snapshots_after_entry[0].yes_price == pytest.approx(0.45)
    assert traj.snapshots_after_entry[-1].yes_price == pytest.approx(0.80)


def test_build_trajectories_no_signal_skips_market():
    loader = _FakeLoader(MARKETS_DF, {"0xmarket1": SNAPSHOTS_MARKET1})

    trajectories = build_trajectories_for_detector(
        detect_func=lambda market_event, now: None,
        loader=loader,
        config_start=datetime(2024, 1, 1, tzinfo=UTC),
        config_end=datetime(2024, 12, 31, tzinfo=UTC),
        category_filter=None,
    )

    assert trajectories == []


def test_build_trajectories_reconstructs_filled_at_from_end_date_minus_hours():
    """filled_at не хранится напрямую в canonical — реконструируется как
    end_date - hours_to_resolve часов на тике эмита."""
    loader = _FakeLoader(MARKETS_DF, {"0xmarket1": SNAPSHOTS_MARKET1})

    trajectories = build_trajectories_for_detector(
        detect_func=_fake_detect_always_signal,
        loader=loader,
        config_start=datetime(2024, 1, 1, tzinfo=UTC),
        config_end=datetime(2024, 12, 31, tzinfo=UTC),
        category_filter=None,
    )

    traj = trajectories[0]
    expected_filled_at = pd.Timestamp("2024-06-10 00:00:00") - pd.Timedelta(hours=168)
    assert traj.filled_at == expected_filled_at
    assert traj.expected_resolution_at == pd.Timestamp("2024-06-10 00:00:00")


def test_canonical_trajectory_converts_to_position_trajectory_for_simulators():
    """CanonicalSignalTrajectory должна конвертироваться в PositionTrajectory
    (существующий формат exit_strategy_backtest.py) без потери данных."""
    from scripts.research.canonical_exit_backtest import to_position_trajectory

    ct = CanonicalSignalTrajectory(
        market_id="0xmarket1",
        detector="d44",
        side="YES",
        category="Crypto",
        entry_price=0.30,
        filled_at=pd.Timestamp("2024-06-03 00:00:00"),
        expected_resolution_at=pd.Timestamp("2024-06-10 00:00:00"),
        yes_won=1,
        snapshots_after_entry=[],
    )
    pt = to_position_trajectory(ct, stake=100.0)

    assert pt.side == "YES"
    assert pt.fill_price == pytest.approx(0.30)
    assert pt.stake == pytest.approx(100.0)
    assert pt.category == "crypto"  # lowercased для fee_calculator._resolve_category
    assert pt.fee_schedule is None  # canonical не содержит per-market fee_schedule
