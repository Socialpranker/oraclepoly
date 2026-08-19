"""Tests для feature_builder — pure functions, no I/O."""

from __future__ import annotations

import pytest

from app.backtest.feature_builder import (
    aggregate_trade_buckets_window,
    compute_buy_imbalance_asof,
    compute_momentum_pp,
    yes_price_at_offset,
)


def _snap(htr: int, yes: float, ts: int = 1700000000) -> dict:
    return {"hours_to_resolve": htr, "yes_price": yes, "trade_ts": ts}


def _bucket(bucket_ts: int, vol: float = 1000.0, takers: int = 5, vwap: float = 0.5,
            buy_vol: float = 500.0, sell_vol: float = 500.0, n_trades: int = 10,
            min_p: float = 0.4, max_p: float = 0.6) -> dict:
    return {
        "bucket_ts_unix": bucket_ts,
        "total_volume_usd": vol,
        "n_distinct_takers": takers,
        "vwap_yes": vwap,
        "buy_volume": buy_vol,
        "sell_volume": sell_vol,
        "n_trades": n_trades,
        "min_yes_price": min_p,
        "max_yes_price": max_p,
    }


# === yes_price_at_offset ===


def test_yes_price_at_offset_exact_match() -> None:
    snaps = [_snap(168, 0.5), _snap(96, 0.6), _snap(24, 0.7)]
    # current at idx=2 (24h), offset=72 → target=96h → snaps[1] = 0.6
    assert yes_price_at_offset(snaps, current_idx=2, offset_hours=72) == 0.6


def test_yes_price_at_offset_tolerance_within_2h() -> None:
    """±2h match allowed (canonical hourly granularity)."""
    snaps = [_snap(170, 0.5), _snap(95, 0.6), _snap(24, 0.7)]
    # current 24h, want 24+72=96h, snaps has 95h (diff 1) → match
    assert yes_price_at_offset(snaps, 2, 72) == 0.6


def test_yes_price_at_offset_too_far_returns_none() -> None:
    """Diff > 2h → no match."""
    snaps = [_snap(200, 0.5), _snap(24, 0.7)]
    # 24h + 72 = 96h → closest is 200 (diff 104) → None
    assert yes_price_at_offset(snaps, 1, 72) is None


def test_yes_price_at_offset_empty() -> None:
    assert yes_price_at_offset([], 0, 72) is None
    assert yes_price_at_offset([_snap(24, 0.5)], current_idx=5, offset_hours=72) is None


# === aggregate_trade_buckets_window ===


def test_aggregate_empty_buckets() -> None:
    result = aggregate_trade_buckets_window([], tick_ts=1000, window_hours=3)
    assert result["volume_usd"] == 0.0
    assert result["n_takers"] == 0
    assert result["vwap_yes"] is None


def test_aggregate_single_bucket_in_window() -> None:
    """3h window = [tick - 10800, tick]. Bucket at tick-3600 (1h ago) included."""
    buckets = [_bucket(bucket_ts=1000 - 3600, vol=1000, takers=5, buy_vol=600, sell_vol=400)]
    result = aggregate_trade_buckets_window(buckets, tick_ts=1000, window_hours=3)
    assert result["volume_usd"] == 1000.0
    assert result["n_takers"] == 5
    assert result["buy_volume_usd"] == 600.0
    assert result["sell_volume_usd"] == 400.0


def test_aggregate_outside_window_excluded() -> None:
    """Bucket older than window — not included."""
    buckets = [_bucket(bucket_ts=1000 - 20000, vol=500)]  # ~5.5h ago
    result = aggregate_trade_buckets_window(buckets, tick_ts=1000, window_hours=3)
    assert result["volume_usd"] == 0.0


def test_aggregate_vwap_weighted_by_volume() -> None:
    """VWAP = sum(vwap × vol) / sum(vol)."""
    buckets = [
        _bucket(bucket_ts=1000 - 1000, vol=200, vwap=0.4),
        _bucket(bucket_ts=1000 - 500, vol=800, vwap=0.6),
    ]
    result = aggregate_trade_buckets_window(buckets, tick_ts=1000, window_hours=3)
    # Expected: (0.4 × 200 + 0.6 × 800) / 1000 = (80 + 480) / 1000 = 0.56
    assert result["vwap_yes"] == pytest.approx(0.56)


def test_aggregate_min_max_yes_price() -> None:
    buckets = [
        _bucket(bucket_ts=900, min_p=0.30, max_p=0.55),
        _bucket(bucket_ts=950, min_p=0.10, max_p=0.65),
    ]
    result = aggregate_trade_buckets_window(buckets, tick_ts=1000, window_hours=3)
    assert result["min_yes_price"] == 0.10
    assert result["max_yes_price"] == 0.65


# === compute_momentum_pp ===


def test_momentum_pp_positive() -> None:
    """yes went from 0.4 → 0.6 за 3h → +0.20 pp momentum.

    compute_momentum_pp реально берёт 1h window на now и 1h window на (now - window_hours).
    Bucket должен попасть в обе [tick-3600, tick] для current и [tick-3h-3600, tick-3h]
    для past.
    """
    tick = 10000
    buckets = [
        # Past window: [tick - 3h - 3600, tick - 3h] = [tick - 14400, tick - 10800]
        _bucket(bucket_ts=tick - 11000, vol=500, vwap=0.4),
        # Current window: [tick - 3600, tick] = [tick - 3600, tick]
        _bucket(bucket_ts=tick - 1800, vol=500, vwap=0.6),
    ]
    momentum = compute_momentum_pp(buckets, tick_ts=tick, window_hours=3)
    assert momentum == pytest.approx(0.2, abs=0.01)


def test_momentum_pp_none_if_missing_window() -> None:
    """Если в одном из окон нет buckets — return None."""
    buckets = [_bucket(bucket_ts=999, vol=500, vwap=0.5)]
    # past window is 3h ago, only current has data
    momentum = compute_momentum_pp(buckets, tick_ts=1000, window_hours=3)
    assert momentum is None


# === compute_buy_imbalance_asof ===


def test_compute_buy_imbalance_asof_ratio() -> None:
    # buckets в окне [cutoff-24h, cutoff]: buy=300, sell=700 → ratio 0.3
    cutoff = 1_700_000_000
    buckets = [
        {"bucket_ts_unix": cutoff - 7200, "buy_volume": 100.0, "sell_volume": 200.0, "total_volume_usd": 300.0},
        {"bucket_ts_unix": cutoff - 3600, "buy_volume": 200.0, "sell_volume": 500.0, "total_volume_usd": 700.0},
    ]
    imb = compute_buy_imbalance_asof(buckets, cutoff_ts=cutoff, window_hours=24)
    assert abs(imb - 0.3) < 1e-9


def test_compute_buy_imbalance_asof_empty_returns_neutral() -> None:
    assert compute_buy_imbalance_asof([], cutoff_ts=1_700_000_000, window_hours=24) == 0.5


def test_compute_buy_imbalance_asof_no_lookahead() -> None:
    """Сделки ПОСЛЕ cutoff не учитываются (агрегатор берёт bucket_ts <= cutoff)."""
    cutoff = 1_700_000_000
    buckets = [{"bucket_ts_unix": cutoff + 3600, "buy_volume": 1000.0, "sell_volume": 0.0, "total_volume_usd": 1000.0}]
    assert compute_buy_imbalance_asof(buckets, cutoff_ts=cutoff, window_hours=24) == 0.5
