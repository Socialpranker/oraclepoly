"""Тест классификатора transient vs terminal reject reasons (audit топ-4)."""

from __future__ import annotations

import pytest

from app.services.reject_classify import is_transient_reject


@pytest.mark.parametrize(
    "reason",
    [
        "kill_switch:redis_flag_set",
        "daemon_paused:D3",
        "max_inflight_orders:5",
        "max_open_positions:10",
        "daily_loss:cap_hit",
        "market_closed_or_empty_book:no_asks",
        "stale_mtm:120s",
        "place_error:ReadTimeout",
    ],
)
def test_transient_reasons_return_true(reason: str) -> None:
    assert is_transient_reject(reason) is True


@pytest.mark.parametrize(
    "reason",
    [
        "category_blacklist:sports",
        "price_drift:12.3pp",
        "stale_signal:301s",
        "wide_spread:0.08",
    ],
)
def test_terminal_reasons_return_false(reason: str) -> None:
    assert is_transient_reject(reason) is False


def test_none_returns_false() -> None:
    assert is_transient_reject(None) is False


def test_empty_string_returns_false() -> None:
    assert is_transient_reject("") is False


def test_stale_signal_is_terminal():
    """stale_signal (30-мин зомби-cap) — терминальный: ретрай бессмыслен."""
    assert is_transient_reject("stale_signal:1850s>1800s") is False


def test_ev_negative_at_live_price_is_terminal():
    """Edge умер на live-цене — ретрай бессмыслен (цена не вернётся по волшебству)."""
    assert is_transient_reject("ev_negative_at_live_price:ev_below_threshold_net (...)") is False
