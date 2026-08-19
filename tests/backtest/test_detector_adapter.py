"""Tests для DetectorAdapter — canonical row → market_event reconstruction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.backtest.detector_adapter import (
    build_market_event_at_tick,
    get_detector,
    register_detector,
)

# === build_market_event_at_tick ===


def _market_row(**overrides) -> dict:
    base = {
        "condition_id": "0xabc",
        "event_id": "ev_001",
        "question": "Will X happen?",
        "category": "Sports",
        "lifetime_volume_usd": 100_000.0,
        "horizon_days": 10.0,
        "neg_risk": False,
        "yes_won": 1,
    }
    base.update(overrides)
    return base


def _snapshot_row(**overrides) -> dict:
    base = {
        "condition_id": "0xabc",
        "hours_to_resolve": 24,
        "yes_price": 0.97,
        "trade_ts": 1700000000,  # 2023-11-14
    }
    base.update(overrides)
    return base


def test_adapter_returns_dict_and_datetime() -> None:
    market_event, tick_now = build_market_event_at_tick(_market_row(), _snapshot_row())
    assert isinstance(market_event, dict)
    assert isinstance(tick_now, datetime)
    assert tick_now.tzinfo is UTC


def test_adapter_reconstructs_end_date() -> None:
    """end_date = tick_now + hours_to_resolve."""
    snapshot = _snapshot_row(hours_to_resolve=48)
    market_event, tick_now = build_market_event_at_tick(_market_row(), snapshot)
    expected_end = tick_now + timedelta(hours=48)
    assert market_event["end_date"] == expected_end


def test_adapter_category_preserved_as_native() -> None:
    """Category preserved in native form ('Sports' not 'sports').

    D44 lowercase'ит internally в `_is_allowed`. Other detectors (D30-D34) compare
    Title-case directly. Adapter передаёт canonical native form.
    """
    market_event, _ = build_market_event_at_tick(_market_row(category="Sports"), _snapshot_row())
    assert market_event["category"] == "Sports"


def test_adapter_current_price_from_snapshot() -> None:
    snap = _snapshot_row(yes_price=0.975)
    market_event, _ = build_market_event_at_tick(_market_row(), snap)
    assert market_event["current_price"] == pytest.approx(0.975)


def test_adapter_volume_proxy_from_lifetime() -> None:
    """volume_24h = lifetime_volume / horizon_days; liquidity = volume * 0.1."""
    market = _market_row(lifetime_volume_usd=10_000.0, horizon_days=10.0)
    market_event, _ = build_market_event_at_tick(market, _snapshot_row())
    assert market_event["volume_24h"] == pytest.approx(1000.0)
    assert market_event["liquidity"] == pytest.approx(100.0)


def test_adapter_volume_proxy_safe_on_zero_horizon() -> None:
    """horizon_days=0 → use max(1, horizon) to avoid div-by-zero."""
    market = _market_row(lifetime_volume_usd=10_000.0, horizon_days=0)
    market_event, _ = build_market_event_at_tick(market, _snapshot_row())
    assert market_event["volume_24h"] == pytest.approx(10000.0)


def test_adapter_uses_true_24h_volume_when_buckets_provided() -> None:
    """С trade_buckets — true rolling 24h, не lifetime-proxy.

    Lifetime $10K / 10 days = $1K/day proxy. С buckets — реальный $250 за
    last 24h (rolling sum). Это закрывает gap для underdog detectors (D7b).
    """
    market = _market_row(lifetime_volume_usd=10_000.0, horizon_days=10.0)
    snap = _snapshot_row(trade_ts=1700000000)
    # 24 hourly buckets within last 24h, total $250
    buckets = [
        {
            "bucket_ts_unix": 1700000000 - i * 3600,
            "total_volume_usd": 10.0,
            "n_distinct_takers": 1,
            "buy_volume": 5.0,
            "sell_volume": 5.0,
            "vwap_yes": 0.5,
            "n_trades": 1,
            "min_yes_price": 0.5,
            "max_yes_price": 0.5,
        }
        for i in range(25)  # 25 buckets, but window=24 → first 24 match
    ]
    market_event, _ = build_market_event_at_tick(
        market,
        snap,
        trade_buckets_for_market=buckets,
    )
    # Sum of 24 buckets × $10 = $240 (≈ 25 - 1, since exact 24h window excludes oldest)
    assert market_event["volume_24h"] == pytest.approx(250.0, abs=10.0)
    assert market_event["liquidity"] == pytest.approx(12.5, abs=0.5)


def test_adapter_falls_back_to_proxy_when_no_buckets() -> None:
    """Если trade_buckets=None (backwards compat) — старая lifetime-proxy."""
    market = _market_row(lifetime_volume_usd=10_000.0, horizon_days=10.0)
    market_event, _ = build_market_event_at_tick(
        market,
        _snapshot_row(),
        trade_buckets_for_market=None,
    )
    assert market_event["volume_24h"] == pytest.approx(1000.0)  # lifetime / horizon
    assert market_event["liquidity"] == pytest.approx(100.0)


def test_adapter_buckets_empty_window_gives_zero() -> None:
    """trade_buckets есть но в window 24h пусто → true volume = 0 (НЕ proxy)."""
    market = _market_row(lifetime_volume_usd=10_000.0, horizon_days=10.0)
    snap = _snapshot_row(trade_ts=1700000000)
    # Buckets есть, но все вне 24h window (>24h ago)
    buckets = [
        {
            "bucket_ts_unix": 1700000000 - 100 * 3600,  # 100h ago
            "total_volume_usd": 500.0,
            "n_distinct_takers": 5,
            "buy_volume": 250.0,
            "sell_volume": 250.0,
            "vwap_yes": 0.5,
            "n_trades": 5,
            "min_yes_price": 0.4,
            "max_yes_price": 0.6,
        },
    ]
    market_event, _ = build_market_event_at_tick(
        market,
        snap,
        trade_buckets_for_market=buckets,
    )
    assert market_event["volume_24h"] == 0.0
    assert market_event["liquidity"] == 0.0


def test_adapter_passes_event_id() -> None:
    market_event, _ = build_market_event_at_tick(_market_row(event_id="ev_42"), _snapshot_row())
    assert market_event["event_id"] == "ev_42"


def test_adapter_market_id_eq_condition_id() -> None:
    market_event, _ = build_market_event_at_tick(_market_row(condition_id="0xdef"), _snapshot_row())
    assert market_event["market_id"] == "0xdef"
    assert market_event["condition_id"] == "0xdef"


# === wallet_score / PIT-safe anchor (D34 smart_money_taker_share_4h) ===


def test_pit_safe_anchor_picks_smallest_ge_hours() -> None:
    """Anchor >= hours_to_resolve (окно якоря не позже тика). H>72 → None."""
    from app.backtest.detector_adapter import _pit_safe_anchor

    assert _pit_safe_anchor(3.0) == 4  # smallest A>=3
    assert _pit_safe_anchor(4.0) == 4  # exact
    assert _pit_safe_anchor(5.0) == 24  # 4<5 → look-ahead, бери 24
    assert _pit_safe_anchor(14.0) == 24
    assert _pit_safe_anchor(24.0) == 24  # exact
    assert _pit_safe_anchor(50.0) == 72  # 24<50 → бери 72
    assert _pit_safe_anchor(72.0) == 72  # exact
    assert _pit_safe_anchor(100.0) is None  # нет безопасного якоря → skip


def test_wallet_score_sets_smart_money_share_at_pit_safe_anchor() -> None:
    """hours_to_resolve=5 → anchor 24 (НЕ 4: anchor 4 кончается позже тика = look-ahead)."""
    market_row = _market_row(condition_id="0xaaa", category="Sports")
    snapshot_row = _snapshot_row(hours_to_resolve=5)  # PIT-safe anchor = 24
    wallet_for_market = {
        4: {"smart_money_taker_share": 0.07, "taker_pnl_weighted_usd": 150.0,
            "taker_roi_weighted": 25.0, "n_takers_24h_before": 5},
        24: {"smart_money_taker_share": 0.50, "taker_pnl_weighted_usd": 1.0,
             "taker_roi_weighted": 1.0, "n_takers_24h_before": 9},
    }
    me, _ = build_market_event_at_tick(
        market_row, snapshot_row, wallet_score_for_market=wallet_for_market
    )
    # Берёт anchor 24 (0.50), НЕ anchor 4 (0.07) — anchor 4 содержал бы будущие сделки
    assert me["smart_money_taker_share_4h"] == 0.50


def test_wallet_score_none_when_no_data() -> None:
    market_row = _market_row(condition_id="0xzzz", category="Sports")
    snapshot_row = _snapshot_row(hours_to_resolve=5)
    me, _ = build_market_event_at_tick(market_row, snapshot_row, wallet_score_for_market=None)
    assert me["smart_money_taker_share_4h"] is None


def test_wallet_score_empty_dict_no_anchor_match() -> None:
    """{} (запрошено, но пусто) и dict без PIT-safe anchor → None, без KeyError."""
    market_row = _market_row(condition_id="0xaaa", category="Sports")
    snapshot_row = _snapshot_row(hours_to_resolve=5)  # PIT-safe anchor = 24
    me_empty, _ = build_market_event_at_tick(market_row, snapshot_row, wallet_score_for_market={})
    assert me_empty["smart_money_taker_share_4h"] is None
    # dict есть, но anchor 24 отсутствует (только 4) → None, не падение
    me_miss, _ = build_market_event_at_tick(
        market_row, snapshot_row, wallet_score_for_market={4: {"smart_money_taker_share": 0.9}}
    )
    assert me_miss["smart_money_taker_share_4h"] is None


# === register_detector / get_detector ===


def test_get_detector_d44_registered() -> None:
    """D44 should be auto-registered on module import."""
    detect = get_detector("d44")
    assert callable(detect)


def test_get_detector_case_insensitive() -> None:
    detect_lower = get_detector("d44")
    detect_upper = get_detector("D44")
    assert detect_lower is detect_upper


def test_get_detector_unknown_raises() -> None:
    with pytest.raises(KeyError, match="not registered"):
        get_detector("d999")


def test_register_detector_allows_lookup() -> None:
    """Custom detector can be registered."""

    def fake_detect(market_event, now=None):
        return None

    register_detector("d_test_99", fake_detect)
    retrieved = get_detector("d_test_99")
    assert retrieved is fake_detect


def test_wallet_score_sets_d14_fields() -> None:
    """build_market_event_at_tick проставляет D14 taker_pnl/roi/n_takers из anchor."""
    from app.backtest.detector_adapter import build_market_event_at_tick

    market_row = _market_row(condition_id="0xaaa", category="Crypto")
    snapshot_row = _snapshot_row(hours_to_resolve=6)  # PIT-safe anchor 24
    wallet_for_market = {
        24: {"smart_money_taker_share": 0.07, "taker_pnl_weighted_usd": 150.0,
             "taker_roi_weighted": 25.0, "n_takers_24h_before": 5},
    }
    me, _ = build_market_event_at_tick(
        market_row, snapshot_row, wallet_score_for_market=wallet_for_market
    )
    assert me["taker_pnl_weighted_usd_24h"] == 150.0
    assert me["taker_roi_weighted_24h"] == 25.0
    assert me["n_takers_24h_before"] == 5


def test_d14_fields_none_when_no_wallet() -> None:
    """Нет wallet → D14-поля None (detect() сам skip)."""
    from app.backtest.detector_adapter import build_market_event_at_tick

    market_row = _market_row(condition_id="0xzzz", category="Crypto")
    snapshot_row = _snapshot_row(hours_to_resolve=6)
    me, _ = build_market_event_at_tick(market_row, snapshot_row, wallet_score_for_market=None)
    assert me["taker_pnl_weighted_usd_24h"] is None
    assert me["taker_roi_weighted_24h"] is None
    assert me["n_takers_24h_before"] is None


def test_buy_imbalance_asof_set_from_buckets() -> None:
    """build_market_event_at_tick считает buy_imbalance_asof из trade_buckets."""
    from app.backtest.detector_adapter import build_market_event_at_tick

    market_row = _market_row(condition_id="0xaaa", category="Crypto")
    snapshot_row = _snapshot_row(hours_to_resolve=6)
    trade_ts = int(snapshot_row["trade_ts"])
    buckets = [
        {"bucket_ts_unix": trade_ts - 3600, "buy_volume": 300.0, "sell_volume": 700.0,
         "total_volume_usd": 1000.0, "n_distinct_takers": 5, "vwap_yes": 0.55,
         "n_trades": 10, "min_yes_price": 0.5, "max_yes_price": 0.6},
    ]
    me, _ = build_market_event_at_tick(
        market_row, snapshot_row, trade_buckets_for_market=buckets
    )
    assert abs(me["buy_imbalance_asof"] - 0.3) < 1e-9  # 300/(300+700)


def test_d14_registered_in_adapter() -> None:
    from app.backtest.detector_adapter import get_detector
    fn = get_detector("d14")  # не должно бросать KeyError
    assert callable(fn)
