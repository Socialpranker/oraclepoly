"""Unit tests для live_price_snapshot_writer.

Проверяет логику anchor windowing — какие anchors "проходят сейчас" для market.
DB layer мокается; тест focuses на pure-функциях.
"""

from __future__ import annotations

import pytest

from app.jobs.live_price_snapshot_writer import (
    ANCHOR_WINDOW_SEC,
    _anchor_passing_now,
)


def test_anchor_passing_at_exact_moment() -> None:
    """end_date - h = now → anchor проходит."""
    end = 1_000_000.0
    now = end - 24 * 3600  # 24h до end_date
    assert _anchor_passing_now(end, anchor_h=24, now_unix=now) is True


def test_anchor_passing_within_window() -> None:
    """anchor_ts в пределах ±ANCHOR_WINDOW_SEC от now → True."""
    end = 1_000_000.0
    anchor_ts = end - 24 * 3600  # canonical anchor moment
    # now чуть позже — anchor только что прошёл
    now = anchor_ts + ANCHOR_WINDOW_SEC - 1
    assert _anchor_passing_now(end, anchor_h=24, now_unix=now) is True


def test_anchor_not_passing_outside_window() -> None:
    """anchor_ts далеко от now → False."""
    end = 1_000_000.0
    anchor_ts = end - 24 * 3600
    now_far_after = anchor_ts + ANCHOR_WINDOW_SEC + 100  # за пределами окна
    assert _anchor_passing_now(end, anchor_h=24, now_unix=now_far_after) is False

    now_far_before = anchor_ts - ANCHOR_WINDOW_SEC - 100
    assert _anchor_passing_now(end, anchor_h=24, now_unix=now_far_before) is False


def test_only_one_anchor_passes_at_a_time() -> None:
    """В типичный момент ровно один anchor попадает в окно (anchors разнесены >>5min)."""
    from app.ml.feature_kernel import CANONICAL_ANCHORS

    end = 1_000_000.0
    # now = "сейчас 24h before end" — должен пройти только anchor=24h
    now = end - 24 * 3600
    passing = [h for h in CANONICAL_ANCHORS if _anchor_passing_now(end, h, now)]
    assert passing == [24]

    # now = 168h before end → только 168h
    now2 = end - 168 * 3600
    passing2 = [h for h in CANONICAL_ANCHORS if _anchor_passing_now(end, h, now2)]
    assert passing2 == [168]


def test_no_anchor_passes_in_quiet_zone() -> None:
    """Между anchor moments — никакой anchor не проходит."""
    from app.ml.feature_kernel import CANONICAL_ANCHORS

    end = 1_000_000.0
    # now = 50h before end (между 24h и 72h) — никакой anchor не должен проходить
    now = end - 50 * 3600
    passing = [h for h in CANONICAL_ANCHORS if _anchor_passing_now(end, h, now)]
    assert passing == []


@pytest.mark.asyncio
async def test_run_returns_zero_when_no_active_markets(monkeypatch) -> None:
    """Если нет active markets — возвращаем нули, не падаем."""
    from app.jobs import live_price_snapshot_writer as mod

    async def fake_fetch(query, *args, **kwargs):
        return []

    monkeypatch.setattr(mod, "fetch", fake_fetch)
    result = await mod.run_live_price_snapshot_writer()
    assert result == {"markets": 0, "snapshots_written": 0, "no_price": 0}
