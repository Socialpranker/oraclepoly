"""Tests для app/calibration/event_aware_split.py.

Stage 1 / Two-Stage ML Pipeline.

Critical property: markets одного event'а должны быть в одном bucket
(train или holdout, не оба) — иначе data leakage между train/holdout.
"""

from __future__ import annotations

import pytest


def test_no_event_appears_in_both_train_and_holdout():
    from app.calibration.event_aware_split import event_aware_holdout_split

    # 10 events, каждый по 3 markets → 30 examples
    event_ids = []
    for ev in range(10):
        for _ in range(3):
            event_ids.append(f"ev{ev}")

    train_idx, holdout_idx = event_aware_holdout_split(event_ids, holdout_fraction=0.3, seed=42)

    train_events = {event_ids[i] for i in train_idx}
    holdout_events = {event_ids[i] for i in holdout_idx}
    assert train_events.isdisjoint(holdout_events), (
        f"Leakage: events {train_events & holdout_events} appear in both train and holdout"
    )


def test_no_overlap_between_indices():
    from app.calibration.event_aware_split import event_aware_holdout_split

    event_ids = [f"ev{i // 3}" for i in range(30)]
    train_idx, holdout_idx = event_aware_holdout_split(event_ids, holdout_fraction=0.3, seed=42)
    assert set(train_idx).isdisjoint(set(holdout_idx))
    assert len(train_idx) + len(holdout_idx) == 30


def test_holdout_size_approx_correct():
    from app.calibration.event_aware_split import event_aware_holdout_split

    event_ids = [f"ev{i // 3}" for i in range(30)]
    train_idx, holdout_idx = event_aware_holdout_split(event_ids, holdout_fraction=0.3, seed=42)
    holdout_share = len(holdout_idx) / 30
    # Не строгое 30%, потому что split идёт по events (10 events × 3 markets)
    assert 0.20 <= holdout_share <= 0.40, f"holdout share {holdout_share:.2f} too far from 0.30"


def test_seed_determinism():
    from app.calibration.event_aware_split import event_aware_holdout_split

    event_ids = [f"ev{i // 2}" for i in range(20)]
    a1, h1 = event_aware_holdout_split(event_ids, holdout_fraction=0.2, seed=42)
    a2, h2 = event_aware_holdout_split(event_ids, holdout_fraction=0.2, seed=42)
    assert a1 == a2
    assert h1 == h2


def test_different_seeds_give_different_splits():
    from app.calibration.event_aware_split import event_aware_holdout_split

    event_ids = [f"ev{i // 2}" for i in range(40)]
    _, h_a = event_aware_holdout_split(event_ids, holdout_fraction=0.2, seed=42)
    _, h_b = event_aware_holdout_split(event_ids, holdout_fraction=0.2, seed=99)
    # Не обязательно полностью different, но distribution должен отличаться
    assert h_a != h_b


def test_empty_input():
    from app.calibration.event_aware_split import event_aware_holdout_split

    train, holdout = event_aware_holdout_split([], holdout_fraction=0.2)
    assert train == []
    assert holdout == []


def test_single_event_goes_entirely_to_one_bucket():
    """Edge: 1 event → не может быть в обоих частях."""
    from app.calibration.event_aware_split import event_aware_holdout_split

    event_ids = ["ev1"] * 10
    train, holdout = event_aware_holdout_split(event_ids, holdout_fraction=0.5, seed=42)
    # Все 10 markets либо в train, либо в holdout
    assert (len(train) == 10 and len(holdout) == 0) or (len(train) == 0 and len(holdout) == 10)


def test_time_based_event_split_simple_order():
    from app.calibration.event_aware_split import time_based_event_holdout_split

    event_ids = [f"ev{i}" for i in range(10)]
    timestamps = [float(i) for i in range(10)]
    train_idx, holdout_idx = time_based_event_holdout_split(event_ids, timestamps, holdout_fraction=0.3)

    train_events = {event_ids[i] for i in train_idx}
    holdout_events = {event_ids[i] for i in holdout_idx}
    # ev0..ev6 → train (7 events = 70%), ev7..ev9 → holdout
    assert train_events == {f"ev{i}" for i in range(7)}
    assert holdout_events == {f"ev{i}" for i in range(7, 10)}


def test_time_based_uses_min_timestamp_per_event():
    """Когда event имеет несколько rows, timestamp event'а = min(rows.ts)."""
    from app.calibration.event_aware_split import time_based_event_holdout_split

    # ev_late имеет один поздний timestamp + один ранний → min=ранний → train (старый)
    # ev_early — только ранние → train
    # ev_latest — только поздние → holdout
    event_ids = ["ev_late", "ev_late", "ev_early", "ev_latest", "ev_latest"]
    timestamps = [10.0, 1.0, 2.0, 100.0, 99.0]
    train_idx, holdout_idx = time_based_event_holdout_split(event_ids, timestamps, holdout_fraction=0.4)

    train_events = {event_ids[i] for i in train_idx}
    holdout_events = {event_ids[i] for i in holdout_idx}
    # 3 events, holdout=0.4 → 1 event in holdout (latest by min ts)
    # event timestamps: ev_late→1.0, ev_early→2.0, ev_latest→99.0
    # sorted: [ev_late, ev_early, ev_latest], cut at 60% = 1 → holdout = [ev_early, ev_latest]
    # Wait — cut = int(3 * 0.6) = 1, holdout starts at index 1, so 2 events in holdout.
    # Так что: train={ev_late}, holdout={ev_early, ev_latest}
    assert "ev_late" in train_events
    assert "ev_latest" in holdout_events


def test_time_based_len_mismatch_raises():
    from app.calibration.event_aware_split import time_based_event_holdout_split

    with pytest.raises(ValueError, match="len mismatch"):
        time_based_event_holdout_split(["ev1", "ev2"], [1.0, 2.0, 3.0])


def test_time_based_empty():
    from app.calibration.event_aware_split import time_based_event_holdout_split

    train, holdout = time_based_event_holdout_split([], [])
    assert train == []
    assert holdout == []
